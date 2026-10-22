"""Fundamentals board, end to end: Refresh shows real rows, never the fixture (#205).

Since 2026-09-27 the board showed its demo fixture because a Fetch dropped cached
bars when yfinance failed and ``run_scan`` then silently emitted the fixture payload.
These tests seed the store, run a Fetch, press Refresh, and assert the rows on
the board are the seeded ones. A Refresh shows the last Fetch and never re-scores
(K4, #383 R1); with no Fetch it says no scan is stored.
"""
from __future__ import annotations

import time

import pytest

from RevenueChaser.module_testing.e2e.harness.screener_seed import bars_to, seed_d01, seed_quote

_ANNUAL = [
    {"fiscal_year": "2024", "revenue": 100.0e6, "gross_profit": 60.0e6, "operating_cash_flow": 25.0e6,
     "capex": 4.0e6, "total_debt": 10.0e6, "cash": 30.0e6, "total_assets": 200.0e6,
     "total_equity": 100.0e6, "net_income": 10.0e6, "ebit": 12.0e6, "depreciation": 3.0e6},
    {"fiscal_year": "2025", "revenue": 130.0e6, "gross_profit": 78.0e6, "operating_cash_flow": 30.0e6,
     "capex": 4.0e6, "total_debt": 10.0e6, "cash": 30.0e6, "total_assets": 220.0e6,
     "total_equity": 110.0e6, "net_income": 14.0e6, "ebit": 16.0e6, "depreciation": 3.0e6},
]


@pytest.fixture
def seeded(dashboard, monkeypatch):
    """ZETA and SPY in D01 up to the latest complete session, ZETA's quote in D04 (the C12 inputs, #383 R3)."""
    import RevenueChaser.core.rawstore as rawstore
    from RevenueChaser.core import calendar

    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
    rawstore.ensure_all_tables()
    quote = {"regularMarketPrice": 130.0, "fiftyTwoWeekHigh": 135.0, "fiftyDayAverage": 120.0,
             "averageDailyVolume10Day": 2_000_000}
    rawstore.replace_all("equity_universe", [{
        "ticker": "ZETA", "cik": "", "name": "Zeta Corp", "market_cap": 2.0e9, "price": 130.0,
        "sector": "Technology", "industry": "Software - Application",
        "quote": quote}])
    session = calendar.latest_complete_session()
    for t in ("ZETA", "SPY"):
        seed_d01(t, bars_to(session))
    seed_quote("ZETA", session, quote, 2.0e9)
    rawstore.write_rows("edgar_financials", [{
        "ticker": "ZETA", "annual": _ANNUAL, "quarterly": [],
        "meta": {"source": "sec_edgar", "entity_name": "Zeta Corp", "is_us": True, "available": True},
        "fetched_at": time.time()}])
    return dashboard


def _refresh(dashboard):
    return dashboard.view("module_fundamentals", refresh=True)


def _rows(dashboard) -> list[dict]:
    """The rows the page fetches after its HTML (K10, #383 R3 slice 0): the board's payload route."""
    r = dashboard.get("/view/module_fundamentals/payload.json")
    assert r.status_code == 200
    return r.json()["rows"]


def _fetch(dashboard):
    """A Refresh shows the last Fetch and never re-scores (K4, #383 R1): the tests that read rows Fetch first."""
    job = dashboard.run_job("/run/module_fundamentals")
    dashboard.wait_until_idle()
    assert job.ok, job
    return job


