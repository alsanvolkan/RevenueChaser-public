"""The Earnings board's one reader of the market store: the 10-year yield series (need ``macro.dgs10``)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.module_earnings_radar import store_reads

from RevenueChaser.module_earnings_radar.tests.market_store_fixture import serve

LAST = date(2026, 10, 26)


def _sessions_back(day: date, n: int) -> date:
    for _ in range(n):
        day = calendar.previous_trading_day(day - timedelta(days=1))
    return day


def _series(end: date, n: int, first: float = 4.0) -> dict[str, float]:
    out, day = {}, end
    for i in range(n):
        out[day.isoformat()] = first + i / 100
        day = calendar.previous_trading_day(day - timedelta(days=1))
    return out


def test_fresh_series_is_read_from_the_market_store():
    serve(_series(LAST, 5))
    rows, note = store_reads.dgs10_map(latest=LAST)
    assert rows[LAST.isoformat()] == pytest.approx(4.0)
    assert len(rows) == 5
    assert note["stored"] is True
    assert note["source"] == store_reads.SERIES_SOURCE


def test_a_stale_newest_observation_is_not_read():
    stale = _sessions_back(LAST, store_reads.MAX_LAG["dgs10"] + 1)
    serve(_series(stale, 5))
    rows, note = store_reads.dgs10_map(latest=LAST)
    assert rows == {}
    assert note["stored"] is False
    assert "stale" in note["label"]


def test_no_serving_file_is_nothing_stored_not_an_error():
    rows, note = store_reads.dgs10_map(latest=LAST)
    assert rows == {}
    assert note["stored"] is False


def test_at_most_the_window_the_board_pairs():
    serve(_series(LAST, store_reads.DGS10_LIMIT + 10))
    rows, _note = store_reads.dgs10_map(latest=LAST)
    assert len(rows) == store_reads.DGS10_LIMIT
    assert max(rows) == LAST.isoformat()


# ── the board's own warm step: the store, and no provider ──

def _pipeline():
    from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
    return EarningsPipeline(["AAPL"], uw=object(), cal_meta={})


def test_the_run_warms_the_yield_series_from_the_market_store():
    serve(_series(LAST, 5))
    p = _pipeline()
    assert p._warm_dgs10()[LAST.isoformat()] == pytest.approx(4.0)
    assert p.input_sources["dgs10"] == "stored"
    assert "10-year yield through" in p.input_sources["dgs10_note"]


def test_without_a_market_store_the_run_asks_no_provider():
    p = _pipeline()
    assert p._warm_dgs10() == {}
    assert p.input_sources["dgs10"] == "none"
    assert p.input_sources["dgs10_note"]


def test_the_board_no_longer_has_a_live_fred_reader():
    """The cut itself: the board's FRED map and its yfinance weekly reader are gone. The nightly FRED sweep keeps
    ``fred_daily_result``, which is ingestion, not a board read."""
    from RevenueChaser.module_earnings_radar import macro_watch
    assert not hasattr(macro_watch, "fred_daily_map")
    assert not hasattr(macro_watch, "macro_weekly_closes")
    assert not hasattr(macro_watch, "_yf_weekly")
    assert hasattr(macro_watch, "fred_daily_result")


# ── the cases the old stored reader pinned, now against the market store ──

def test_an_observation_with_no_value_is_skipped():
    serve({LAST.isoformat(): None})
    rows, note = store_reads.dgs10_map(latest=LAST)
    assert rows == {}
    assert note["stored"] is False


def test_only_the_series_own_source_is_read(monkeypatch):
    """``market_series`` is keyed (series_id, obs_date, source): another source's row for the same id is not the
    10-year yield the board reads (``MACRO_SERIES["US10Y"]["series"]`` names FRED)."""
    monkeypatch.setattr(store_reads.read, "market_series",
                        lambda ids, start=None, end=None: [
                            {"series_id": "DGS10", "obs_date": LAST, "source": "fred", "value": 4.0},
                            {"series_id": "DGS10", "obs_date": LAST, "source": "yahoo", "value": 9.9}])
    rows, _note = store_reads.dgs10_map(latest=LAST)
    assert rows == {LAST.isoformat(): 4.0}


def test_an_observation_exactly_at_the_lag_limit_is_still_read():
    """Three sessions after the newest observation, up to the board's day, is ``MAX_LAG``."""
    newest = _sessions_back(LAST, store_reads.MAX_LAG["dgs10"])
    serve(_series(newest, 10))
    rows, _note = store_reads.dgs10_map(latest=LAST)
    assert max(rows) == newest.isoformat()


def test_a_bond_market_only_observation_still_counts_its_sessions():
    """FRED observes bond-market days: Good Friday 2026-04-03 has no stock session, and the lag still counts."""
    serve({"2026-04-02": 4.0, "2026-04-03": 4.1})
    rows, _note = store_reads.dgs10_map(latest=date(2026, 4, 6))
    assert rows == {"2026-04-02": 4.0, "2026-04-03": 4.1}
