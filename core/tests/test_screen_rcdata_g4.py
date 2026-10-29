"""PR 10 (G4): Swing and Fundamentals read their bars, SPY's baseline and the freeze anchor from the prices store
(``prices.duckdb`` through ``rcdata.read``); the ``swing_ohlc`` SPY fallback and ``ledger.last_bars`` are gone (F4 A,
part 1)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from RevenueChaser.core import calendar, ledger, rawstore, screen_common
from RevenueChaser.rcdata.tests.prices_seed import seed_bars, seed_factor

SESSION = date(2026, 10, 14)
NOW = datetime(2026, 10, 14, 21, 45, tzinfo=timezone.utc)                      # 17:45 EDT: the session is complete
AFTER = datetime(2026, 10, 14, 20, 40, tzinfo=timezone.utc).timestamp()        # read after the close + 30 min
BEFORE = datetime(2026, 10, 14, 19, 0, tzinfo=timezone.utc).timestamp()        # 15:00 EDT: during the session
DAYS = [d.date() for d in pd.bdate_range(end=SESSION, periods=5)]


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


def _complete_spy_in_swing_ohlc():
    """What the old fallback read: a complete SPY series in the shared OHLC cache."""
    rawstore.write_rows("swing_ohlc", [{"ticker": "SPY", "ts": f"{d.isoformat()}T00:00:00", "open": 1.0, "high": 2.0,
                                         "low": 0.5, "close": 1.5, "volume": 100.0, "cached_at": AFTER}
                                        for d in DAYS])


def test_the_anchor_reads_the_prices_store_and_never_the_old_ohlc_cache(rawstore_tmp_db):
    _complete_spy_in_swing_ohlc()
    assert ledger.spy_anchor_problem(SESSION) == "no SPY bars in the prices store"
    seed_bars("SPY", DAYS, fetched_at=AFTER)
    assert ledger.spy_anchor_problem(SESSION) is None


def test_the_anchor_refuses_a_bar_read_before_the_close_and_an_older_session():
    seed_bars("SPY", DAYS, fetched_at=BEFORE)
    assert "before the close + 30 min" in ledger.spy_anchor_problem(SESSION)
    seed_bars("SPY", DAYS[:-1], fetched_at=AFTER)
    assert ledger.spy_anchor_problem(SESSION) == f"SPY's last bar is {DAYS[-2]}, not the {SESSION} session"


def test_the_old_cache_reader_is_gone():
    assert not hasattr(ledger, "last_bars")


def test_load_spy_is_the_prices_stores_total_return_series_and_takes_no_fallback(rawstore_tmp_db):
    _complete_spy_in_swing_ohlc()
    empty = screen_common.load_spy()
    assert empty.empty and empty.attrs["stale"] is True                # no store: no baseline, never the old cache
    seed_bars("SPY", DAYS, fetched_at=AFTER, close=10.0)
    seed_factor("SPY", 0.9, valid_to=DAYS[-1])                           # a dividend after every bar but the last
    frame = screen_common.load_spy()
    assert list(frame["Close"]) == pytest.approx([9.0, 9.0, 9.0, 9.0, 10.0])
    assert frame.attrs["stale"] is False and frame.attrs["source"] == "prices"
    seed_bars("SPY", DAYS, fetched_at=BEFORE, close=10.0)
    assert screen_common.load_spy().attrs["stale"] is True               # a bar read during the session


def test_a_scored_names_bars_come_from_the_prices_store():
    seed_bars("UP1", DAYS, fetched_at=AFTER, close=40.0)
    seed_factor("UP1", 0.5, valid_to=DAYS[2])                            # a 2-for-1 split ex the 3rd day
    nominal, adjusted = screen_common.load_bars_many(["up1"])["UP1"]
    assert list(nominal["Close"]) == [20.0, 20.0, 40.0, 40.0, 40.0]      # the boards' "nominal": split-adjusted
    assert list(adjusted["Close"]) == [20.0, 20.0, 40.0, 40.0, 40.0]
    assert nominal.attrs["stale"] is False
    assert screen_common.load_bars_many(["NOPE"])["NOPE"][0].empty


def test_stale_names_and_the_reference_session_read_the_prices_store():
    seed_bars("SPY", DAYS, fetched_at=AFTER)
    seed_bars("OLD", DAYS[:-1], fetched_at=AFTER)
    for t in ("A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T"):
        seed_bars(t, DAYS, fetched_at=AFTER)
    assert screen_common.bars_reference_session() == SESSION
    names = ["OLD"] + [chr(c) for c in range(ord("A"), ord("U"))]
    assert screen_common.stale_names(names, SESSION) == {"OLD"}


def test_asymmetric_growths_freeze_keeps_reading_the_bars_its_board_scores_from(rawstore_tmp_db):
    """AG's engine still reads D01 until PR 11 moves it, so its freeze checks (``d01_has_session``) stay on D01 while
    Swing's and Fundamentals' move to the prices store: each board's freeze checks the bars that board scored from."""
    from RevenueChaser.core.tests.d01_seed import seed_d01
    seed_bars("SPY", DAYS, fetched_at=AFTER)                             # the prices store only
    assert ledger.prices_has_session(SESSION, "SPY") and not ledger.d01_has_session(SESSION, "SPY")
    seed_d01("SPY", DAYS, AFTER)                                         # both stores
    assert ledger.d01_has_session(SESSION, "SPY")
