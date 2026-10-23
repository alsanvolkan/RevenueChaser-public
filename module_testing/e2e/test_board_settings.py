"""Settings / Data health board, end to end.

The board is store-only: it renders on first load with no Fetch, and Refresh
must re-derive it from ``raw.duckdb`` / ``edgar.duckdb`` with zero network. The
temp store is seeded through ``rawstore`` (the value-chain pattern) so the
inventory has a known hole: two NULL market caps, one of which (MU) the
standalone ``/run/universe-stats`` job can fill and one of which (NOCAP) it must
never invent a value for.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx
import pytest

_APP = "module_settings"
_ARTIFACT = "settings_latest.html"


def _hole(n: int) -> str:
    """The exact universe hole cell: count + its ``null market cap`` label."""
    return f'warnv">{n}</span><span class="hlabel">null market cap'


@pytest.fixture
def seeded_settings(dashboard, monkeypatch):
    """Seed the dashboard's temp store with the universe + spine + one filing."""
    import RevenueChaser.core.rawstore as rawstore

    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
    rawstore.write_rows("equity_universe", [
        {"ticker": "AAPL", "market_cap": 3.0e12, "name": "Apple Inc."},
        {"ticker": "MU", "market_cap": None, "name": "Micron Technology"},
        {"ticker": "NOCAP", "market_cap": None, "name": "No Cap Co"},
    ])
    rawstore.write_rows("sec_company_tickers", [
        {"ticker": "AAPL", "cik": "0000320193", "name": "Apple Inc."},
    ])
    # The only universe name at/above the cap floor; the key feeds the corpus note.
    rawstore.write_rows("chain_meta", [{"key": "miss:AAPL", "value": json.dumps({
        "reason": "no_filing", "cik": "0000320193",
        "checked": datetime.now(timezone.utc).isoformat()})}])
    rawstore.write_rows("chain_filings", [
        {"ticker": "AAPL", "cik": "0000320193", "form": "10-K",
         "filed": "2026-02-01", "accession": "acc-aapl",
         "primary_doc": "http://x", "item_text": "Item 1. Business",
         "quality": "ok", "fetched_at": time.time()},
    ])
    return dashboard


def _caps() -> dict[str, float | None]:
    """Read stored caps from the temp store (DB_PATH is patched by the seed)."""
    import RevenueChaser.core.rawstore as rawstore

    return {r["ticker"]: r["market_cap"] for r in rawstore.query(
        "SELECT ticker, market_cap FROM equity_universe")}


class TestView:
    def test_the_tab_is_in_the_shell(self, dashboard):
        r = dashboard.get("/")
        assert r.status_code == 200
        assert "Settings / Data health" in r.text

    def test_inventory_shows_the_null_cap_hole(self, dashboard, seeded_settings):
        r = dashboard.view(_APP)
        assert r.status_code == 200
        assert _hole(2) in r.text

    def test_corpus_row_notes_the_recorded_miss(self, dashboard, seeded_settings):
        r = dashboard.view(_APP)
        assert r.status_code == 200
        assert ("1 recorded misses (1 no 10-K/20-F, 0 40-F; 0 due for retry)" in r.text), (
            "the corpus row must name the seeded miss: key")


class TestRefreshIsOfflineAndFaithful:
    @pytest.fixture
    def rendered(self, dashboard, seeded_settings):
        """A job-written artifact on disk (leaps pattern).

        Creating it via the job rather than a first-load ``/view`` matters on
        Windows: a view serves the file straight from disk and the server holds
        it open a beat, so the immediate ``clear_cache`` in a refresh test would
        lose a race. A job writes and closes it with no response handle involved.
        """
        result = dashboard.run_job("/run/universe-stats")
        assert result.ok, f"universe-stats failed: {result}\n{dashboard.logs()}"
        assert dashboard.artifact(_ARTIFACT).exists()
        return result

    def test_refresh_rebuilds_a_deleted_artifact(self, dashboard, rendered, clear_cache):
        clear_cache()
        assert not dashboard.artifact(_ARTIFACT).exists()
        r = dashboard.view(_APP, refresh=True)
        assert r.status_code == 200
        assert "Settings / Data health" in r.text
        # Serving 200 is not enough: Refresh must re-derive and re-write the
        # artifact. Without this, a refresh that answered from memory (or from a
        # stale handle) would still look green while leaving no file on disk.
        assert dashboard.artifact(_ARTIFACT).exists()

    def test_refresh_makes_no_network_calls(self, dashboard, rendered, provider_routes,
                                            clear_cache):
        clear_cache()
        before_provider = provider_routes.request_count
        before_yf = dashboard.yf_calls()
        r = dashboard.view(_APP, refresh=True)
        assert r.status_code == 200
        assert provider_routes.request_count == before_provider, (
            "refresh reached the network: "
            f"{[q.path for q in provider_routes.requests()[before_provider:]]}")
        assert dashboard.yf_calls() == before_yf, (
            "refresh reached yfinance: "
            f"{dashboard.yf_calls()[len(before_yf):]}")

    def test_refresh_survives_a_restart(self, dashboard, rendered, clear_cache):
        clear_cache()
        dashboard.restart()
        r = dashboard.view(_APP, refresh=True)
        assert r.status_code == 200
        assert "Settings / Data health" in r.text


