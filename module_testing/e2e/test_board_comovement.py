"""Co-movement board, end to end.

The board is store-backed: membership comes from D01 (``price_bars_daily``, since
the #379 D01 switch), metadata from ``equity_universe``. These tests seed those
tables directly, the way Daily Bars and the universe pull leave them, and prove
the contract in docs/data_retrieval.md:

1. Fetch persists a dated run and renders the board, and downloads nothing
   (Daily Bars is D01's one writer).
2. Refresh rebuilds a deleted artifact from the store with zero provider calls.
3. Only what reached .store survives a restart.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from .harness.dashboard_driver import Dashboard, JobResult
from .harness.shared import clear_cache_dir, shared_dashboard, store_of

# The tests that only read what one default Fetch left behind share it (``fetched``); this mark keeps them on one
# worker under ``--dist loadgroup``. A test that switches the window, restarts, saves settings or watches the
# Fetch's own provider calls keeps its own dashboard (``seeded_comovement``).
SHARED = pytest.mark.xdist_group("e2e-comovement-shared-fetch")


def _returns(n=200, seed=7):
    """Four tight groups of four names each (#97: a group is named only when its industry is enriched
    versus the cut, so two equal groups in eight names, lift 2, would stay neutral)."""
    rng = np.random.default_rng(seed)
    data = {}
    for group in (("A1", "A2", "A3", "A4"), ("B1", "B2", "B3", "B4"),
                  ("C1", "C2", "C3", "C4"), ("D1", "D2", "D3", "D4")):
        base = rng.normal(0, 0.01, n)
        for t in group:
            data[t] = base + rng.normal(0, 0.0004, n)
    return pd.DataFrame(data, index=pd.date_range("2025-09-01", periods=n, freq="D"))


def _benchmarks(index, seed=7):
    """Benchmark columns (SPY/DIA/QQQ) on the same dates.

    D01 holds all three (Daily Bars keeps the benchmarks), so the Remove-market
    control has data.
    """
    rng = np.random.default_rng(seed + 1)
    return pd.DataFrame(
        {b: rng.normal(0, 0.008, len(index)) for b in ("SPY", "DIA", "QQQ")},
        index=index)


_INDUSTRY = {"A1": "Semiconductors", "A2": "Semiconductors", "A3": "Semiconductors",
             "A4": "Semiconductors", "B1": "Banks - Diversified", "B2": "Banks - Diversified",
             "B3": "Banks - Diversified", "B4": "Banks - Diversified",
             **{f"C{i}": "Oil & Gas E&P" for i in range(1, 5)},
             **{f"D{i}": "Utilities - Regulated Electric" for i in range(1, 5)}}


@pytest.fixture
def seeded_comovement(dashboard, monkeypatch):
    """Seed the dashboard's temp store with universe rows + D01 bars."""
    import RevenueChaser.core.rawstore as rawstore

    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")

    monkeypatch.setattr("RevenueChaser.core.config.MARKET_DIR", dashboard.env["RC_MARKET_DIR"])  # the prices store (G3)
    _seed()
    return dashboard


def _seed() -> None:
    import RevenueChaser.core.rawstore as rawstore
    ret = _returns()
    tickers = list(ret.columns)

    rawstore.replace_all("equity_universe", [{
        "ticker": t, "market_cap": 1.0e10, "sector": "Financial Services" if t[0] == "B" else "Technology",
        "price": 10.0, "name": t + " Inc", "industry": _INDUSTRY[t], "quote": {"symbol": t},
    } for t in tickers])

    now = time.time()
    rows = []
    for t in tickers:
        price = 100 * np.exp(ret[t].cumsum())
        for ts, p in price.items():
            rows.append({"ticker": t, "ts": ts.isoformat(), "open": p, "high": p,
                         "low": p, "close": float(p), "volume": 1.0, "cached_at": now})
    bench = _benchmarks(ret.index)
    for t in bench.columns:
        price = 100 * np.exp(bench[t].cumsum())
        for ts, p in price.items():
            rows.append({"ticker": t, "ts": ts.isoformat(), "open": p, "high": p,
                         "low": p, "close": float(p), "volume": 1.0, "cached_at": now})
    from RevenueChaser.module_comovement.tests.prices_bars import write_bars
    write_bars(rows)


@dataclass
class Fetched:
    dashboard: Dashboard
    result: JobResult


@pytest.fixture(scope="module")
def _fetched_once(tmp_path_factory, mock_provider):
    with shared_dashboard(tmp_path_factory, mock_provider, "comovement-shared") as dash:
        with store_of(dash):
            _seed()
        yield Fetched(dashboard=dash, result=dash.run_job("/run/module_comovement"))


