"""U1 (#402, K8b's SPY part), on the prices store since PR 10 (G4): the SPY anchor of the Swing and Fundamentals
freeze and the picks' own check read the same daily bars the boards score from.

On 2026-10-14 the daily bars had SPY's session, but the freeze read SPY from the old ``swing_ohlc`` cache, whose
refetch had failed, and refused both boards. That cache is no longer read at all (F4 A, part 1). Offline; the prices
store is seeded directly (``prices_seed``).
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import ledger, market_bars, rawstore
from RevenueChaser.rcdata.tests.prices_seed import seed_bars

D = date(2026, 10, 2)          # a Friday and a normal session


def _close_plus(minutes, day=D):
    return cal.session_close(day).timestamp() + minutes * 60


def _seed_cache(ticker, last_day, cached_at):
    rawstore.write_rows("swing_ohlc", [
        {"ticker": ticker, "ts": f"{d}T00:00:00", "open": 1.0, "high": 2.0, "low": 0.5,
         "close": 1.5, "volume": 100.0, "cached_at": cached_at}
        for d in ("2026-09-30", "2026-10-01", last_day)])


def _seed_bars(ticker, last_day, fetched_at):
    seed_bars(ticker, ["2026-09-30", "2026-10-01", last_day], fetched_at=fetched_at)


def test_a_complete_session_bar_anchors_the_board_whatever_the_old_cache_holds(rawstore_tmp_db):
    """The 14 Oct shape: the old cache's SPY ends the day before, the prices store has the session."""
    _seed_cache("SPY", "2026-10-01", _close_plus(-24 * 60))
    _seed_bars("SPY", "2026-10-02", _close_plus(40))
    assert ledger.spy_anchor_problem(D) is None


def test_the_old_cache_never_anchors_the_board(rawstore_tmp_db):
    _seed_cache("SPY", "2026-10-02", _close_plus(45))
    assert ledger.spy_anchor_problem(D) == "no SPY bars in the prices store"
    _seed_bars("SPY", "2026-10-01", _close_plus(40, date(2026, 10, 1)))
    assert ledger.spy_anchor_problem(D) == "SPY's last bar is 2026-10-01, not the 2026-10-02 session"


def test_a_session_bar_read_before_the_close_plus_30_is_not_the_session():
    _seed_bars("SPY", "2026-10-02", _close_plus(29))
    assert ledger.spy_anchor_problem(D) == "SPY's 2026-10-02 bar was read before the close + 30 min"
    assert ledger.prices_has_session(D, "SPY") is False
    _seed_bars("SPY", "2026-10-02", _close_plus(31))
    assert ledger.spy_anchor_problem(D) is None and ledger.prices_has_session(D, "SPY") is True


def test_a_bar_later_than_the_session_is_not_the_session():
    _seed_bars("SPY", "2026-10-05", _close_plus(40, date(2026, 10, 5)))
    assert ledger.prices_has_session(D, "SPY") is False


def test_prices_last_bars_is_each_tickers_newest_session_and_its_read_time():
    _seed_bars("SPY", "2026-10-02", _close_plus(40))
    _seed_bars("AAA", "2026-10-01", 123.0)
    got = ledger.prices_last_bars(t for t in [" spy", "AAA", "MISSING", None])   # callers pass generators
    assert got == {"SPY": ("2026-10-02", _close_plus(40)), "AAA": ("2026-10-01", 123.0)}
    assert ledger.prices_last_bars([]) == {}


def test_no_prices_store_reads_as_no_bars_and_refuses_the_anchor():
    assert ledger.prices_last_bars(["SPY"]) == {}
    assert ledger.prices_has_session(D, "SPY") is False
    assert ledger.spy_anchor_problem(D) == "no SPY bars in the prices store"


def test_a_failed_read_reads_as_no_bars_and_refuses_the_anchor(monkeypatch):
    _seed_bars("SPY", "2026-10-02", _close_plus(45))

    def locked(tickers):
        raise RuntimeError("IO Error: Could not set lock on file prices.duckdb")

    monkeypatch.setattr(market_bars, "last_bars", locked)
    assert ledger.prices_last_bars(["SPY"]) == {}
    assert ledger.spy_anchor_problem(D) == "no SPY bars in the prices store"


def test_the_picks_own_bars_are_read_from_the_store_the_board_scored_from(rawstore_tmp_db):
    """A stale old cache no longer refuses a pick whose session bar is complete, and a pick the prices store lacks is
    refused whatever the cache holds. Option A (avo 2026-10-03) is unchanged."""
    _seed_bars("AAA", "2026-10-02", _close_plus(40))
    _seed_cache("AAA", "2026-10-01", _close_plus(-24 * 60))
    _seed_cache("BBB", "2026-10-02", _close_plus(45))
    seed_bars("BBB", ["2026-09-30", "2026-10-01"], fetched_at=_close_plus(-24 * 60), replace=True)
    assert ledger.ohlc_gaps(D, ["AAA", "BBB"]) == {"BBB": ledger.NEEDS_REFETCH}
