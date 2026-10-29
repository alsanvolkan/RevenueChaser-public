"""SEC 13F board, end to end.

Since G9 (build plan PR 17) the board reads the ownership store (``ownership.duckdb`` in the market root, and the
spine for names): "/run/module_sec_13f" queues one ``ownership/serve`` once that dataset has had its first run and
renders from the store, with no SEC, OpenFIGI or UW call; Refresh renders the same rows with zero network. "Check for
new data set" (``/run/module_sec_13f-check``) and the rebuild job stay until G10 and are credential-free. The serving
files are written here the way ``ownership/serve`` and the spine build leave them.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import duckdb
import pytest

from RevenueChaser.module_sec_13f.tests.ownership_store import _row, _write_ownership, _write_spine
from RevenueChaser.rcdata.build.ownership.serve import filing_deadline

TOTALS = {
    "NVDA": {"2026-03-31": {"holders": 2100, "shares": 5_000_000, "value": 700_000_000},
             "2026-06-30": {"holders": 2250, "shares": 5_400_000, "value": 780_000_000}},
    "AMD": {"2026-03-31": {"holders": 1400, "shares": 3_100_000, "value": 400_000_000},
            "2026-06-30": {"holders": 1330, "shares": 2_900_000, "value": 370_000_000}},
}
NAMES = {"NVDA": ("NVIDIA Corp", "Technology"), "AMD": ("Advanced Micro Devices", "Technology")}


def _market(dashboard) -> Path:
    market = Path(dashboard.env["RC_MARKET_DIR"])
    market.mkdir(parents=True, exist_ok=True)
    return market


def _seed_store(dashboard, totals=TOTALS, shorts=()):
    """``ownership.duckdb`` and ``spine.duckdb`` as ``ownership/serve`` and the spine build serve them."""
    rows = []
    for ticker, periods in totals.items():
        for period, cell in periods.items():
            p = date.fromisoformat(period)
            rows.append(_row(ticker, p, float(cell["shares"]), filing_deadline(p), value=float(cell["value"]),
                             holders=cell["holders"]))
    market = _market(dashboard)
    _write_ownership(market, rows, [], shorts)
    _write_spine(market, {t: n for t, n in NAMES.items() if t in totals})


SEC_PAGE = "/data-research/sec-markets-data/form-13f-data-sets"


def _rows(dashboard) -> list:
    """The board's rows from the payload route (#429); the page itself is chrome and scripts now."""
    r = dashboard.get("/view/module_sec_13f/payload.json")
    assert r.status_code == 200, r.text
    return r.json()["rows"]


def _tickers(dashboard) -> set:
    return {row[0] for row in _rows(dashboard)}


@pytest.fixture
def sec_page(provider_routes):
    """The SEC data-set page, listing only sets that are too old to download."""
    provider_routes.route(SEC_PAGE, raw=(
        b'<a href="/files/structureddata/data/form-13f-data-sets/2024q4_form13f.zip">x</a>'),
        content_type="text/html")
    return provider_routes


@pytest.fixture
def sec_page_with_new_set(provider_routes):
    provider_routes.route(SEC_PAGE, raw=(
        b'<a href="/files/structureddata/data/form-13f-data-sets/'
        b'01jun2026-31aug2026_form13f.zip">x</a>'), content_type="text/html")
    return provider_routes


@pytest.fixture
def seeded_totals(dashboard):
    """The ownership store's 13F quarters, as ``ownership/serve`` serves them (the board renders what it holds)."""
    _seed_store(dashboard)
    return TOTALS


