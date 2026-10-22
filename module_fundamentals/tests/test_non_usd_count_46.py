"""#46, #64 checkbox 6: the Fundamentals scan counts the names that report in a currency other than USD.

A filer that tags its money facts in EUR yields no USD series, so ``_compute_row`` used to drop it as if EDGAR held
nothing; nothing on the page said how many names left for that reason. The count now comes from the ``meta`` the
extraction already stores (``non_usd_units``), a store read with no SEC request, and it is a funnel ``drop`` so it
reconciles with the rest: cards + drops == the scanned universe.

The measure is the unit on the fact, never the accounting standard: a non-USD filer is usually an IFRS filer, but an
IFRS filer may still tag USD, so the board says exactly what it counted.

Hand-built store rows and a synthetic history: no real store, no network.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import funnel, rawstore, screen_common
from RevenueChaser.core.tests.screen_parity import _OfflineTicker, _bars, seed_d01
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard

NOW = datetime(2026, 10, 14, 21, 45, tzinfo=timezone.utc)      # the 14 Oct session is complete
SESSION = date(2026, 10, 14)

ANNUAL = [{"revenue": 100.0 * (i + 1), "gross_profit": 50.0, "operating_cash_flow": 20.0, "capex": 5.0,
           "total_debt": 10.0, "cash": 30.0, "total_assets": 200.0, "total_equity": 100.0, "net_income": 10.0}
          for i in range(3)]

_USD_ROW = {"source": "sec_edgar", "entity_name": "AAA", "is_us": True, "available": True, "extract_version": 2,
            "non_usd_units": []}


def _edgar(ticker: str, annual: list[dict], **meta) -> None:
    """One stored ``edgar_financials`` row, as the nightly extraction writes it."""
    rawstore.write_rows("edgar_financials", [{
        "ticker": ticker, "annual": annual, "quarterly": [],
        "meta": {**_USD_ROW, "entity_name": ticker, **meta}, "fetched_at": time.time()}])


@pytest.fixture
def scan_env(rawstore_tmp_db, monkeypatch):
    """Four scanned names with a full D01 history; EDGAR comes from the store, never the network."""
    rows = [{"ticker": t, "market_cap": 1e9, "price": 50.0, "name": f"{t} Inc", "sector": "Technology"}
            for t in ("AAA", "BBB", "CCC", "DDD")]
    for t in ("AAA", "BBB", "CCC", "DDD", "SPY"):
        seed_d01(t, _bars("up", 0.001, 300))
    monkeypatch.setattr(cal, "_now", lambda: NOW)
    monkeypatch.setattr(fp, "fetch_universe_stats", lambda **kw: (rows, {"out_of_universe": 0}))
    monkeypatch.setattr(fp, "prefilter", lambda r: screen_common.Prefiltered(list(r)))
    monkeypatch.setattr(fp, "load_ticker_info", lambda *a, **k: {"longName": "Test"})
    monkeypatch.setattr("yfinance.Ticker", _OfflineTicker)
    return rows


def _two_non_usd() -> None:
    """AAA in USD (a card), BBB in EUR and DDD in CAD (dropped), CCC a row from before the measure."""
    _edgar("AAA", ANNUAL)
    _edgar("BBB", [], non_usd_units=["EUR"])
    _edgar("CCC", [])
    _edgar("DDD", [], non_usd_units=["CAD"])


def _steps(payload: dict) -> dict[str, dict]:
    return {s["key"]: s for s in payload["funnel_steps"]}


# ── the count and the currencies ─────────────────────────────────────────────

def test_the_scan_counts_the_non_usd_names_and_names_the_currencies(scan_env):
    _two_non_usd()

    payload = fp.run_scan()

    assert payload["data_quality"]["dropped"]["non_usd_currency"] == 2
    assert payload["data_quality"]["non_usd_units"] == ["CAD", "EUR"]
    assert [c["ticker"] for c in payload["candidates"]] == ["AAA"]      # they were not scored
    assert _steps(payload)["non_usd_currency"]["tickers"] == ["BBB", "DDD"]


def test_a_store_row_without_the_measure_is_not_counted_as_non_usd_and_is_no_error(scan_env):
    """Limitation of #46: rows stored before the measure carry no ``non_usd_units``. Absent is "not measured":
    such a name drops as ``no_edgar_annual``, exactly as before, and the currency count stays 0."""
    _edgar("AAA", ANNUAL)
    _edgar("CCC", [])

    payload = fp.run_scan()

    assert "non_usd_currency" not in payload["data_quality"]["dropped"]
    assert payload["data_quality"]["dropped"]["no_edgar_annual"] == 1
    assert "non_usd_units" not in payload["data_quality"]
    assert not payload.get("no_data")


def test_cards_plus_drops_reconcile_with_the_scanned_universe(scan_env):
    """The counter is a funnel drop, not loose text: the steps still add up."""
    _two_non_usd()

    payload = fp.run_scan()
    steps = payload["funnel_steps"]

    assert funnel.validate(steps) == []
    assert steps[0]["count"] == len(scan_env) == 4
    drops = sum(s["count"] for s in steps if s["kind"] == "drop")
    assert steps[0]["count"] - drops == steps[-1]["count"] == len(payload["candidates"])


def test_the_funnel_step_is_a_drop_with_the_measured_wording(scan_env):
    _two_non_usd()

    step = _steps(fp.run_scan())["non_usd_currency"]

    assert step["kind"] == "drop"
    assert step["count"] == 2
    assert "currency other than USD" in step["label"]


# ── the board ────────────────────────────────────────────────────────────────

def test_the_board_says_what_was_measured_and_never_ifrs(scan_env):
    _two_non_usd()

    html = render_fundamental_dashboard(fp.run_scan())

    assert "2 names report in a currency other than USD and could not be scored" in html
    assert "(CAD, EUR)" in html
    assert "IFRS" not in html


def test_zero_non_usd_names_renders_zero_not_a_blank(scan_env):
    """0 is a measurement, so it is shown: never a blank, never a dropped clause."""
    _edgar("AAA", ANNUAL)
    _edgar("CCC", [])

    payload = fp.run_scan()
    html = render_fundamental_dashboard(payload)

    assert payload["data_quality"]["dropped"].get("non_usd_currency", 0) == 0
    assert "0 names report in a currency other than USD and could not be scored" in html


def test_a_hostile_currency_string_is_escaped_on_the_board(scan_env):
    """The line renders a store value: ``meta`` is filer data and never reaches markup raw."""
    _two_non_usd()
    payload = fp.run_scan()
    payload["data_quality"]["non_usd_units"] = ["<img src=x onerror=alert(1)>"]

    html = render_fundamental_dashboard(payload)

    assert "<img" not in html and "&lt;img" in html


# ── no network ───────────────────────────────────────────────────────────────

def test_the_scan_makes_no_sec_request(scan_env, monkeypatch):
    """Test 7 of #46: every input is a stored row, so the currency count costs zero SEC calls."""
    calls: list = []

    def _no_sec(*a, **k):
        calls.append(a)
        raise AssertionError("a SEC request was made")

    for name in ("get_json", "get_text", "get_bytes", "_fetch_facts", "_cik_map"):
        monkeypatch.setattr(fp.sec_edgar, name, _no_sec)
    _two_non_usd()

    payload = fp.run_scan()

    assert calls == []
    assert payload["data_quality"]["dropped"]["non_usd_currency"] == 2
