"""The yfinance fake, observed end to end.

The fake counts calls in ``_CALLS``, but that list lives inside the dashboard
**subprocess** — the test process can never read it. S2 closes that hole with a
call log file (``$RC_E2E_YF_LOG``), so "this Fetch touched Yahoo" and "this
store-only view did not" become assertable from the outside.

This file also pins the ``fast_info`` contract the fake must honour for Phase B:
``.shares`` / ``.last_price`` come from the fixture, and ``.market_cap`` raises,
because real yfinance silently falls back to ``Ticker.info`` (a network call) for
it.
"""
from __future__ import annotations

import pytest

from .harness import yfinance_fake


class TestFastInfoContract:
    """What Phase B reads, and the one attribute it must never read."""

    def test_shares_and_last_price_come_from_the_fixture(self):
        fi = yfinance_fake.FakeTicker("MU").fast_info
        assert fi.shares == pytest.approx(1.12e9)
        assert fi.last_price == pytest.approx(110.0)

    def test_market_cap_raises_instead_of_falling_back_to_info(self):
        with pytest.raises(AssertionError):
            _ = yfinance_fake.FakeTicker("MU").fast_info.market_cap

    def test_a_ticker_without_fast_info_yields_no_numbers(self):
        fi = yfinance_fake.FakeTicker("NOCAP").fast_info
        assert fi.shares is None
        assert fi.last_price is None


class TestCallsCrossTheProcessBoundary:
    def test_yf_calls_are_visible_across_the_process_boundary(self, dashboard, monkeypatch):
        import RevenueChaser.core.rawstore as rawstore

        monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
        rawstore.replace_all("equity_universe", [
            {"ticker": "AAPL", "market_cap": 3.0e12},
            {"ticker": "MU", "market_cap": None},
        ])

        # A plain, store-only /view must not reach yfinance ...
        assert dashboard.view("module_settings").status_code == 200
        assert dashboard.yf_calls() == [], (
            f"a store-only view recorded yfinance calls: {dashboard.yf_calls()}")

        # ... while a Fetch that resolves the NULL cap must leave a line behind.
        result = dashboard.run_job("/run/universe-stats")
        assert result.ok, f"universe-stats failed: {result}\n{dashboard.logs()}"
        calls = dashboard.yf_calls()
        assert calls, "0 yfinance calls recorded across the process boundary"
        assert "fast_info:MU" in calls
