"""U1 (#402, K8b's SPY part), on the prices store since PR 10 (G4): the Swing and Fundamentals SPY baseline.

``core.screen_common.load_spy`` reads SPY's total-return series from the prices store (``prices.duckdb``) and marks it
stale unless the newest bar is one complete latest session. The old fallback (``swing_ohlc`` through yfinance) is gone
(F4 A, part 1): no SPY bars is an empty, stale frame. Offline; the store is seeded directly, the clock is the calendar
seam.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import ledger, market_bars, rawstore, screen_common
from RevenueChaser.rcdata.tests.prices_seed import seed_bars, seed_factor

D = date(2026, 10, 14)                                         # the 14 Oct session (#402)
NOW = datetime(2026, 10, 14, 21, 45, tzinfo=timezone.utc)      # 17:45 EDT, after the daily bars
FETCHED = datetime(2026, 10, 14, 20, 40, tzinfo=timezone.utc).timestamp()   # 16:40 EDT, after close + 30 min


def _sessions(end: date, n: int) -> list[date]:
    return [d.date() for d in pd.bdate_range(end=end, periods=n)]


def _seed(ticker: str, days: list[date], *, fetched_at: float = FETCHED, close: float = 400.0):
    seed_bars(ticker, days, fetched_at=fetched_at, close=close,
              ohlc={d: (close + i, (close + i) * 1.01, (close + i) * 0.99, close + i) for i, d in enumerate(days)})


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(cal, "_now", lambda: NOW)


def test_the_baseline_is_the_prices_stores_series_through_the_latest_complete_session(clock):
    _seed("SPY", _sessions(D, 300))
    spy = screen_common.load_spy()
    assert spy.index[-1] == pd.Timestamp(D)
    assert list(spy.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert spy["Close"].iloc[-1] == 400.0 + 299
    assert spy.attrs["stale"] is False and spy.attrs["source"] == "prices"


def test_a_store_a_day_behind_is_a_stale_baseline_not_a_fallback(clock):
    _seed("SPY", _sessions(D - timedelta(days=1), 300))
    spy = screen_common.load_spy()
    assert spy.index[-1] == pd.Timestamp(D - timedelta(days=1)) and spy.attrs["stale"] is True


def test_no_spy_in_the_store_is_an_empty_stale_baseline(clock):
    _seed("QQQ", _sessions(D, 10))
    spy = screen_common.load_spy()
    assert spy.empty and spy.attrs["stale"] is True


def test_a_session_bar_read_before_the_close_plus_30_is_stale(clock):
    early = datetime(2026, 10, 14, 20, 29, tzinfo=timezone.utc).timestamp()
    _seed("SPY", _sessions(D, 300), fetched_at=early)
    assert screen_common.load_spy().attrs["stale"] is True


def test_a_failed_read_is_an_empty_stale_baseline(clock, monkeypatch):
    _seed("SPY", _sessions(D, 300))

    def boom(*a, **k):
        raise RuntimeError("IO Error: Could not set lock on file prices.duckdb")

    monkeypatch.setattr(market_bars, "daily_bars", boom)
    spy = screen_common.load_spy()
    assert spy.empty and spy.attrs["stale"] is True


def test_a_run_the_calendar_cannot_place_is_an_empty_stale_baseline(monkeypatch):
    monkeypatch.setattr(cal, "_now", lambda: datetime(2099, 1, 5, 23, 0, tzinfo=timezone.utc))
    spy = screen_common.load_spy()
    assert spy.empty and spy.attrs["stale"] is True


def test_the_baseline_is_dividend_adjusted_like_yfinance_auto_adjust(clock):
    """yfinance ``auto_adjust=True`` (the old read) scaled the bars before an ex-date; the prices store's total-return
    factor does the same."""
    days = _sessions(D, 300)
    seed_bars("SPY", days, fetched_at=FETCHED, close=400.0)
    ex = days[-10]
    seed_factor("SPY", 1.0, valid_to=ex, tr_factor=1 - 4.0 / 400.0)          # a $4 dividend ex on ``ex``
    spy = screen_common.load_spy()
    assert spy.loc[pd.Timestamp(days[-11]), "Close"] == pytest.approx(400.0 * (1 - 4.0 / 400.0))
    assert spy.loc[pd.Timestamp(ex), "Close"] == pytest.approx(400.0)


def test_the_baseline_window_is_the_old_two_years(clock):
    _seed("SPY", _sessions(D, 700))
    spy = screen_common.load_spy()
    assert spy.index[0] >= pd.Timestamp(D - timedelta(days=screen_common.SPY_BASELINE_DAYS))
    assert len(spy) >= 252 + 1                                  # enough for the 12-month RS and the 200-day mean


def test_the_baseline_and_the_freeze_anchor_agree(clock):
    """Both use ``ledger.prices_has_session``: a fresh baseline is never refused by the anchor, and back."""
    _seed("SPY", _sessions(D - timedelta(days=1), 5))
    assert screen_common.load_spy().attrs["stale"] is True and ledger.spy_anchor_problem(D) is not None
    _seed("SPY", _sessions(D, 5))
    assert screen_common.load_spy().attrs["stale"] is False and ledger.spy_anchor_problem(D) is None


# ── the two boards are wired to it ──────────────────────────────────────────

@pytest.mark.parametrize("board", ["swing", "fundamentals"])
def test_each_board_takes_its_spy_baseline_from_the_prices_store(rawstore_tmp_db, clock, monkeypatch, board):
    """The board's ``load_spy`` hook goes through ``screen_common.load_spy``: with yfinance failing and SPY's old cache
    a day old, the baseline is the prices store's, not stale, and yfinance is never asked."""
    from RevenueChaser.module_fundamentals import pipeline as fp
    from RevenueChaser.module_swing_potential import pipeline as sp
    mod = {"swing": sp, "fundamentals": fp}[board]
    asked = []

    class _Failing:
        def __init__(self, t):
            asked.append(t)

        def history(self, *a, **k):
            raise RuntimeError("HTTP Error 401: Invalid Crumb")

    monkeypatch.setattr("yfinance.Ticker", _Failing)
    rawstore.write_rows("swing_ohlc", [{
        "ticker": "SPY", "ts": f"{d.isoformat()}T00:00:00", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
        "volume": 1.0, "cached_at": datetime(2026, 10, 13, 21, 0, tzinfo=timezone.utc).timestamp()}
        for d in _sessions(D - timedelta(days=1), 300)])
    _seed("SPY", _sessions(D, 300))
    seen = {}

    def run_screen(**kw):
        seen["spy"] = kw["load_spy"]()
        return {}

    monkeypatch.setattr(mod.screen_common, "run_screen", run_screen)
    mod.run_scan()
    assert seen["spy"].attrs["stale"] is False
    assert seen["spy"].index[-1] == pd.Timestamp(D)
    assert asked == []
