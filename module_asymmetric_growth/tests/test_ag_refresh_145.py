"""#145 (27-1) in the Asymmetric Growth engine: the universe load honours ``no_fetch``.

``fetch_universe_stats(no_fetch=True)`` is the store-only read behind a Refresh and the plain-load self-heal.
It returns the stored universe or an empty result, and never calls the Yahoo screener, whatever ``refresh`` says.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore, universe_fetch
from RevenueChaser.module_asymmetric_growth import engine


@pytest.fixture
def screener(monkeypatch):
    """Records every screener call; a call is a failure for a store-only load."""
    calls: list[int] = []
    import yfinance
    monkeypatch.setattr(yfinance, "screen", lambda *a, **k: calls.append(1) or {"quotes": [], "total": 0})
    monkeypatch.setattr("time.sleep", lambda s: None)
    return calls


def _store(*tickers):
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "market_cap": 1e9, "price": 10.0, "name": t, "sector": "Technology"} for t in tickers])
    rawstore.replace_all("equity_universe_provenance", [
        {"key": engine._UNIVERSE_META_KEY, "generated_at": "2026-10-01T00:00:00+00:00", "refreshed_by": "Swing Potential"}])


def test_no_fetch_with_an_empty_store_makes_no_screener_call(rawstore_tmp_db, screener):
    rows, stats = engine.fetch_universe_stats(no_fetch=True)
    assert rows == [] and screener == []
    assert stats["empty_store"] is True and stats["cached"] is False
    assert stats["universe_generated_at"] is None


def test_no_fetch_ignores_refresh(rawstore_tmp_db, screener):
    rows, _ = engine.fetch_universe_stats(no_fetch=True, refresh=True)
    assert rows == [] and screener == []


def test_no_fetch_returns_the_stored_universe_with_its_provenance(rawstore_tmp_db, screener):
    _store("AAA", "BBB")
    rows, stats = engine.fetch_universe_stats(no_fetch=True, refresh=True)
    assert sorted(r["ticker"] for r in rows) == ["AAA", "BBB"]
    assert screener == []
    assert stats["cached"] is True and stats["universe_refreshed_by"] == "Swing Potential"
    assert "empty_store" not in stats


def test_a_fetch_with_an_empty_store_still_tries_the_screener(rawstore_tmp_db, screener, monkeypatch):
    """The first load is the documented network step (D1); only a Fetch makes it.

    Membership comes from the SEC list first (#264), so the test hands the fetch a healthy list; the screener
    then answers nothing and the fetch still went to it.
    """
    monkeypatch.setattr(universe_fetch, "_sec_member_list", lambda: ([{"ticker": "AAA", "name": "AAA Inc"}], None))
    monkeypatch.setattr(universe_fetch, "MIN_FULL_UNIVERSE", 1)
    monkeypatch.setattr(universe_fetch, "backfill_universe_caps", lambda **kw: {})
    _, stats = engine.fetch_universe_stats()
    assert screener, "a Fetch with no stored universe must call the screener"
    assert "empty_store" not in stats


def test_the_hunter_reports_an_unloaded_store_on_a_refresh(rawstore_tmp_db, screener):
    result = engine.run_asymmetric_growth_hunter(None, no_fetch=True)
    assert screener == []
    assert result["cards"] == [] and result["universe_total"] == 0
    assert result["no_data_reason"] == "no_universe_stored"
    assert "No stored universe yet" in result["no_data_message"]


def test_the_hunter_reports_an_empty_screen_on_a_fetch(rawstore_tmp_db, screener, monkeypatch):
    # The SEC list is empty (#264): a Fetch ends with no universe, and the board says so (not "not stored").
    monkeypatch.setattr(universe_fetch, "_sec_member_list", lambda: ([], "SEC ticker map unavailable"))
    result = engine.run_asymmetric_growth_hunter(None)
    assert result["no_data_reason"] == "no_universe"


def test_a_populated_hunter_result_carries_no_empty_marker(rawstore_tmp_db, screener, monkeypatch):
    _store("AAA")
    monkeypatch.setattr(engine, "enrich_and_score", lambda *a, **k: None)
    result = engine.run_asymmetric_growth_hunter(None, no_fetch=True)
    assert "no_data_reason" not in result and "no_data_message" not in result


def test_the_empty_board_shows_the_scans_message_not_click_refresh():
    from RevenueChaser.module_asymmetric_growth.report import render_asymmetric_growth_hunter_html
    from RevenueChaser.core.scan_quality import no_data_message
    msg = no_data_message("no_universe_stored")
    html = render_asymmetric_growth_hunter_html({
        "asof": "2026-10-03T00:00:00", "universe_total": 0, "summary": {}, "cards": [], "thresholds": engine.THRESHOLDS,
        "no_data_reason": "no_universe_stored", "no_data_message": msg})
    assert "No stored universe yet" in html
    assert "Click Refresh to retry" not in html


def test_an_unscored_but_stored_board_has_a_fallback_message():
    from RevenueChaser.module_asymmetric_growth.report import render_asymmetric_growth_hunter_html
    html = render_asymmetric_growth_hunter_html({
        "asof": "2026-10-03T00:00:00", "universe_total": 3, "summary": {}, "cards": [], "thresholds": engine.THRESHOLDS})
    assert "No companies could be scored from the stored data" in html