class TestStatsFetch:
    def test_fetch_caps_fills_the_null_and_reports_it(self, dashboard, seeded_settings):
        before_rows = dashboard.table_count("equity_universe")
        result = dashboard.run_job("/run/universe-stats")
        assert result.ok, f"universe-stats failed: {result}\n{dashboard.logs()}"
        assert "1 caps filled" in (result.message or ""), result.message
        caps = _caps()
        assert caps["MU"] == pytest.approx(1.232e11, rel=1e-9)
        assert caps["NOCAP"] is None, "never invent a cap"
        assert dashboard.table_count("equity_universe") == before_rows, (
            "the stats fetch must not add or remove membership rows")

    def test_the_fetch_never_calls_the_screener(self, dashboard, seeded_settings):
        result = dashboard.run_job("/run/universe-stats")
        assert result.ok, f"universe-stats failed: {result}\n{dashboard.logs()}"
        calls = dashboard.yf_calls()
        assert "fast_info:MU" in calls, calls
        assert not any(c.startswith("screen") for c in calls), (
            f"the standalone stats job must not call the screener: {calls}")

    def test_after_fetch_the_board_shows_one_hole(self, dashboard, seeded_settings):
        result = dashboard.run_job("/run/universe-stats")
        assert result.ok, f"universe-stats failed: {result}\n{dashboard.logs()}"
        html = dashboard.view(_APP).text
        assert _hole(1) in html


class TestDrillApi:
    def test_drill_api_is_offline(self, dashboard, seeded_settings, provider_routes):
        before_provider = provider_routes.request_count
        before_yf = dashboard.yf_calls()
        r = dashboard.get("/api/settings/ticker/AAPL")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["ticker"] == "AAPL"
        assert body["name"] == "Apple Inc."
        assert body["filing"]["form"] == "10-K"
        assert provider_routes.request_count == before_provider, (
            "the drill reached the network: "
            f"{[q.path for q in provider_routes.requests()[before_provider:]]}")
        assert dashboard.yf_calls() == before_yf, (
            "the drill reached yfinance: "
            f"{dashboard.yf_calls()[len(before_yf):]}")

    def test_drill_api_unknown_is_404(self, dashboard, seeded_settings):
        r = dashboard.get("/api/settings/ticker/ZZZZ")
        assert r.status_code == 404
        assert r.json() == {"ok": False}


class TestEmptyStore:
    def test_a_board_with_no_data_renders_an_empty_state(self, dashboard):
        r = dashboard.view(_APP)
        assert r.status_code == 200
        assert "Settings / Data health" in r.text


