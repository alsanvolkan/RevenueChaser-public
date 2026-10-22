"""Swing board, end to end (#145 U21): Refresh is store-only and honest about what it has.

Seeds the store, runs a Fetch, presses Refresh, and asserts the board shows that scan (never the fixture), the
Refresh adds no yfinance or provider call, and the durable score tables are untouched. A Refresh shows the last
Fetch and never re-scores (K4, #383 R1); with no Fetch it says no scan is stored. The empty-universe
case is the one that used to call the Yahoo screener from a GET.
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.module_testing.e2e.harness.screener_seed import bars_to, seed_d01, seed_quote


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
        "sector": "Information Technology", "industry": "Software",
        "industry_group": "Software & Services", "sub_industry": "Application Software",
        "quote": quote}])
    session = calendar.latest_complete_session()
    for t in ("ZETA", "SPY"):
        seed_d01(t, bars_to(session))
    seed_quote("ZETA", session, quote, 2.0e9)
    return dashboard


def _refresh(dashboard):
    return dashboard.view("module_swing_potential", refresh=True)


def _fetch(dashboard):
    """A Refresh shows the last Fetch and never re-scores (K4, #383 R1): the tests that read a scan Fetch first."""
    job = dashboard.run_job("/run/swing")
    dashboard.wait_until_idle()
    assert job.ok, job
    return job


def _payload(dashboard) -> dict:
    return json.loads(dashboard.artifact("swing_potential/swing_potential_latest.json").read_text(encoding="utf-8"))


class TestRefreshShowsTheScan:
    def test_refresh_renders_the_scan_not_the_fixture(self, dashboard, seeded):
        _fetch(dashboard)
        r = _refresh(dashboard)
        assert r.status_code == 200
        assert "[DEMO/FIXTURE]" not in r.text
        assert "No data:" not in r.text
        payload = _payload(dashboard)
        assert payload["no_data"] is False and payload["universe_total"] == 1

    def test_the_method_toggles_are_a_live_filter(self, dashboard, seeded, tmp_path):
        """#384 SW-S1: Minervini is ticked, and unticking widens the grid while adding a method narrows it."""
        from RevenueChaser.core.tests.board_js import run_board_js
        _fetch(dashboard)
        payload = _payload(dashboard)
        html = _refresh(dashboard).text
        assert 'data-method="minervini" checked' in html
        out = run_board_js(html, """
function cards(){ return (_els['grid'].innerHTML.match(/class="card"/g) || []).length; }
var log = {};
METHODS.minervini = false; render(); log.none = cards();
METHODS.minervini = true; render(); log.minervini = cards();
METHODS.weinstein = true; render(); log.both = cards();
console.log(JSON.stringify(log));
""", tmp_path, payload={"rows": payload["candidates"]})
        assert out["none"] >= out["minervini"]
        assert out["both"] <= out["minervini"]

    def test_refresh_makes_no_network_calls(self, dashboard, provider_routes, seeded):
        _fetch(dashboard)
        before, yf_before = provider_routes.request_count, dashboard.yf_calls()
        assert _refresh(dashboard).status_code == 200
        assert provider_routes.request_count == before
        assert dashboard.yf_calls() == yf_before

    def test_refresh_leaves_the_durable_score_tables_alone(self, dashboard, seeded):
        import RevenueChaser.core.rawstore as rawstore
        rawstore.replace_all("swing_scores", [{"ticker": "OLD1", "multiplier": 3}, {"ticker": "OLD2", "multiplier": 2}])
        assert _refresh(dashboard).status_code == 200
        assert dashboard.table_count("swing_scores") == 2

    def test_a_missing_spy_series_shows_no_regime(self, dashboard, seeded):
        import RevenueChaser.core.rawstore as rawstore
        with rawstore.connect(db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw")) as conn:
            conn.execute("DELETE FROM price_bars_daily WHERE ticker = 'SPY'")
        _fetch(dashboard)                       # no D01 SPY: the fallback finds no cache, and the fake Yahoo no SPY
        r = _refresh(dashboard)
        assert _payload(dashboard)["no_data"] is False
        assert "Regime —" in r.text
        assert "RISK-ON" not in r.text and "RISK-OFF" not in r.text
        assert _payload(dashboard)["regime"] is None

    def test_refresh_with_no_stored_scan_says_so_not_fixture(self, dashboard, seeded):
        """No Fetch yet: the honest empty state names the missing scan, never the demo fixture (K4, #383 R1)."""
        r = _refresh(dashboard)
        assert "[DEMO/FIXTURE]" not in r.text
        assert "No stored scan yet" in r.text
        assert _payload(dashboard)["no_data_reason"] == "no_scan_stored"


class TestEmptyUniverse:
    def test_refresh_with_no_stored_universe_says_so_and_calls_nothing(self, dashboard, provider_routes):
        before = provider_routes.request_count
        r = _refresh(dashboard)
        assert r.status_code == 200
        assert "No stored universe yet" in r.text and "Fetch Universe" in r.text
        assert "[DEMO/FIXTURE]" not in r.text
        assert dashboard.yf_calls() == [], "a Refresh reached the Yahoo screener"
        assert provider_routes.request_count == before

    def test_a_plain_load_with_no_artifact_does_the_same(self, dashboard):
        """The plain-load self-heal shares the Refresh path (``_MODEL_ONLY_RENDERERS``)."""
        r = dashboard.view("module_swing_potential")
        assert r.status_code == 200
        assert "No stored universe yet" in r.text
        assert dashboard.yf_calls() == []


def test_a_card_with_no_rr_shows_na(tmp_path):
    """#384 SW-S2 (DF81): a null R:R shows "R:R n/a" and no slider, never a made-up 2.0."""
    from RevenueChaser.core.tests.board_js import run_board_js
    from RevenueChaser.module_swing_potential.pipeline import build_payload
    from RevenueChaser.module_swing_potential.report import render_swing_dashboard
    gate = {"passed": True, "score": "1/1", "rules": [{"id": "r", "passed": True, "actual": 1.0}]}
    p = build_payload([], run_stats={})
    p["is_fixture"] = False
    p["candidates"] = [{
        "ticker": "ZETA", "name": "Zeta Corp", "sector": "Tech", "price": 10.0, "change_1w": 0.01,
        "change_1m": 0.01, "market_cap": 2e9, "themes": [], "index": [], "earnings_date": None,
        "quality": 80, "rs_rating": 90, "entry": 10.0, "target": None, "stop": None, "rr": None, "atr": 0.5,
        "methods": ["minervini"], "bucket": "minervini", "direction_ok": True,
        "gates": {k: gate for k in ("minervini", "weinstein", "qullamaggie", "direction")}}]
    grid = run_board_js(render_swing_dashboard(p),
                        "console.log(JSON.stringify(_els['grid'].innerHTML));", tmp_path)
    assert "R:R <b>n/a</b>" in grid
    assert 'type="range"' not in grid
