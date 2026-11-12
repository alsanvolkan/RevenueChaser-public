"""SEC 13F board, end to end.

Since G9 the numbers are the builder's: ``ownership.duckdb`` holds ``sec13f_ticker_period`` and
"/run/module_sec_13f" refreshes only what the dashboard owns — the UW names/sectors and shares outstanding — then
re-renders. It needs no Unusual Whales key (#206): the names/sectors step is best-effort and a rejected token only
leaves a note on the board. Fetch and Refresh make no SEC call at all, so there is no data-set page to mock: both
render whatever the ownership store holds.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import duckdb
import pytest

from . import harness

TOTALS = {
    "NVDA": {"2026-03-31": {"holders": 2100, "shares": 5_000_000, "value": 700_000_000},
             "2026-06-30": {"holders": 2250, "shares": 5_400_000, "value": 780_000_000}},
    "AMD": {"2026-03-31": {"holders": 1400, "shares": 3_100_000, "value": 400_000_000},
            "2026-06-30": {"holders": 1330, "shares": 2_900_000, "value": 370_000_000}},
}


def _rows(dashboard) -> list:
    """The board's rows from the payload route (#429); the page itself is chrome and scripts now."""
    r = dashboard.get("/view/module_sec_13f/payload.json")
    assert r.status_code == 200, r.text
    return r.json()["rows"]


def _tickers(dashboard) -> set:
    return {row[0] for row in _rows(dashboard)}


@pytest.fixture
def seeded_totals(serve_13f_totals):
    return serve_13f_totals(TOTALS)


def _served_shares(dashboard, ticker: str, shares: float) -> None:
    """The cover-page count in the dashboard's own served prices file: where "% Out" divides since #646 PR 1.

    The ``ownership_snapshot`` rows stay beside it - they still carry insider %, and keeping them proves the
    board reads this number and not that one.
    """
    from datetime import date

    import duckdb
    path = Path(dashboard.env["RC_MARKET_DIR"]) / "prices.duckdb"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS security_symbol (security_id VARCHAR, symbol VARCHAR, "
                    "valid_from DATE, valid_to DATE, source VARCHAR)")
        con.execute("CREATE TABLE IF NOT EXISTS shares_class (security_id VARCHAR, measure VARCHAR, as_of DATE, "
                    "value DOUBLE, symbol VARCHAR, cik VARCHAR, class_member VARCHAR, source VARCHAR)")
        con.execute("DELETE FROM security_symbol WHERE symbol = ?", [ticker])
        con.execute("DELETE FROM shares_class WHERE symbol = ?", [ticker])
        con.execute("INSERT INTO security_symbol (security_id, symbol, valid_from, source) VALUES (?, ?, ?, ?)",
                    [f"sym:{ticker}", ticker, date(2026, 1, 2), "alpaca/bars"])
        con.execute("INSERT INTO shares_class (security_id, measure, as_of, value, symbol, source) "
                    "VALUES (?, 'outstanding', ?, ?, ?, 'sec/cover_shares')",
                    [f"sym:{ticker}", date(2026, 1, 2), shares, ticker])
    finally:
        con.close()


@pytest.fixture
def seeded_inputs(dashboard, seeded_totals, monkeypatch):
    """The snapshot inputs the board reads since #375 F2: NVDA's shares outstanding (UW) and insider % (yfinance) in
    ``ownership_snapshot``, its quarter-end closes in D01 and its Short% in D10. AMD has none of them."""
    from datetime import date

    import RevenueChaser.core.rawstore as rawstore
    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
    monkeypatch.setattr("RevenueChaser.core.config.MARKET_DIR", dashboard.env["RC_MARKET_DIR"])  # served prices.duckdb
    rawstore.ensure_all_tables()

    def own(source, shares, insider):
        return {"ticker": "NVDA", "session_date": date(2026, 10, 8), "source": source,
                "shares_outstanding": shares, "insider_pct": insider, "institutional_pct": None,
                "fetched_at": 0.0, "run_id": "e2e", "run_at": 0.0}

    rawstore.write_rows("ownership_snapshot", [own("uw_info", 24_000_000.0, None),
                                               own("yfinance", 30_000_000.0, 0.035)])
    _served_shares(dashboard, "NVDA", 24_000_000.0)   # the denominator's home since #646 PR 1
    from RevenueChaser.core.tests.d01_seed import write_d01
    write_d01([
        {"ticker": "NVDA", "session_date": date.fromisoformat(d), "open": c, "high": c, "low": c, "close": c,
         "adj_close": None, "volume": 1.0, "split_ratio": None, "adj_source": None, "source": "uw",
         "fetched_at": 0.0, "run_id": "e2e", "run_at": 0.0}
        for d, c in (("2026-03-31", 100.0), ("2026-06-30", 150.0))])
    rawstore.write_rows("short_interest", [
        {"ticker": "NVDA", "session_date": date(2026, 10, 15), "market_date": None,
         "short_interest": None, "si_float": 0.048, "total_float": None, "days_to_cover": None,
         "fee_rate": None, "rebate_rate": None, "short_shares_available": None, "payload": {},
         "source": "uw_short_screener", "fetched_at": 0.0, "run_id": "e2e", "run_at": 0.0}])
    return dashboard