class TestRefreshShowsRealRows:
    def test_refresh_renders_the_seeded_ticker_not_the_fixture(self, dashboard, seeded):
        _fetch(dashboard)
        r = _refresh(dashboard)
        assert r.status_code == 200
        assert [c["ticker"] for c in _rows(dashboard)] == ["ZETA"]
        assert "[DEMO/FIXTURE]" not in r.text
        assert "No data:" not in r.text

    def test_refresh_carries_the_spine_sector_and_industry(self, dashboard, seeded):
        """#266: the card's sector and industry are the spine's two levels, and the board has no group or sub-industry."""
        _fetch(dashboard)
        r = _refresh(dashboard)
        assert r.status_code == 200
        row = _rows(dashboard)[0]
        assert row["sector"] == "Technology" and row["industry"] == "Software - Application"
        assert 'id="rc-filters"' in r.text
        assert "industry_group" not in r.text and "sub_industry" not in r.text
        assert "industry_group" not in row and "sub_industry" not in row

    def test_refresh_makes_no_network_calls(self, dashboard, provider_routes, seeded):
        _fetch(dashboard)
        before, yf_before = provider_routes.request_count, dashboard.yf_calls()
        assert _refresh(dashboard).status_code == 200
        assert provider_routes.request_count == before
        assert dashboard.yf_calls() == yf_before

    def test_refresh_with_no_stored_scan_says_so_not_fixture(self, dashboard, seeded):
        """No Fetch yet: the honest empty state names the missing scan, never the demo fixture (K4, #383 R1)."""
        r = _refresh(dashboard)
        assert r.status_code == 200
        assert "[DEMO/FIXTURE]" not in r.text
        assert "No stored scan yet" in r.text


class TestRuleToggles:
    """#386 F2.8/F3: the rule toggles replace the gate tabs; Rule of 40 is on by default."""

    def test_the_board_loads_with_the_rule_of_40_toggle_checked(self, dashboard, seeded):
        _fetch(dashboard)
        r = _refresh(dashboard)
        assert r.status_code == 200
        assert 'id="rule-rule_of_40" checked' in r.text
        assert 'id="rule-quality"' in r.text and 'id="rule-momentum"' in r.text

    def test_unticking_the_rule_of_40_shows_at_least_as_many_cards(self, dashboard, seeded, tmp_path):
        from RevenueChaser.core.tests.board_js import run_board_js
        _fetch(dashboard)
        html = _refresh(dashboard).text
        payload = {"rows": _rows(dashboard)}
        out = run_board_js(html, """
function cards(){ return (_els['grid'].innerHTML.match(/class="card"/g) || []).length; }
var a = cards();
setRule('rule_of_40', false);
var b = cards();
console.log(JSON.stringify({on: a, off: b}));
""", tmp_path, payload=payload)
        assert out["on"] >= 1
        assert out["off"] >= out["on"]


class TestRefreshIsStoreOnly:
    """#145: a Refresh never calls the screener and never rewrites the durable score tables."""

    def test_refresh_leaves_the_durable_score_tables_alone(self, dashboard, seeded):
        import RevenueChaser.core.rawstore as rawstore
        rawstore.replace_all("fundamentals_scores", [{"ticker": "OLD1", "multiplier": 3},
                                                     {"ticker": "OLD2", "multiplier": 2}])
        assert _refresh(dashboard).status_code == 200
        assert dashboard.table_count("fundamentals_scores") == 2

    def test_a_missing_spy_series_shows_no_regime(self, dashboard, seeded):
        import RevenueChaser.core.rawstore as rawstore
        with rawstore.connect(db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw")) as conn:
            conn.execute("DELETE FROM price_bars_daily WHERE ticker = 'SPY'")
        _fetch(dashboard)                       # no D01 SPY: the fallback finds no cache, and the fake Yahoo no SPY
        r = _refresh(dashboard)
        assert [c["ticker"] for c in _rows(dashboard)] == ["ZETA"]
        assert "Regime —" in r.text
        assert "RISK-ON" not in r.text and "RISK-OFF" not in r.text

    def test_refresh_with_no_stored_universe_says_so_and_calls_nothing(self, dashboard, provider_routes):
        before = provider_routes.request_count
        r = _refresh(dashboard)
        assert r.status_code == 200
        assert "No stored universe yet" in r.text and "Fetch Universe" in r.text
        assert "[DEMO/FIXTURE]" not in r.text
        assert dashboard.yf_calls() == [], "a Refresh reached the Yahoo screener"
        assert provider_routes.request_count == before
