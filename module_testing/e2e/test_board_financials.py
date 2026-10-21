"""Financials board, end to end: store-only refresh and error surfacing.

module_financials was the board with the silent-fallback bug: a store-only
re-render failure was swallowed by ``run_quick_cached`` (warning logged, stale
artifact or empty state served), so the tab emptied and the app went back over
the network even though the data sat in ``.store``. These tests seed the store
directly — no yfinance fixtures needed for the offline path — and prove:

1. Refresh renders the report from the store with zero provider calls.
2. A genuine render failure (the report file made unwritable, the same failure
   family as the cleared-``.cache`` FileNotFoundError) is *displayed* as an
   error board instead of being swallowed.
"""
from __future__ import annotations

import time

import pytest

_PAYLOAD = {
    "ticker": "AAPL", "sector": "Technology", "shares_outstanding": 15.0e9,
    "market_cap": 3.7e12, "current_price": 250.0, "currency": "USD", "source": "test",
    "info": {"longName": "Apple Inc.", "shortName": "Apple"},
    "annual": [
        {"fiscal_year": "2021", "revenue": 365.0e9, "ebitda": 100.0e9, "ebit": 90.0e9,
         "depreciation": 11.0e9, "capex": 11.0e9, "net_income": 95.0e9,
         "total_debt": 120.0e9, "cash": 35.0e9, "total_assets": 350.0e9,
         "total_equity": 63.0e9, "receivables": 30.0e9, "inventory": 5.0e9,
         "payables": 50.0e9, "current_assets": 135.0e9, "current_liabilities": 130.0e9,
         "interest_expense": 2.5e9, "pretax_income": 110.0e9, "tax_provision": 14.0e9},
        {"fiscal_year": "2022", "revenue": 395.0e9, "ebitda": 115.0e9, "ebit": 105.0e9,
         "depreciation": 11.0e9, "capex": 11.0e9, "net_income": 100.0e9,
         "total_debt": 110.0e9, "cash": 25.0e9, "total_assets": 350.0e9,
         "total_equity": 50.0e9, "receivables": 30.0e9, "inventory": 5.0e9,
         "payables": 50.0e9, "current_assets": 135.0e9, "current_liabilities": 130.0e9,
         "interest_expense": 2.5e9, "pretax_income": 120.0e9, "tax_provision": 14.0e9},
        {"fiscal_year": "2023", "revenue": 385.0e9, "ebitda": 110.0e9, "ebit": 100.0e9,
         "depreciation": 11.0e9, "capex": 11.0e9, "net_income": 97.0e9,
         "total_debt": 110.0e9, "cash": 30.0e9, "total_assets": 350.0e9,
         "total_equity": 60.0e9, "receivables": 30.0e9, "inventory": 5.0e9,
         "payables": 50.0e9, "current_assets": 135.0e9, "current_liabilities": 130.0e9,
         "interest_expense": 2.5e9, "pretax_income": 115.0e9, "tax_provision": 14.0e9},
    ],
}


@pytest.fixture
def seeded_apl(dashboard, monkeypatch):
    """Seed AAPL's raw payload + a quick-remarks card into the dashboard's store.

    Writes happen from the test process straight into the dashboard's temp
    ``raw.duckdb`` (idle — the dashboard holds no persistent handle), mirroring
    the exact rows a live Fetch would have written, so no yfinance fixtures are
    needed to exercise the offline refresh path.
    """
    import RevenueChaser.core.rawstore as rawstore
    from RevenueChaser.core import ticker_store

    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
    ticker_store.write_from_cards([{
        "ticker": "AAPL", "name": "Apple Inc.", "sector": "Technology",
        "industry": "Consumer Electronics", "price": 250.0, "market_cap": 3.7e12,
        "total_points": 5, "source": "test", "themes": [], "signals": [],
    }])
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": "AAPL",
        "payload": dict(_PAYLOAD),
        "fetched_at": time.time(),
        "fetched_at_iso": "2026-09-02T08:00:00+00:00",
    }])
    return dashboard


def _refresh(dashboard, **params):
    return dashboard.view("module_financials", refresh=True, **params)


class TestRefreshIsOfflineAndFaithful:
    def test_refresh_renders_the_report_from_the_store(self, dashboard, seeded_apl):
        r = _refresh(dashboard, ticker="AAPL")
        assert r.status_code == 200
        assert "Apple Inc." in r.text
        assert "No Financials data yet" not in r.text

    def test_refresh_rebuilds_a_deleted_artifact(self, dashboard, seeded_apl, clear_cache):
        clear_cache()
        assert not dashboard.artifact("financials_latest.html").exists()
        r = _refresh(dashboard, ticker="AAPL")
        assert r.status_code == 200
        assert "Apple Inc." in r.text

    def test_refresh_makes_no_network_calls(self, dashboard, provider_routes, seeded_apl):
        """The no-network contract, counted at the HTTP boundary."""
        before = provider_routes.request_count
        r = _refresh(dashboard, ticker="AAPL")
        assert r.status_code == 200
        assert provider_routes.request_count == before, (
            "refresh reached the network: "
            f"{[q.path for q in provider_routes.requests()[before:]]}")

    def test_refresh_survives_a_restart(self, dashboard, seeded_apl):
        """Only what reached .store may be load-bearing."""
        dashboard.restart()
        r = _refresh(dashboard, ticker="AAPL")
        assert r.status_code == 200
        assert "Apple Inc." in r.text


def _break_payload():
    """Replace AAPL's stored payload with one the engines cannot read (``annual`` holds a bare number).

    Since #173 D5 the dashboard writes its single ``financials_latest.html`` directly, so a blocked
    write would also block the error board that replaces it. A render failure is forced from the
    data instead: the same path, a store-only re-render that raises."""
    import RevenueChaser.core.rawstore as rawstore

    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": "AAPL",
        "payload": {**_PAYLOAD, "annual": [5]},
        "fetched_at": time.time(),
        "fetched_at_iso": "2026-09-02T08:00:00+00:00",
    }])


# The error board names the exception type (``app/runners.py::_write_quick_error``).
_RENDER_ERRORS = ("TypeError",)


class TestRenderFailureIsDisplayed:
    def test_a_failed_refresh_shows_an_error_board_not_a_swallowed_empty(self,
                                                                         dashboard,
                                                                         seeded_apl):
        """Force the store-only re-render to raise: it must be *displayed*
        (error board), not swallowed into a silent empty tab + a network Fetch."""
        r = _refresh(dashboard, ticker="AAPL")
        assert r.status_code == 200 and "Apple Inc." in r.text
        assert dashboard.artifact("financials_latest.html").exists()

        _break_payload()
        r = _refresh(dashboard, ticker="AAPL")

        # The failure must be visible, not papered over.
        assert r.status_code == 200
        assert "Refresh failed" in r.text
        assert any(e in r.text for e in _RENDER_ERRORS)
        assert "Apple Inc." not in r.text

    def test_the_error_board_replaces_the_artifact(self, dashboard, seeded_apl):
        """The stale report must not linger as if it were fresh."""
        _refresh(dashboard, ticker="AAPL")
        _break_payload()
        _refresh(dashboard, ticker="AAPL")
        body = dashboard.artifact("financials_latest.html").read_text(encoding="utf-8")
        assert "Refresh failed" in body
        assert "Apple Inc." not in body




class TestEmptyStore:
    def test_a_board_with_no_data_renders_an_empty_state(self, dashboard):
        r = dashboard.view("module_financials")
        assert r.status_code == 200
        assert "No Financials data yet" in r.text