def _payload(dashboard) -> dict:
    r = dashboard.get("/view/module_sec_13f/payload.json")
    assert r.status_code == 200, r.text
    return r.json()


def _cells(dashboard, ticker) -> dict:
    payload = _payload(dashboard)
    return dict(zip(payload["cols"], next(r for r in payload["rows"] if r[0] == ticker)[3]))


class TestCredentials:
    """Neither 13F route is gated on the UW token: the served store is the board."""

    def test_fetch_runs_when_the_token_is_rejected(
            self, dashboard, dead_uw_token, seeded_totals):
        result = dashboard.run_job("/run/module_sec_13f")  # asserts it was accepted (HTTP 202)
        assert "not refreshed" in str(result)  # the names step said so, the job did not fail
        assert dashboard.view("module_sec_13f").status_code == 200
        assert "NVDA" in _tickers(dashboard)

    def test_refresh_renders_with_a_rejected_token(
            self, dashboard, dead_uw_token, seeded_totals, clear_cache):
        clear_cache()
        assert dashboard.view("module_sec_13f", refresh=True).status_code == 200
        assert "NVDA" in _tickers(dashboard)


class TestRenderAndRefresh:
    def test_fetch_renders_the_served_totals(self, dashboard, seeded_totals):
        assert dashboard.run_job("/run/module_sec_13f").ok
        assert dashboard.view("module_sec_13f").status_code == 200
        assert {"NVDA", "AMD"} <= _tickers(dashboard)

    def test_refresh_makes_no_network_calls(self, dashboard, provider_routes,
                                            seeded_totals, clear_cache):
        assert dashboard.run_job("/run/module_sec_13f").ok
        clear_cache()
        before = provider_routes.request_count
        r = dashboard.view("module_sec_13f", refresh=True)
        assert r.status_code == 200
        assert provider_routes.request_count == before, (
            "refresh reached the network: "
            f"{[q.path for q in provider_routes.requests()[before:]]}")

    def test_refresh_still_shows_the_data(self, dashboard, seeded_totals, clear_cache):
        assert dashboard.run_job("/run/module_sec_13f").ok
        clear_cache()
        assert dashboard.view("module_sec_13f", refresh=True).status_code == 200
        assert "NVDA" in _tickers(dashboard)

    def test_refresh_survives_a_restart(self, dashboard, seeded_totals, clear_cache):
        assert dashboard.run_job("/run/module_sec_13f").ok
        clear_cache()
        dashboard.restart()
        assert dashboard.view("module_sec_13f", refresh=True).status_code == 200
        assert "NVDA" in _tickers(dashboard)


class TestEmptyStore:
    def test_no_totals_renders_an_empty_state_not_a_crash(self, dashboard):
        r = dashboard.view("module_sec_13f")
        assert r.status_code == 200


@pytest.fixture
def seeded_inputs(dashboard, seeded_totals, monkeypatch):
    """The snapshot inputs the board reads since #375 F2: NVDA's shares outstanding (UW) and insider % (yfinance) in
    ``ownership_snapshot``, its quarter-end closes in D01 and its Short% in D10. AMD has none of them."""
    from datetime import date

    import RevenueChaser.core.rawstore as rawstore
    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
    monkeypatch.setattr("RevenueChaser.core.config.MARKET_DIR", dashboard.env["RC_MARKET_DIR"])  # served prices.duckdb
    rawstore.ensure_all_tables()

    def own(source, shares, insider):
        return {"ticker": "NVDA", "session_date": date(2026, 10, 8), "source": source,
                "shares_outstanding": shares, "insider_pct": insider, "institutional_pct": None,
                "fetched_at": 0.0, "run_id": "e2e", "run_at": 0.0}

    rawstore.write_rows("ownership_snapshot", [own("uw_info", 24_000_000.0, None),
                                               own("yfinance", 30_000_000.0, 0.035)])
    _served_shares(dashboard, "NVDA", 24_000_000.0)   # the denominator's home since #646 PR 1
    from RevenueChaser.core.tests.d01_seed import write_d01
    write_d01([
        {"ticker": "NVDA", "session_date": date.fromisoformat(d), "open": c, "high": c, "low": c, "close": c,
         "adj_close": None, "volume": 1.0, "split_ratio": None, "adj_source": None, "source": "uw",
         "fetched_at": 0.0, "run_id": "e2e", "run_at": 0.0}
        for d, c in (("2026-03-31", 100.0), ("2026-06-30", 150.0))])
    rawstore.write_rows("short_interest", [
        {"ticker": "NVDA", "session_date": date(2026, 10, 15), "market_date": None,
         "short_interest": None, "si_float": 0.048, "total_float": None, "days_to_cover": None,
         "fee_rate": None, "rebate_rate": None, "short_shares_available": None, "payload": {},
         "source": "uw_short_screener", "fetched_at": 0.0, "run_id": "e2e", "run_at": 0.0}])
    return dashboard


def _payload(dashboard) -> dict:
    r = dashboard.get("/view/module_sec_13f/payload.json")
    assert r.status_code == 200, r.text
    return r.json()