@pytest.fixture
def seeded_holdings(dashboard):
    """One ingested quarter (two holders of NVDA, one of AMD) plus the stored CUSIP map (#207: no name match).

    The zip ingest itself is not reachable from the dashboard without a download, so the
    ingested tables are seeded; what the rebuild promises is "derive from what is stored".

    Both tables are created at the v12 shape, DDL taken from ``rawstore.MANIFEST`` (#208): a
    v11-shaped table is refused by the derive, and a table without ``(accession, line_no)``
    only worked while the manifest's ``ALTER TABLE`` happened to run first. Named columns, so
    a seeded row carries the period and the long-equity marker the derive filters on.
    """
    import json

    from RevenueChaser.core import rawstore

    conn = duckdb.connect(str(dashboard.store_dir / "raw.duckdb"))
    try:
        for table in ("sec13f_submissions", "sec13f_holdings"):
            conn.execute(f'CREATE TABLE IF NOT EXISTS {table} ('
                         + ", ".join(rawstore.MANIFEST[table]["columns"]) + ")")
        conn.execute('CREATE TABLE IF NOT EXISTS sec13f_cusip_names ("cusip" TEXT PRIMARY KEY, '
                     '"name_norm" TEXT, "name_raw" TEXT, "count" INTEGER)')
        conn.execute('CREATE TABLE IF NOT EXISTS cusip_ticker '
                     '("cusip" TEXT PRIMARY KEY, "entry" JSON)')
        for acc, cik, shares, value in (("a1", "1000", 100, 1000), ("a2", "2000", 250, 2500)):
            conn.execute("INSERT INTO sec13f_submissions (accession, cik, period, filing_date, type) "
                         "VALUES (?, ?, '2026-06-30', '01-AUG-2026', '13F-HR')", [acc, cik])
            conn.execute("INSERT INTO sec13f_holdings (accession, line_no, cusip, cik, shares, value, "
                         "period, putcall, sshprnamttype) "
                         "VALUES (?, 1, '67066G104', ?, ?, ?, '2026-06-30', NULL, 'SH')",
                         [acc, cik, shares, value])
        conn.execute("INSERT INTO sec13f_cusip_names VALUES ('67066G104', 'NVIDIA', 'NVIDIA CORP', 2)")
        conn.execute("INSERT INTO cusip_ticker VALUES ('67066G104', ?)", [json.dumps("NVDA")])
    finally:
        conn.close()


class TestCredentials:
    """None of the 13F routes is gated on the UW token: the SEC data is the board."""

    def test_fetch_runs_when_the_token_is_rejected(
            self, dashboard, dead_uw_token, provider_routes, seeded_totals):
        before = provider_routes.request_count
        result = dashboard.run_job("/run/module_sec_13f")  # asserts it was accepted (HTTP 202)
        assert result.ok
        assert provider_routes.request_count == before, (
            f"the 13F Fetch reached the network: {[q.path for q in provider_routes.requests()[before:]]}")
        page = dashboard.view("module_sec_13f")
        assert page.status_code == 200
        assert "ownership/serve not queued" in page.text      # no first run of the derive yet: nothing queued
        assert "NVDA" in _tickers(dashboard)

    def test_check_needs_no_token_and_reports_a_new_set(
            self, dashboard, dead_uw_token, sec_page_with_new_set):
        r = dashboard.post_fetch("/run/module_sec_13f-check")
        assert r.status_code == 202
        result = dashboard.run_job("/run/module_sec_13f-check")
        assert result.ok
        assert "01jun2026-31aug2026_form13f.zip" in str(result)

    def test_refresh_renders_with_a_rejected_token(
            self, dashboard, dead_uw_token, seeded_totals, clear_cache):
        clear_cache()
        assert dashboard.view("module_sec_13f", refresh=True).status_code == 200
        assert "NVDA" in _tickers(dashboard)


