"""Asymmetric Growth board, end to end (#202): a ticker with EDGAR annual data whose
legacy ``edgar_financials.meta`` cell is double-encoded must stay on the board.

The store is seeded the way the live one looks (JSON-typed EDGAR columns, meta a JSON
*string* holding the JSON object), then a store-only Refresh renders the board.
"""
from __future__ import annotations

import json
import time

import duckdb
import pytest

_ANNUAL = [
    {"fiscal_year": "2023", "revenue": 800.0, "gross_profit": 400.0, "operating_cash_flow": 120.0,
     "capex": 20.0, "total_debt": 50.0, "cash": 200.0, "total_assets": 1000.0,
     "rd_expense": 100.0, "diluted_shares": 100.0},
    {"fiscal_year": "2024", "revenue": 850.0, "gross_profit": 500.0, "operating_cash_flow": 150.0,
     "capex": 25.0, "total_debt": 50.0, "cash": 250.0, "total_assets": 1050.0,
     "rd_expense": 130.0, "diluted_shares": 101.0},
    {"fiscal_year": "2025", "revenue": 1080.0, "gross_profit": 700.0, "operating_cash_flow": 250.0,
     "capex": 40.0, "total_debt": 60.0, "cash": 400.0, "total_assets": 1200.0,
     "rd_expense": 200.0, "diluted_shares": 102.0},
]
_QUOTE = {"regularMarketPrice": 100.0, "fiftyTwoWeekHigh": 105.0, "fiftyDayAverage": 90.0,
          "averageDailyVolume10Day": 160.0, "averageDailyVolume3Month": 100.0}


@pytest.fixture
def seeded_edgar_ticker(dashboard, monkeypatch):
    import RevenueChaser.core.rawstore as rawstore

    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
    rawstore.write_rows("equity_universe", [{
        "ticker": "EDGR", "market_cap": 5e9, "sector": "Technology", "price": 100.0,
        "name": "Edgar Co", "quote": _QUOTE}])
    # yfinance payload carries only info: the annual set comes from EDGAR
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": "EDGR", "fetched_at": time.time(), "fetched_at_iso": "2026-10-02T00:00:00+00:00",
        "payload": {"ticker": "EDGR", "sector": "Technology", "market_cap": 5e9, "source": "test",
                    "info": {"longName": "Edgar Co", "country": "United States", "exchange": "NMS",
                             "industry": "Software"}, "annual": []}}])
    with duckdb.connect(str(rawstore.edgar_db_path())) as c:
        c.execute("CREATE TABLE edgar_financials (ticker TEXT PRIMARY KEY, "
                  "annual JSON, quarterly JSON, meta JSON)")
        c.execute("INSERT INTO edgar_financials VALUES ('EDGR', ?, '[]', ?)", [
            json.dumps(json.dumps(_ANNUAL)),
            json.dumps(json.dumps({"source": "sec_edgar", "is_us": True, "available": True}))])
    return dashboard


def test_refresh_keeps_a_ticker_with_edgar_annual_data(seeded_edgar_ticker, provider_routes):
    """The Fetch scores the EDGAR-backed ticker; the Refresh shows it from the stored rows (K4, #383 R1)."""
    job = seeded_edgar_ticker.run_job("/run/module_asymmetric_growth")
    seeded_edgar_ticker.wait_until_idle()
    assert job.ok, job
    before, yf_before = provider_routes.request_count, seeded_edgar_ticker.yf_calls()
    r = seeded_edgar_ticker.view("module_asymmetric_growth", refresh=True)
    assert r.status_code == 200
    assert "could not be scored" not in r.text
    # The cards come from the payload route (#380 S7, K10), not the page.
    data = seeded_edgar_ticker.get("/view/module_asymmetric_growth/payload.json")
    assert data.status_code == 200
    assert "EDGR" in [c["ticker"] for c in data.json()["cards"]], "EDGAR-backed ticker was dropped from the board"
    assert provider_routes.request_count == before, "refresh reached the network"
    assert seeded_edgar_ticker.yf_calls() == yf_before, "refresh reached yfinance"


def test_refresh_leaves_ticker_scores_alone(seeded_edgar_ticker):
    import RevenueChaser.core.rawstore as rawstore
    rawstore.replace_all("ticker_scores", [{"ticker": "OLD1"}, {"ticker": "OLD2"}])
    assert seeded_edgar_ticker.view("module_asymmetric_growth", refresh=True).status_code == 200
    assert seeded_edgar_ticker.table_count("ticker_scores") == 2


def test_refresh_with_no_stored_universe_says_so_and_calls_nothing(dashboard, provider_routes):
    before = provider_routes.request_count
    r = dashboard.view("module_asymmetric_growth", refresh=True)
    assert r.status_code == 200
    assert "No stored universe yet" in r.text and "Fetch Universe" in r.text
    assert "Click Refresh to retry" not in r.text
    assert dashboard.yf_calls() == [], "a Refresh reached the Yahoo screener"
    assert provider_routes.request_count == before