def _verdicts(html: str, bodies: list) -> list:
    """Run the board's own ``settingsJobVerdict`` (under node) on real API bodies."""
    import json
    import re
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    m = re.search(r"function settingsJobVerdict\(.*?\n}\n", html, re.S)
    assert m, "the served Settings board has no settingsJobVerdict"
    script = (m.group(0) + "\nvar b = " + json.dumps(bodies)
              + ";\nconsole.log(JSON.stringify(b.map(settingsJobVerdict)));\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


class TestFetchButtonPoller:
    """The Fetch button must never say "done" for a job that has not run."""

    def test_verdict_on_real_job_bodies(self, dashboard, seeded_settings):
        html = dashboard.view(_APP).text

        # (a) an unknown job id: the server answers 404 {"ok": false}.
        unknown = dashboard.get("/api/job/no-such-job")
        assert unknown.status_code == 404
        unknown_body = unknown.json()

        # (b) one real job, caught while it is still live. Submitting the same writer again used to queue a
        # second run; since #26 PL6-a it is refused 409 and the body carries the id of the run already going,
        # which is a stronger way to catch a live job: the refusal itself proves the first has not finished.
        first = dashboard.post_fetch("/run/universe-stats")
        assert first.status_code == 202, first.text
        job_id = first.json()["job_id"]

        again = dashboard.post_fetch("/run/universe-stats")
        assert again.status_code == 409, again.text
        assert again.json()["job_id"] == job_id, again.text

        live = dashboard.get(f"/api/job/{job_id}").json()
        live_status = live["job"]["status"]

        # (c) the same job once finished.
        dashboard.wait_for_job(job_id)
        finished = dashboard.get(f"/api/job/{job_id}").json()
        assert finished["job"]["status"] == "done", finished

        got = _verdicts(html, [unknown_body, live, finished])
        assert got[0]["state"] == "error", (
            f"an unknown job must not read as done: {got[0]}")
        assert got[2]["state"] == "done", got[2]
        assert live_status in ("queued", "running"), (
            f"could not catch an unfinished job (status {live_status!r}); "
            "the poll case is untested")
        assert got[1]["state"] == "poll", (
            f"an unfinished job ({live_status}) must keep polling, not finish: {got[1]}")


# ── Board inputs: the Settings card must tell the same story as the board ────
_STEP_LEGACY = {"scanned": "scanned_total", "below_floor": "below_floor",
                "no_market_cap": "no_market_cap",
                # R4 (#87): the security-type rows, then the note (shown, never counted)
                "funds_etfs": "funds_etfs", "spacs": "spacs",
                "other_securities": "other_securities", "unknown_type": "unknown_type",
                "no_prices": "no_prices",
                "listed_recently": "listed_recently", "missing_bars": "missing_bars",
                "flat": "flat",
                "clustered": "clustered"}


def _card_rows(html: str) -> list[tuple[str, str, int]]:
    """The Co-movement card's funnel rows: ``(kind, label, count)``.

    ``kind`` is ``"note"`` for a note row (a ⚠ marker, a Settings link, a positive
    count) and ``"step"`` for every counted row (drops are shown as ``−N``).
    """
    import html as _html
    import re

    m = re.search(r'<div class="bcard" id="board-module_comovement">(.*?)</div></div>'
                  r'(?=<div class="bcard"|\s*</div>|\s*<script)', html, re.S)
    assert m, "the Settings board has no Co-movement card"
    rows = []
    for cls, inner in re.findall(r'<div class="(frow2[^"]*)">(.*?)</div>', m.group(1), re.S):
        label = re.match(r"<span>([^<]*)", inner).group(1)
        num = re.search(r'<span class="num">[−]?([\d,]+)</span>', inner).group(1)
        kind = "note" if "note" in cls.split() else "step"
        rows.append((kind, _html.unescape(label.replace("⚠", "")).strip(),
                     int(num.replace(",", ""))))
    return rows


def _card_steps(html: str) -> dict[str, int]:
    """Counted step label -> count shown on the Co-movement card."""
    return {label: n for kind, label, n in _card_rows(html) if kind == "step"}


def _card_notes(html: str) -> dict[str, int]:
    """Note label -> count shown on the Co-movement card."""
    return {label: n for kind, label, n in _card_rows(html) if kind == "note"}


_FUND = "FUNDX"


class TestBoardInputs:
    def _seed_and_fetch(self, dashboard):
        import numpy as np
        import RevenueChaser.core.rawstore as rawstore
        from RevenueChaser.module_testing.e2e import test_board_comovement as cm

        ret = cm._returns()
        tickers = list(ret.columns)
        rawstore.replace_all("equity_universe", [{
            "ticker": t, "market_cap": 1.0e10, "sector": "Technology", "price": 10.0,
            "name": t + " Inc", "industry": cm._INDUSTRY.get(t, ""), "quote": {"symbol": t},
        } for t in tickers + [_FUND]])
        # Identity R4 (#87): a closed-end fund at the floor (the type step is non-zero)
        # and one operating name with no master row (the unknown-type note is non-zero).
        rawstore.write_rows("security_master", [
            {"ticker": _FUND, "security_type": "cef"}] + [
            {"ticker": t, "security_type": "common"} for t in tickers[1:]])
        now = time.time()
        rows = []
        for frame in (ret, cm._benchmarks(ret.index)):
            for t in frame.columns:
                price = 100 * np.exp(frame[t].cumsum())
                for ts, p in price.items():
                    rows.append({"ticker": t, "ts": ts.isoformat(), "open": p,
                                 "high": p, "low": p, "close": float(p),
                                 "volume": 1.0, "cached_at": now})
        from RevenueChaser.module_comovement.tests.d01_bars import write_bars
        write_bars(rows)
        result = dashboard.run_job("/run/module_comovement")
        assert result.ok, f"comovement fetch failed: {result}\n{dashboard.logs()}"

    def test_the_card_matches_the_comovement_payload(self, dashboard, seeded_settings):
        import json

        self._seed_and_fetch(dashboard)
        payload = json.loads(dashboard.artifact(
            "comovement/comovement_latest.json").read_text(encoding="utf-8"))
        html = dashboard.view(_APP, refresh=True).text
        assert "Board inputs" in html
        assert 'id="board-module_comovement"' in html
        assert "Co-movement has not been rendered yet" not in html
        assert "Rendered before funnel reporting" not in html

        shown, notes = _card_steps(html), _card_notes(html)
        f = payload["funnel"]
        steps = payload["funnel_steps"]
        counted = [s for s in steps if s["kind"] != "note"]
        noted = [s for s in steps if s["kind"] == "note"]
        assert [s["key"] for s in steps] == list(_STEP_LEGACY), (
            f"funnel_steps keys drifted: {[s['key'] for s in steps]}")
        # the seed makes the type step and the note non-zero, so they are really compared
        assert f["funds_etfs"] == 1 and f["unknown_type"] == 1, f
        assert [s["key"] for s in noted] == ["unknown_type"]
        assert len(shown) == len(counted), (
            f"card shows {len(shown)} funnel rows, payload has {len(counted)}: {shown}")
        assert len(notes) == len(noted), f"card notes {notes}, payload notes {noted}"
        for s in counted:
            assert s["count"] == f[_STEP_LEGACY[s["key"]]], (
                f"step {s['key']} = {s['count']} but the board's own funnel says "
                f"{f[_STEP_LEGACY[s['key']]]}")
            assert shown.get(s["label"]) == s["count"], (
                f"card row {s['label']!r} shows {shown.get(s['label'])}, "
                f"payload step says {s['count']}")
        for s in noted:
            assert s["count"] == f[_STEP_LEGACY[s["key"]]]
            assert notes.get(s["label"]) == s["count"], (
                f"card note {s['label']!r} shows {notes.get(s['label'])}, "
                f"payload note says {s['count']}")
            assert s["label"] not in shown, "a note must not be shown as a counted row"
        # the headline: the card's clustered count is the board's clustered count
        assert shown["Clustered"] == payload["eligible"] == f["clustered"]
        board = dashboard.view("module_comovement").text
        assert f"clustered <b class=\"num\">{payload['eligible']}</b>" in board
        # the board says the same: the type row, the note, and no store-writing button
        assert "Funds / ETFs" in board and "Unknown security type — kept" in board
        assert "operating companies" in board
        assert "/run/identity" not in board

    def test_a_settings_refresh_stays_offline_with_the_card(
            self, dashboard, seeded_settings, provider_routes):
        self._seed_and_fetch(dashboard)
        before_provider = provider_routes.request_count
        before_yf = dashboard.yf_calls()
        r = dashboard.view(_APP, refresh=True)
        assert r.status_code == 200 and "Board inputs" in r.text
        assert provider_routes.request_count == before_provider, (
            "settings refresh reached the network: "
            f"{[q.path for q in provider_routes.requests()[before_provider:]]}")
        assert dashboard.yf_calls() == before_yf

    def test_before_the_board_is_rendered_the_card_says_so(self, dashboard, seeded_settings):
        html = dashboard.view(_APP, refresh=True).text
        assert "Co-movement has not been rendered yet. Open or Refresh that tab first." in html


_PANEL = {"X-RC-Panel": "automations"}


class TestAutomationsPanel:
    """#130: the Automations panel, against the fake scheduler the harness installs."""

    def test_rows_render_on_the_board(self, dashboard, seeded_settings):
        html = dashboard.view(_APP).text
        assert 'id="auto-body"' in html
        for name in ("RC-Store-Backup", "RC-Relatedness-OffPeak", "RC-ValueChain-Sync", "RC-Daily-Bars",
                     "RC-Screeners-Daily", "RC-Forecast-Outcomes", "RC-Forecast-Baselines", "RC-Index-Lists",
                     "RC-Earnings-Inputs", "RC-Open-Issues-Check"):
            assert name in html

    def test_the_route_reports_every_row_not_installed(self, dashboard, seeded_settings):
        d = dashboard.get("/api/automations").json()
        assert d["ok"] and d["available"]
        assert [r["state"] for r in d["rows"]] == ["not_installed"] * 10

    def test_install_goes_to_the_fake_scheduler_not_the_real_one(self, dashboard, seeded_settings):
        r = dashboard.client.post("/api/automations/RC-Store-Backup/install", headers=_PANEL)
        assert r.status_code == 200, r.text
        assert r.json()["row"]["state"] == "enabled"
        rows = {x["name"]: x["state"] for x in dashboard.get("/api/automations").json()["rows"]}
        assert rows == {"RC-Store-Backup": "enabled", "RC-Relatedness-OffPeak": "not_installed",
                        "RC-ValueChain-Sync": "not_installed", "RC-Daily-Bars": "not_installed",
                        "RC-Screeners-Daily": "not_installed", "RC-Forecast-Outcomes": "not_installed",
                        "RC-Forecast-Baselines": "not_installed", "RC-Index-Lists": "not_installed", "RC-Earnings-Inputs": "not_installed",
                        "RC-Open-Issues-Check": "not_installed"}
        r = dashboard.client.post("/api/automations/RC-Store-Backup/remove", headers=_PANEL)
        assert r.json()["row"]["state"] == "not_installed"

    def test_an_unknown_task_is_refused(self, dashboard, seeded_settings):
        r = dashboard.client.post("/api/automations/Windows%20Defrag/remove", headers=_PANEL)
        assert r.status_code == 404

    def test_a_post_without_the_panel_header_is_refused(self, dashboard, seeded_settings):
        # A bare client: dashboard.client sends X-RC-Panel by default (#231), which would
        # make this a different test (the automations route wants its own value).
        r = httpx.post(dashboard.url + "/api/automations/RC-Store-Backup/install")
        assert r.status_code == 403
        states = [x["state"] for x in dashboard.get("/api/automations").json()["rows"]]
        assert "enabled" not in states

    def test_the_backup_row_reports_both_folder_sizes_from_the_log(self, dashboard, seeded_settings):
        """#309 part 3: the route reads the sizes line the backup job logs; above the local default (20 GB) it warns."""
        log = dashboard.cache_dir / "logs" / "RC-Store-Backup.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("backup sizes 2026-10-16T04:02:00+00:00: local 25000000000 bytes, "
                       "off-machine 7676019838 bytes\n", encoding="utf-8")
        try:
            rows = {x["name"]: x for x in dashboard.get("/api/automations").json()["rows"]}
            size = rows["RC-Store-Backup"]["backup_size"]
            assert size["kind"] == "warn"
            assert size["text"].startswith("local 25.00 GB, above 20 GB (RAW_STORE_BACKUP_WARN_GB), "
                                           "off-machine 7.68 GB")
            assert rows["RC-Daily-Bars"]["backup_size"] is None
        finally:
            log.unlink()

    def test_the_backups_card_is_on_the_page(self, dashboard, seeded_settings):
        """#309: the Settings page shows the Backups card (its values come from the host's backup folder records,
        "unknown" without them, so only the card and its rows are asserted here)."""
        html = dashboard.view(_APP, refresh=True).text
        assert 'id="backups"' in html and 'id="backup-local"' in html and 'id="backup-scan"' in html
        card = html[html.index('id="backups"'):].split("</section>")[0]
        assert "Off-machine checkpoints" in card

    def test_board_and_route_make_no_network_calls(self, dashboard, seeded_settings,
                                                   provider_routes, clear_cache):
        dashboard.run_job("/run/universe-stats")
        clear_cache()
        before_provider = provider_routes.request_count
        before_yf = dashboard.yf_calls()
        assert dashboard.view(_APP, refresh=True).status_code == 200
        assert dashboard.get("/api/automations").status_code == 200
        assert provider_routes.request_count == before_provider
        assert dashboard.yf_calls() == before_yf
