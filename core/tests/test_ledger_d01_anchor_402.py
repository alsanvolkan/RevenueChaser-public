"""U1 (#402, K8b's SPY part): the SPY anchor of the Swing and Fundamentals freeze reads D01 first.

On 2026-10-14 RC-Daily-Bars had SPY's session bar in ``price_bars_daily``, but the freeze read SPY from the old
``swing_ohlc`` cache, whose refetch had failed, and refused both boards. Offline; the store is a temp DuckDB.
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import ledger, rawstore

D = date(2026, 10, 2)          # a Friday and a normal session


def _close_plus(minutes, day=D):
    return cal.session_close(day).timestamp() + minutes * 60


def _seed_cache(ticker, last_day, cached_at):
    rawstore.write_rows("swing_ohlc", [
        {"ticker": ticker, "ts": f"{d}T00:00:00", "open": 1.0, "high": 2.0, "low": 0.5,
         "close": 1.5, "volume": 100.0, "cached_at": cached_at}
        for d in ("2026-09-30", "2026-10-01", last_day)])


def _seed_d01(ticker, last_day, fetched_at):
    rawstore.write_rows("price_bars_daily", [
        {"ticker": ticker, "session_date": date.fromisoformat(d), "open": 1.0, "high": 2.0, "low": 0.5,
         "close": 1.5, "adj_close": None, "volume": 100.0, "split_ratio": None, "adj_source": None,
         "source": "uw", "fetched_at": fetched_at, "run_id": "seed", "run_at": fetched_at}
        for d in ("2026-09-30", "2026-10-01", last_day)])


def test_a_complete_d01_session_bar_anchors_the_board_when_the_cache_is_stale(rawstore_tmp_db):
    """The 14 Oct shape: the cache's refetch failed (its SPY ends the day before), D01 has the session."""
    _seed_cache("SPY", "2026-10-01", _close_plus(-24 * 60))
    _seed_d01("SPY", "2026-10-02", _close_plus(40))
    assert ledger.spy_anchor_problem(D) is None


def test_with_no_spy_in_the_cache_at_all_d01_still_anchors_the_board(rawstore_tmp_db):
    """Common review K8b's red test: a store with only ``price_bars_daily`` for SPY still freezes."""
    _seed_d01("SPY", "2026-10-02", _close_plus(40))
    assert ledger.spy_anchor_problem(D) is None


def test_when_d01_lacks_the_session_the_old_cache_check_decides_with_its_old_reasons(rawstore_tmp_db):
    _seed_d01("SPY", "2026-10-01", _close_plus(40, date(2026, 10, 1)))
    assert "no SPY" in ledger.spy_anchor_problem(D)
    _seed_cache("SPY", "2026-10-01", _close_plus(45))
    assert "SPY's last bar is 2026-10-01" in ledger.spy_anchor_problem(D)
    _seed_cache("SPY", "2026-10-02", _close_plus(-270))
    assert "before the close" in ledger.spy_anchor_problem(D)
    _seed_cache("SPY", "2026-10-02", _close_plus(31))
    assert ledger.spy_anchor_problem(D) is None


def test_a_d01_session_bar_written_before_the_close_plus_30_is_not_the_session(rawstore_tmp_db):
    """D01 writes only complete sessions; a bar stamped earlier is not trusted and the cache decides."""
    _seed_cache("SPY", "2026-10-01", _close_plus(45))
    _seed_d01("SPY", "2026-10-02", _close_plus(29))
    assert "SPY's last bar is 2026-10-01" in ledger.spy_anchor_problem(D)
    assert ledger.d01_has_session(D, "SPY") is False


def test_a_d01_bar_later_than_the_session_is_not_the_session(rawstore_tmp_db):
    _seed_d01("SPY", "2026-10-05", _close_plus(40, date(2026, 10, 5)))
    assert ledger.d01_has_session(D, "SPY") is False


def test_d01_last_bars_has_the_shape_of_last_bars(rawstore_tmp_db):
    _seed_d01("SPY", "2026-10-02", _close_plus(40))
    _seed_d01("AAA", "2026-10-01", 123.0)
    got = ledger.d01_last_bars([" spy", "AAA", "MISSING", None])
    assert got == {"SPY": ("2026-10-02", _close_plus(40)), "AAA": ("2026-10-01", 123.0)}
    assert ledger.d01_last_bars([]) == {}


def test_no_price_file_reads_as_no_d01_bars(isolate_rawstore):
    """A store without prices.duckdb (or without the table) is the old behaviour, never an error."""
    assert ledger.d01_last_bars(["SPY"]) == {}
    assert ledger.d01_has_session(D, "SPY") is False


def test_a_failed_d01_read_reads_as_no_bars(rawstore_tmp_db, monkeypatch):
    real = rawstore.query

    def query(sql, params=None, read_only=True, db="raw"):
        if "price_bars_daily" in sql:
            raise RuntimeError("IO Error: Could not set lock on file prices.duckdb")
        return real(sql, params, read_only=read_only, db=db)

    monkeypatch.setattr(rawstore, "query", query)
    _seed_cache("SPY", "2026-10-02", _close_plus(45))
    assert ledger.d01_last_bars(["SPY"]) == {}
    assert ledger.spy_anchor_problem(D) is None             # the old cache check, which passes here


def test_the_picks_own_bars_are_read_from_d01_the_board_scored_from(rawstore_tmp_db):
    """Since C12 and K8b (#383 R3) the boards score their picks from D01, so the picks' check reads D01 too: a stale
    cache no longer refuses a pick whose D01 session bar is complete, and a pick D01 lacks is refused whatever the
    cache holds. Option A (avo 2026-10-03) is unchanged, on D01."""
    _seed_d01("AAA", "2026-10-02", _close_plus(40))
    _seed_cache("AAA", "2026-10-01", _close_plus(-24 * 60))
    _seed_cache("BBB", "2026-10-02", _close_plus(45))
    _seed_d01("BBB", "2026-10-01", _close_plus(-24 * 60))
    assert ledger.ohlc_gaps(D, ["AAA", "BBB"]) == {"BBB": ledger.NEEDS_REFETCH}