@pytest.fixture
def fetched(_fetched_once):
    """The seeded store after ONE default Fetch, shared by the read-only tests of this module."""
    f = _fetched_once
    assert f.result.ok, f"comovement fetch failed: {f.result}\n{f.dashboard.logs()}"
    return f


class TestFetch:
    @SHARED
    def test_fetch_persists_a_run(self, fetched):
        dashboard, result = fetched.dashboard, fetched.result
        assert result.ok, f"comovement fetch failed: {result}\n{dashboard.logs()}"
        # the Fetch's own run (1y, reused by the precompute) + the 6m precompute (#95)
        assert dashboard.table_count("comov_runs") == 2
        assert dashboard.table_count("comov_members") > 0

    @SHARED
    def test_a_fetch_records_one_header_naming_its_runs(self, fetched):
        """#379 D2: both runs carry the Fetch's id, and history holds one header-only run with that id."""
        import json

        import duckdb

        dashboard, result = fetched.dashboard, fetched.result
        assert result.ok, f"comovement fetch failed: {result}\n{dashboard.logs()}"
        raw = duckdb.connect(str(dashboard.store_dir / "raw.duckdb"), read_only=True)
        try:
            ids = raw.execute("SELECT DISTINCT run_id FROM comov_runs").fetchall()
            as_ofs = sorted(r[0] for r in raw.execute("SELECT as_of FROM comov_runs").fetchall())
        finally:
            raw.close()
        hist = duckdb.connect(str(dashboard.store_dir / "history.duckdb"), read_only=True)
        try:
            heads = hist.execute("SELECT run_id, row_counts, params FROM result_runs "
                                 "WHERE board = 'module_comovement'").fetchall()
        finally:
            hist.close()
        assert len(heads) == 1 and ids == [(heads[0][0],)]
        assert heads[0][1] in ("{}", {})
        params = heads[0][2] if isinstance(heads[0][2], dict) else json.loads(heads[0][2])
        assert sorted(params["as_of"]) == as_ofs

    def test_a_switch_to_a_precomputed_window_serves_the_stored_run(
            self, dashboard, seeded_comovement):
        import json

        import duckdb

        result = dashboard.run_job("/run/module_comovement")
        assert result.ok, f"comovement fetch failed: {result}\n{dashboard.logs()}"
        conn = duckdb.connect(str(dashboard.store_dir / "raw.duckdb"))
        try:
            stored = conn.execute(
                "SELECT as_of FROM comov_runs WHERE window_days = 126").fetchall()
        finally:
            conn.close()
        assert len(stored) == 1
        r = dashboard.view("module_comovement", refresh=True, window=126)
        assert r.status_code == 200
        board = json.loads(dashboard.artifact("comovement/comovement_latest.json")
                           .read_text(encoding="utf-8"))
        assert board["window"] == 126 and board["as_of"] == stored[0][0]
        assert dashboard.table_count("comov_runs") == 2      # served, not re-stored

    @SHARED
    def test_the_board_renders_clusters(self, fetched):
        html = fetched.dashboard.view("module_comovement").text
        assert "Co-movement" in html
        assert "A1" in html and "B1" in html

    @SHARED
    def test_the_board_names_each_group_by_its_industry(self, fetched):
        html = fetched.dashboard.view("module_comovement").text
        # At the broad cut the four banks are one group, named after their shared industry text (#266).
        # (The four semiconductor names are not a group on this seed, and the finer cuts leave single names.)
        assert '"cluster_label": "Banks - Diversified 100%, ×4.0", "cluster_n": 4' in html
        assert "sub_industry" not in html and "industry_group" not in html


    def test_fetch_makes_no_network_calls_and_writes_no_swing_ohlc(
            self, dashboard, seeded_comovement, provider_routes):
        """#379 D01 switch: the bars are D01's; a Fetch calls no provider and leaves swing_ohlc alone."""
        before, yf_before = provider_routes.request_count, len(dashboard.yf_calls())
        result = dashboard.run_job("/run/module_comovement")
        assert result.ok, f"comovement fetch failed: {result}\n{dashboard.logs()}"
        assert provider_routes.request_count == before, (
            "the Fetch reached the network: "
            f"{[q.path for q in provider_routes.requests()[before:]]}")
        assert dashboard.yf_calls()[yf_before:] == []           # the yfinance fake records every call
        assert dashboard.table_count("swing_ohlc") == 0
        assert dashboard.table_count("comov_members") > 0


