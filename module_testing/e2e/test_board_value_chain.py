"""Value Chain board, end to end.

The corpus is store-backed. Seeding a filing row for every ``config.SEED`` ticker
means ``fetch_corpus`` finds nothing to fetch, so the POST path never touches
EDGAR and the tests exercise the real job + render + refresh flow offline.
"""
from __future__ import annotations

import re
import time

import pytest

from RevenueChaser.module_value_chain import config

_TEXT = (
    "Item 1. Business\n"
    "We purchase wafers from Taiwan Semiconductor Manufacturing Company Limited "
    "(TSMC), our primary foundry supplier. Our customers include Microsoft and "
    "Amazon. We compete with Advanced Micro Devices.\n"
    "Item 1A. Risk Factors\nWe depend on a limited number of suppliers."
)


@pytest.fixture
def seeded_chain(dashboard, monkeypatch):
    import RevenueChaser.core.rawstore as rawstore

    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
    now = time.time()
    rawstore.write_rows("chain_filings", [{
        "ticker": t, "cik": "1000", "form": "10-K", "filed": "2026-02-01",
        "accession": "acc-" + t, "primary_doc": "http://x", "item_text": _TEXT,
        "quality": "ok", "fetched_at": now,
    } for t in config.SEED])
    return dashboard


class TestFetch:
    def test_fetch_persists_the_corpus(self, dashboard, seeded_chain):
        result = dashboard.run_job("/run/module_value_chain")
        assert result.ok, f"value chain fetch failed: {result}\n{dashboard.logs()}"
        assert dashboard.table_count("chain_filings") == len(config.SEED)
        assert dashboard.table_count("chain_mentions") > 0

    def test_the_board_renders_edges(self, dashboard, seeded_chain):
        dashboard.run_job("/run/module_value_chain")
        html = dashboard.view("module_value_chain").text
        assert "Value Chain" in html
        assert "TSM" in html and "supplier" in html
        assert 'id="q"' in html
        # Filter chrome: the shared filter bar (#383 C1b), filled by RCFilters.mount in the page.
        assert 'id="rc-filters"' in html and "RCFilters.mount(" in html
        assert 'id="mc-tabs"' not in html
        assert "Last sync:" in html


class TestPrecisionCoverage:
    def test_the_tile_counts_labels_against_served_links(self, dashboard, seeded_chain):
        """"labels cover N of M links": M from the store, N from the committed fixture (#61)."""
        import duckdb

        from RevenueChaser.core import rawstore
        from RevenueChaser.module_value_chain import precision

        result = dashboard.run_job("/run/module_value_chain")
        assert result.ok, f"value chain fetch failed: {result}\n{dashboard.logs()}"
        html = dashboard.view("module_value_chain").text
        m = re.search(r"labels cover (\d+) of (\d+) links", html)
        assert m, "precision tile has no label-coverage text"
        covered, links = int(m.group(1)), int(m.group(2))

        db = "edgar.duckdb" if rawstore.MANIFEST["chain_mentions"].get("db") == "edgar" else "raw.duckdb"
        conn = duckdb.connect(str(dashboard.store_dir / db), read_only=True)
        try:
            served = {tuple(r) for r in conn.execute(
                "SELECT DISTINCT src, dst_ticker, relation FROM chain_mentions "
                "WHERE relation IN ('supplier', 'customer')").fetchall()}
        finally:
            conn.close()
        assert links == len(served) > 0
        labelled = {(l["src"], l["dst_ticker"], l["relation"]) for l in precision.load_labels()
                    if l.get("label") in (True, False)}
        assert covered == len(labelled & served)
        # Every seed names TSMC as supplier: the fixture judges AVGO→TSM and NXPI→TSM.
        assert covered == 2
        # n = 2 is below the minimum labelled sample: no pass, a warning tile (#118 C27).
        tile = re.search(r'<div class="(stat[^"]*)"><div class="v">[^<]*</div>'
                         r'<div class="l">Precision</div><div class="d">([^<]*)</div>', html)
        assert tile, "no precision tile"
        assert tile.group(1) == "stat warn"
        assert f"insufficient sample (min {precision.MIN_LABELED_N})" in tile.group(2)