def _cells(dashboard, ticker) -> dict:
    payload = _payload(dashboard)
    return dict(zip(payload["cols"], next(r for r in payload["rows"] if r[0] == ticker)[3]))


class TestFilterKit:
    """#375 KF: the shared filter kit mounts on the 13F board and its rows/metadata ride the payload route."""

    def test_render_includes_the_filter_bar(self, dashboard, seeded_totals):
        assert dashboard.run_job("/run/module_sec_13f").ok
        html = dashboard.view("module_sec_13f").text
        assert '<section class="rcf-bar" id="rc-filters"></section>' in html

    def test_payload_carries_filter_fields(self, dashboard, seeded_totals):
        assert dashboard.run_job("/run/module_sec_13f").ok
        payload = _payload(dashboard)
        assert "fields" in payload
        for t in ("NVDA", "AMD"):
            assert t in payload["fields"], payload["fields"].keys()
            f = payload["fields"][t]
            assert set(f) >= {"ticker", "name", "index", "market_cap", "gics", "theme_ids"}

    def test_off_spine_tickers_are_unclassified(self, dashboard, serve_13f_totals):
        serve_13f_totals({**TOTALS, "FAKE999": {"2026-06-30": {"holders": 1, "shares": 100, "value": 1000}}})
        assert dashboard.run_job("/run/module_sec_13f").ok
        payload = _payload(dashboard)
        assert payload["fields"]["FAKE999"]["gics"][0] == "Unclassified"
        assert payload["fields"]["FAKE999"]["market_cap"] is None


class TestSnapshotInputs:
    """#375 F2: % Out and Price Δ come from ``ownership_snapshot`` and D01, through one path for Refresh and Fetch; a
    Refresh makes no network call. Insider% and Short% are off the board since #732 (owner, 12 Nov 2026)."""

    def test_refresh_reads_the_stored_inputs_with_no_network(self, seeded_inputs, provider_routes, clear_cache):
        clear_cache()
        before = provider_routes.request_count
        assert seeded_inputs.view("module_sec_13f", refresh=True).status_code == 200
        assert provider_routes.request_count == before, (
            f"refresh reached the network: {[q.path for q in provider_routes.requests()[before:]]}")
        nvda = _cells(seeded_inputs, "NVDA")
        assert nvda["pct_out"][:2] == ["22.5%", 22.5]           # 5.4M 13F shares of UW's 24M outstanding
        assert nvda["price_delta"][:2] == ["+50.0%", 50.0]
        cols = _payload(seeded_inputs)["cols"]
        assert "short" not in cols and "insider" not in cols
        amd = _cells(seeded_inputs, "AMD")
        assert [amd[c][0] for c in ("pct_out", "price_delta")] == ["—", "—"]

    def test_a_refresh_shows_the_numbers_the_fetch_showed(self, seeded_inputs, clear_cache):
        assert seeded_inputs.run_job("/run/module_sec_13f").ok
        fetched = _payload(seeded_inputs)
        assert _cells(seeded_inputs, "NVDA")["price_delta"][1] == 50.0
        clear_cache()
        assert seeded_inputs.view("module_sec_13f", refresh=True).status_code == 200
        assert _payload(seeded_inputs) == fetched


# ── R7 review: the rows load in a real browser ───────────────────────────────────────────────────────────────────
# The payload route and the served markup above can both be right while the page itself shows nothing: the filter
# kit's script once rode outside any <script> tag, so ``RCFilters`` was undefined, ``build`` threw and no row was
# drawn. Headless Chrome (as in test_board_leaps.py) loads the served board and its payload from the dashboard.
def _chrome_path() -> str | None:
    override = os.environ.get("RC_CHROME")
    if override:
        return override if Path(override).exists() or shutil.which(override) else None
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    return None


class TestRowsInTheBrowser:
    def test_the_board_draws_its_rows_and_mounts_the_kit(self, dashboard, seeded_totals, tmp_path):
        chrome = _chrome_path()
        if chrome is None:
            msg = "no Chrome/Chromium for the 13F browser check: set RC_CHROME"
            if os.environ.get("CI", "").lower() == "true":
                pytest.fail(msg)
            pytest.skip(msg)
        assert dashboard.run_job("/run/module_sec_13f").ok
        out = subprocess.run(
            [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-extensions",
             f"--user-data-dir={tmp_path / 'profile'}",
             "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1",
             "--virtual-time-budget=5000", "--dump-dom", dashboard.url + "/view/module_sec_13f"],
            capture_output=True, text=True, encoding="utf-8", timeout=harness.CHROME_TIMEOUT_S)
        dom = out.stdout
        assert "<html" in dom.lower(), f"Chrome dumped no DOM (exit {out.returncode}): {out.stderr[-1500:]}"
        drawn = set(re.findall(r'<tr\b[^>]*\bdata-ticker="([^"]+)"', dom))
        assert {"NVDA", "AMD"} <= drawn, f"rows drawn: {sorted(drawn)}"
        assert "Loading rows" not in dom
        assert re.search(r'id="rc-filters"[^>]*>\s*<', dom), "the filter bar never mounted"