class TestRenderAndRefresh:
    def test_fetch_renders_the_seeded_holdings(self, dashboard, sec_page, seeded_totals):
        assert dashboard.run_job("/run/module_sec_13f").ok
        assert dashboard.view("module_sec_13f").status_code == 200
        assert {"NVDA", "AMD"} <= _tickers(dashboard)

    def test_refresh_makes_no_network_calls(self, dashboard, provider_routes, sec_page,
                                            seeded_totals, clear_cache):
        assert dashboard.run_job("/run/module_sec_13f").ok
        clear_cache()
        before = provider_routes.request_count
        r = dashboard.view("module_sec_13f", refresh=True)
        assert r.status_code == 200
        assert provider_routes.request_count == before, (
            "refresh reached the network: "
            f"{[q.path for q in provider_routes.requests()[before:]]}")

    def test_refresh_still_shows_the_data(self, dashboard, sec_page, seeded_totals, clear_cache):
        assert dashboard.run_job("/run/module_sec_13f").ok
        clear_cache()
        assert dashboard.view("module_sec_13f", refresh=True).status_code == 200
        assert "NVDA" in _tickers(dashboard)

    def test_refresh_survives_a_restart(self, dashboard, sec_page, seeded_totals, clear_cache):
        assert dashboard.run_job("/run/module_sec_13f").ok
        clear_cache()
        dashboard.restart()
        assert dashboard.view("module_sec_13f", refresh=True).status_code == 200
        assert "NVDA" in _tickers(dashboard)


class TestRebuild:
    """``/run/module_sec_13f-rebuild`` re-derives the totals from the store: no network."""

    def test_rebuild_derives_the_totals_with_no_network_calls(
            self, dashboard, provider_routes, sec_page, seeded_holdings):
        """The old store's rebuild stays until G10; the board it re-renders reads the ownership store (G9)."""
        before = provider_routes.request_count
        result = dashboard.run_job("/run/module_sec_13f-rebuild")
        assert provider_routes.request_count == before, (
            "rebuild reached the network: "
            f"{[q.path for q in provider_routes.requests()[before:]]}")
        assert "Rebuilt 13F totals: 1 tickers" in str(result)
        assert dashboard.view("module_sec_13f").status_code == 200

    def test_rebuild_needs_no_token(self, dashboard, dead_uw_token, seeded_totals):
        assert dashboard.post_fetch("/run/module_sec_13f-rebuild").status_code == 202

    def test_nothing_ingested_leaves_the_board(self, dashboard, seeded_totals):
        assert "nothing to rebuild" in str(dashboard.run_job("/run/module_sec_13f-rebuild"))
        assert dashboard.view("module_sec_13f").status_code == 200
        assert "NVDA" in _tickers(dashboard)


class TestEmptyStore:
    def test_no_totals_renders_an_empty_state_not_a_crash(self, dashboard):
        r = dashboard.view("module_sec_13f")
        assert r.status_code == 200

    def test_a_fetch_before_the_first_fill_draws_the_empty_state(self, dashboard, provider_routes):
        """Before the 13F first fill the board says what fills it; the Fetch downloads nothing (avo, 29 Oct)."""
        before = provider_routes.request_count
        dashboard.run_job("/run/module_sec_13f")
        assert provider_routes.request_count == before
        assert "The ownership store holds no 13F quarter yet" in dashboard.view("module_sec_13f").text