class TestGraph:
    def test_the_drawn_graph_is_the_per_ticker_graph(self, dashboard, seeded_chain):
        """#118 C19: the board indexes the edges once; the graph embedded for a ticker
        is still exactly ``focal_graph(edges, ticker)`` built on its own."""
        import json

        import duckdb

        from RevenueChaser.module_value_chain import engine

        result = dashboard.run_job("/run/module_value_chain")
        assert result.ok, f"value chain fetch failed: {result}\n{dashboard.logs()}"
        html = dashboard.view("module_value_chain").text
        m = re.search(r"^var GRAPHS = (.*);$", html, re.M)
        assert m, "no GRAPHS payload on the page"
        graphs = json.loads(m.group(1).replace("<\\/", "</"))

        conn = duckdb.connect(str(dashboard.store_dir / "edgar.duckdb"), read_only=True)
        try:
            cur = conn.execute("SELECT * FROM chain_mentions")
            cols = [d[0] for d in cur.description]
            edges = [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()
        # The board serves the stored edges in store order; rebuild that order.
        payload_edges = json.loads(json.dumps(edges, default=str))
        assert "NVDA" in graphs
        want = engine.focal_graph(payload_edges, "NVDA", depth=2)
        assert sorted(map(json.dumps, graphs["NVDA"]["links"])) == sorted(
            map(json.dumps, want["links"]))
        assert any(l["src"] == "NVDA" and l["dst"] == "TSM" and l["hop"] == 1
                   for l in graphs["NVDA"]["links"])


class TestRefreshIsOfflineAndFaithful:
    def test_refresh_rebuilds_a_deleted_artifact(self, dashboard, seeded_chain, clear_cache):
        dashboard.run_job("/run/module_value_chain")
        clear_cache()
        assert not dashboard.artifact("value_chain_latest.html").exists()
        r = dashboard.view("module_value_chain", refresh=True)
        assert r.status_code == 200
        assert "Value Chain" in r.text

    def test_refresh_makes_no_network_calls(self, dashboard, seeded_chain,
                                            provider_routes, clear_cache):
        dashboard.run_job("/run/module_value_chain")
        clear_cache()
        before = provider_routes.request_count
        r = dashboard.view("module_value_chain", refresh=True)
        assert r.status_code == 200
        assert provider_routes.request_count == before, (
            "refresh reached the network: "
            f"{[q.path for q in provider_routes.requests()[before:]]}")

    def test_refresh_writes_nothing_to_the_store(self, dashboard, seeded_chain,
                                                 clear_cache):
        import duckdb

        def last_sync():
            con = duckdb.connect(str(dashboard.store_dir / "edgar.duckdb"), read_only=True)
            try:
                return con.execute(
                    "SELECT value FROM chain_meta WHERE key = 'last_sync'").fetchone()
            finally:
                con.close()

        dashboard.run_job("/run/module_value_chain")
        before = (last_sync(), dashboard.table_count("chain_mentions"))
        clear_cache()
        time.sleep(1.1)
        assert dashboard.view("module_value_chain", refresh=True).status_code == 200
        assert (last_sync(), dashboard.table_count("chain_mentions")) == before

    def test_refresh_survives_a_restart(self, dashboard, seeded_chain, clear_cache):
        dashboard.run_job("/run/module_value_chain")
        clear_cache()
        dashboard.restart()
        r = dashboard.view("module_value_chain", refresh=True)
        assert r.status_code == 200
        assert "Value Chain" in r.text


class TestRecordedMisses:
    def test_a_recorded_miss_is_left_out_of_pending_and_refresh_stays_pure(
            self, dashboard, seeded_chain, provider_routes, clear_cache):
        """#116 C26: a ``miss:<T>`` key is not Pending; the Refresh reads it, makes no
        network call and writes nothing (chain_meta included)."""
        import json
        from datetime import datetime, timezone

        import duckdb

        import RevenueChaser.core.rawstore as rawstore

        extra = {"MISSX": "9001", "PENDX": "9002"}
        rawstore.write_rows("equity_universe", [
            {"ticker": t, "market_cap": 1e12, "name": t} for t in (*config.SEED, *extra)])
        rawstore.write_rows("sec_exchange_file", [     # #115: the CIK map production reads (#301 D21)
            {"ticker": t, "cik": c, "name": t, "exchange": "Nasdaq", "valid_from": None, "status": "present",
             "source": "sec_exchange", "fetched_at": 1.0, "run_id": "seed", "run_at": 1.0} for t, c in extra.items()])
        rawstore.write_rows("chain_meta", [{"key": "miss:MISSX", "value": json.dumps({
            "reason": "no_filing", "note": "no 10-K / 20-F", "cik": "9001",
            "checked": datetime.now(timezone.utc).isoformat()})}])
        rawstore.ensure_table("chain_mentions")

        def chain_state():
            con = duckdb.connect(str(dashboard.store_dir / "edgar.duckdb"), read_only=True)
            try:
                return (con.execute("SELECT key, value FROM chain_meta ORDER BY key").fetchall(),
                        con.execute("SELECT count(*) FROM chain_filings").fetchone(),
                        con.execute("SELECT count(*) FROM chain_mentions").fetchone())
            finally:
                con.close()

        before, requests = chain_state(), provider_routes.request_count
        clear_cache()
        r = dashboard.view("module_value_chain", refresh=True)
        assert r.status_code == 200
        assert re.search(r'<div class="v">1</div><div class="l">Pending</div>'
                         r'<div class="d">not yet fetched · 1 with no 10-K / 20-F</div>', r.text), (
            "Pending must count PENDX only and name the recorded miss")
        assert provider_routes.request_count == requests, "refresh reached the network"
        assert chain_state() == before, "refresh wrote to the store"


class TestEmptyStore:
    def test_a_board_with_no_data_renders_an_empty_state(self, dashboard):
        r = dashboard.view("module_value_chain")
        assert r.status_code == 200
        assert "No companies with supplier / customer edges" in r.text