class TestRefreshIsOfflineAndFaithful:
    # The three shared tests empty the cache and Refresh rebuilds it, so a later shared test finds it whole again.
    @SHARED
    def test_refresh_rebuilds_a_deleted_artifact(self, fetched):
        dashboard = fetched.dashboard
        clear_cache_dir(dashboard.cache_dir)
        assert not dashboard.artifact("comovement_latest.html").exists()
        r = dashboard.view("module_comovement", refresh=True)
        assert r.status_code == 200
        assert "Co-movement" in r.text
        # The empty-state page also says "Co-movement": only the rebuilt artifact and its clusters prove the rebuild.
        assert dashboard.artifact("comovement_latest.html").exists(), "Refresh did not rebuild the artifact"
        assert "A1" in r.text and "B1" in r.text

    @SHARED
    def test_refresh_makes_no_network_calls(self, fetched, provider_routes):
        dashboard = fetched.dashboard
        clear_cache_dir(dashboard.cache_dir)
        before = provider_routes.request_count
        r = dashboard.view("module_comovement", refresh=True)
        assert r.status_code == 200
        assert provider_routes.request_count == before, (
            "refresh reached the network: "
            f"{[q.path for q in provider_routes.requests()[before:]]}")

    def test_refresh_survives_a_restart(self, dashboard, seeded_comovement, clear_cache):
        dashboard.run_job("/run/module_comovement")
        clear_cache()
        dashboard.restart()
        r = dashboard.view("module_comovement", refresh=True)
        assert r.status_code == 200
        assert "Co-movement" in r.text

    @SHARED
    def test_refresh_does_not_write_the_store(self, fetched):
        dashboard = fetched.dashboard
        before = dashboard.table_count("comov_runs")
        clear_cache_dir(dashboard.cache_dir)
        r = dashboard.view("module_comovement", refresh=True)
        assert r.status_code == 200
        assert dashboard.table_count("comov_runs") == before, (
            "Refresh wrote a comov_runs row; docs/data_retrieval.md says Refresh writes .cache only")


class TestEmptyStore:
    def test_a_board_with_no_data_renders_an_empty_state(self, dashboard):
        r = dashboard.view("module_comovement")
        assert r.status_code == 200
        assert "No clusters yet" in r.text


# ── window control + editable settings (#40 Part B) ──────────────────────────
import re  # noqa: E402

_SETTINGS_URL = "/api/settings/board/module_comovement"


def _seed_with_late_listers(dashboard, monkeypatch):
    """12 names: A/B groups with 330 sessions, group C listed 150 sessions ago.

    330 sessions of SPY so the default window + holdout (252 + 63) fit the stored
    history (the save rule). At the default MIN_OBS=126 C clusters (149 returns);
    at MIN_OBS=200 it is "listed recently" and drops out.
    """
    import RevenueChaser.core.rawstore as rawstore

    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")

    monkeypatch.setattr("RevenueChaser.core.config.MARKET_DIR", dashboard.env["RC_MARKET_DIR"])  # the prices store (G3)
    n, c_rows = 330, 150
    ret = _returns(n)
    rng = np.random.default_rng(11)
    base = rng.normal(0, 0.01, n)
    for t in ("C1", "C2", "C3", "C4"):
        col = base + rng.normal(0, 0.0004, n)
        col[: n - c_rows] = np.nan
        ret[t] = col
    industry = dict(_INDUSTRY, C1="Biotechnology", C2="Biotechnology", C3="Biotechnology",
                    C4="Biotechnology")
    rawstore.replace_all("equity_universe", [{
        "ticker": t, "market_cap": 1.0e10, "sector": "Healthcare", "price": 10.0,
        "name": t + " Inc", "industry": industry[t], "quote": {"symbol": t},
    } for t in ret.columns])
    now = time.time()
    rows = []
    for frame in (ret, _benchmarks(ret.index)):
        for t in frame.columns:
            col = frame[t]
            price = 100 * np.exp(col.fillna(0).cumsum())
            for ts, p in price.items():
                if np.isnan(col[ts]) and t[0] == "C":
                    continue
                rows.append({"ticker": t, "ts": ts.isoformat(), "open": p, "high": p,
                             "low": p, "close": float(p), "volume": 1.0, "cached_at": now})
    from RevenueChaser.module_comovement.tests.prices_bars import write_bars
    write_bars(rows)
    result = dashboard.run_job("/run/module_comovement")
    assert result.ok, f"comovement fetch failed: {result}\n{dashboard.logs()}"
    return dashboard


def _step(html: str, prefix: str) -> int:
    from RevenueChaser.module_testing.e2e.test_board_settings import _card_steps

    hits = {k: v for k, v in _card_steps(html).items() if k.startswith(prefix)}
    assert len(hits) == 1, f"expected one card row starting {prefix!r}, got {hits}"
    return next(iter(hits.values()))


def _post(dashboard, body):
    return dashboard.client.post(_SETTINGS_URL, json=body)


def _board(dashboard, **params):
    return dashboard.view("module_comovement", **params).text