@pytest.fixture
def seeded_inputs(dashboard, monkeypatch):
    """The snapshot inputs the board reads since #375 F2: NVDA's shares outstanding (UW) and insider % (yfinance) in
    ``ownership_snapshot``, its quarter-end closes in D01 and, since G9, FINRA's short interest in the ownership
    store. AMD has none of them."""
    from datetime import date

    import RevenueChaser.core.rawstore as rawstore
    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
    rawstore.ensure_all_tables()

    def own(source, shares, insider):
        return {"ticker": "NVDA", "session_date": date(2026, 10, 8), "source": source,
                "shares_outstanding": shares, "insider_pct": insider, "institutional_pct": None,
                "fetched_at": 0.0, "run_id": "e2e", "run_at": 0.0}

    rawstore.write_rows("ownership_snapshot", [own("uw_info", 24_000_000.0, None),
                                               own("yfinance", 30_000_000.0, 0.035)])
    rawstore.write_rows("price_bars_daily", [
        {"ticker": "NVDA", "session_date": date.fromisoformat(d), "open": c, "high": c, "low": c, "close": c,
         "adj_close": None, "volume": 1.0, "split_ratio": None, "adj_source": None, "source": "uw",
         "fetched_at": 0.0, "run_id": "e2e", "run_at": 0.0}
        for d, c in (("2026-03-31", 100.0), ("2026-06-30", 150.0))])
    from RevenueChaser.core import calendar
    from datetime import timedelta
    _seed_store(dashboard, shorts=[("NVDA", calendar.today_ny() - timedelta(days=14), 48_000_000, 1.6)])
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

    def test_render_includes_the_filter_bar(self, dashboard, sec_page, seeded_totals):
        assert dashboard.run_job("/run/module_sec_13f").ok
        html = dashboard.view("module_sec_13f").text
        assert '<section class="rcf-bar" id="rc-filters"></section>' in html

    def test_payload_carries_filter_fields(self, dashboard, sec_page, seeded_totals):
        assert dashboard.run_job("/run/module_sec_13f").ok
        payload = _payload(dashboard)
        assert "fields" in payload
        for t in ("NVDA", "AMD"):
            assert t in payload["fields"], payload["fields"].keys()
            f = payload["fields"][t]
            assert set(f) >= {"ticker", "name", "index", "market_cap", "gics", "theme_ids"}

    def test_off_spine_tickers_are_unclassified(self, dashboard, sec_page):
        _seed_store(dashboard, {**TOTALS, "FAKE999": {"2026-06-30": {"holders": 1, "shares": 100, "value": 1000}}})
        assert dashboard.run_job("/run/module_sec_13f").ok
        payload = _payload(dashboard)
        assert payload["fields"]["FAKE999"]["gics"][0] == "Unclassified"
        assert payload["fields"]["FAKE999"]["market_cap"] is None


class TestSnapshotInputs:
    """#375 F2: % Out, Insider% and Price Δ come from ``ownership_snapshot`` and D01, through one path for Refresh
    and Fetch; a Refresh makes no network call."""

    def test_refresh_reads_the_stored_inputs_with_no_network(self, seeded_inputs, provider_routes, clear_cache):
        clear_cache()
        before = provider_routes.request_count
        assert seeded_inputs.view("module_sec_13f", refresh=True).status_code == 200
        assert provider_routes.request_count == before, (
            f"refresh reached the network: {[q.path for q in provider_routes.requests()[before:]]}")
        nvda = _cells(seeded_inputs, "NVDA")
        assert nvda["pct_out"][:2] == ["22.5%", 22.5]           # 5.4M 13F shares of UW's 24M outstanding
        assert nvda["insider"][:2] == ["3.5%", pytest.approx(3.5)]   # the stored fraction 0.035, in %
        assert nvda["short"][:2] == ["48.00M (1.6d)", 48_000_000]     # FINRA's shares short, days to cover
        assert nvda["price_delta"][:2] == ["+50.0%", 50.0]
        assert "short" in _payload(seeded_inputs)["cols"]
        amd = _cells(seeded_inputs, "AMD")
        assert [amd[c][0] for c in ("pct_out", "insider", "short", "price_delta")] == ["—", "—", "—", "—"]

    def test_a_refresh_shows_the_numbers_the_fetch_showed(self, seeded_inputs, sec_page, clear_cache):
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
    def test_the_board_draws_its_rows_and_mounts_the_kit(self, dashboard, sec_page, seeded_totals, tmp_path):
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
            capture_output=True, text=True, encoding="utf-8", timeout=90)
        dom = out.stdout
        assert "<html" in dom.lower(), f"Chrome dumped no DOM (exit {out.returncode}): {out.stderr[-1500:]}"
        drawn = set(re.findall(r'<tr\b[^>]*\bdata-ticker="([^"]+)"', dom))
        assert {"NVDA", "AMD"} <= drawn, f"rows drawn: {sorted(drawn)}"
        assert "Loading rows" not in dom
        assert re.search(r'id="rc-filters"[^>]*>\s*<', dom), "the filter bar never mounted"