class TestWindowControl:
    def test_a_two_month_window_recomputes_offline_and_locks_the_horizon(
            self, dashboard, seeded_comovement, provider_routes, clear_cache):
        dashboard.run_job("/run/module_comovement")
        clear_cache()
        before_provider = provider_routes.request_count
        before_yf = dashboard.yf_calls()
        r = dashboard.view("module_comovement", refresh=True, window=42)
        assert r.status_code == 200, r.text[:300]
        html = r.text
        assert re.search(r'data-v="42" aria-checked="true"[^>]*>2m', html), (
            "the 2m window button is not the active one")
        hz = re.search(r'data-group="horizon">(.*?)</div>', html, re.S).group(1)
        assert hz.count('aria-disabled="true"') == 3, "3d/5d/10d must be locked"
        assert "Short windows use 1-day returns only" in html
        assert "noisy, exploratory" in html
        assert "too short for the out-of-sample persistence test" in html
        assert provider_routes.request_count == before_provider, (
            "a window change reached the network: "
            f"{[q.path for q in provider_routes.requests()[before_provider:]]}")
        assert dashboard.yf_calls() == before_yf, (
            f"a window change reached yfinance: {dashboard.yf_calls()[len(before_yf):]}")

    def test_the_window_survives_a_horizon_change_link(self, dashboard, seeded_comovement):
        dashboard.run_job("/run/module_comovement")
        html = dashboard.view("module_comovement", refresh=True, window=126).text
        assert re.search(r'data-v="126" aria-checked="true"', html)
        assert "aria-disabled" not in re.search(
            r'data-group="horizon">(.*?)</div>', html, re.S).group(1)
        assert "var WINDOW = 126;" in html

    def test_an_unknown_window_is_refused(self, dashboard, seeded_comovement):
        dashboard.run_job("/run/module_comovement")
        assert dashboard.view("module_comovement", refresh=True, window=99).status_code == 400


class TestSettingsSaveAndReset:
    def test_save_runs_a_job_changes_the_card_and_the_badge_then_reset_undoes_it(
            self, dashboard, monkeypatch, provider_routes):
        _seed_with_late_listers(dashboard, monkeypatch)
        base_html = dashboard.view("module_settings", refresh=True).text
        base_clustered = _step(base_html, "Clustered")
        assert _step(base_html, "Listed recently") == 0
        assert "Custom settings" not in _board(dashboard)
        before_provider = provider_routes.request_count
        before_yf = dashboard.yf_calls()

        # (b) save MIN_OBS = 200 -> 202 + a job -> the card funnel moves
        r = _post(dashboard, {"changes": {"MIN_OBS": 200}})
        assert r.status_code == 202, r.text
        job = dashboard.wait_for_job(r.json()["job_id"])
        assert job.ok, f"recompute failed: {job}\n{dashboard.logs()}"
        saved = dashboard.view("module_settings", refresh=True).text
        assert _step(saved, "Listed recently") == 4, (
            "the four late listers must move to 'Listed recently'")
        assert _step(saved, "Clustered") == base_clustered - 4
        assert 'value="200"' in saved and "custom (default 126)" in saved
        board = _board(dashboard)
        assert 'id="custom-badge"' in board and "Custom settings" in board, (
            "after saving MIN_OBS the Co-movement board must show the Custom settings badge")
        assert 'id="reset-settings"' in board, "the board must offer Reset to defaults"
        assert provider_routes.request_count == before_provider, "a save reached the network"
        assert dashboard.yf_calls() == before_yf

        # (c) reset -> back to the default numbers, no badge
        r = _post(dashboard, {"reset": True})
        assert r.status_code == 202, r.text
        assert dashboard.wait_for_job(r.json()["job_id"]).ok
        back = dashboard.view("module_settings", refresh=True).text
        assert _step(back, "Listed recently") == 0
        assert _step(back, "Clustered") == base_clustered
        assert "Custom settings" not in _board(dashboard)
        assert "Custom settings" not in back

    def test_an_invalid_value_is_a_400_with_the_rule_and_saves_nothing(
            self, dashboard, monkeypatch):
        _seed_with_late_listers(dashboard, monkeypatch)
        rows_before = dashboard.table_count("board_settings")
        r = _post(dashboard, {"changes": {"MIN_OBS": 10}})            # (d)
        assert r.status_code == 400, r.text
        body = r.json()
        assert body["ok"] is False
        assert any("MIN_OBS must be at least 60" in m for m in body["messages"]), body
        assert dashboard.table_count("board_settings") == rows_before == 0, (
            "a rejected change must not be stored")
        assert "Custom settings" not in _board(dashboard, refresh=1)
        assert "data-setting=\"MIN_OBS\"" in dashboard.view("module_settings", refresh=True).text
