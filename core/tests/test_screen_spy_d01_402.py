"""U1 (#402, K8b's SPY part): the Swing and Fundamentals SPY baseline reads D01 first.

``core.screen_common.load_spy`` takes SPY from ``price_bars_daily`` when D01 holds the latest complete session, and
falls back to the old read (``swing_ohlc`` through yfinance) only when it does not. Offline; the store is a temp
DuckDB, the clock is the calendar seam.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import ledger, rawstore, screen_common

D = date(2026, 10, 14)                                         # the 14 Oct session (#402)
NOW = datetime(2026, 10, 14, 21, 45, tzinfo=timezone.utc)      # 17:45 EDT, after RC-Daily-Bars
FETCHED = datetime(2026, 10, 14, 20, 40, tzinfo=timezone.utc).timestamp()   # 16:40 EDT, after close + 30 min


def _sessions(end: date, n: int) -> list[date]:
    return [d.date() for d in pd.bdate_range(end=end, periods=n)]


def _seed_d01(ticker: str, days: list[date], *, fetched_at: float = FETCHED, close=None):
    rawstore.write_rows("price_bars_daily", [{
        "ticker": ticker, "session_date": d, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
        "adj_close": None, "volume": 1.0e6, "split_ratio": None, "adj_source": None, "source": "uw",
        "fetched_at": fetched_at, "run_id": "seed", "run_at": fetched_at}
        for i, d in enumerate(days) for c in [close(i) if close else 400.0 + i]])


class _Fallback:
    """The old read: counts its calls and returns a frame marked stale, like a failed refetch."""

    def __init__(self):
        self.calls = 0
        self.frame = pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1.0]},
                                  index=pd.DatetimeIndex([pd.Timestamp("2026-10-13")]))
        self.frame.attrs["stale"] = True

    def __call__(self):
        self.calls += 1
        return self.frame


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(cal, "_now", lambda: NOW)


def test_the_baseline_reads_d01_when_it_holds_the_latest_complete_session(rawstore_tmp_db, clock):
    _seed_d01("SPY", _sessions(D, 300))
    old = _Fallback()
    spy = screen_common.load_spy(old)
    assert old.calls == 0
    assert spy.index[-1] == pd.Timestamp(D)
    assert list(spy.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert spy["Close"].iloc[-1] == 400.0 + 299
    assert not spy.attrs.get("stale")
    assert spy.attrs.get("source") == "d01"


def test_the_baseline_falls_back_when_d01_lacks_the_session(rawstore_tmp_db, clock):
    _seed_d01("SPY", _sessions(D - timedelta(days=1), 300))
    old = _Fallback()
    spy = screen_common.load_spy(old)
    assert old.calls == 1
    assert spy is old.frame and spy.attrs["stale"] is True       # the old read, stale flag and all


def test_the_baseline_falls_back_with_no_spy_in_d01(rawstore_tmp_db, clock):
    _seed_d01("QQQ", _sessions(D, 10))
    old = _Fallback()
    assert screen_common.load_spy(old) is old.frame


def test_the_baseline_falls_back_when_the_d01_bar_was_written_before_the_close_plus_30(rawstore_tmp_db, clock):
    early = datetime(2026, 10, 14, 20, 29, tzinfo=timezone.utc).timestamp()
    _seed_d01("SPY", _sessions(D, 300), fetched_at=early)
    old = _Fallback()
    assert screen_common.load_spy(old) is old.frame


def test_a_failed_d01_read_falls_back(rawstore_tmp_db, clock, monkeypatch):
    from RevenueChaser.core import prices
    _seed_d01("SPY", _sessions(D, 300))

    def boom(*a, **k):
        raise RuntimeError("IO Error: Could not set lock on file prices.duckdb")

    monkeypatch.setattr(prices, "daily_bars", boom)
    old = _Fallback()
    assert screen_common.load_spy(old) is old.frame


def test_a_run_the_calendar_cannot_place_falls_back(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr(cal, "_now", lambda: datetime(2099, 1, 5, 23, 0, tzinfo=timezone.utc))
    old = _Fallback()
    assert screen_common.load_spy(old) is old.frame


def test_the_baseline_is_dividend_adjusted_like_yfinance_auto_adjust(rawstore_tmp_db, clock):
    """yfinance ``auto_adjust=True`` (the old read) scales the bars before an ex-date; so does D01's adjusted basis."""
    days = _sessions(D, 300)
    _seed_d01("SPY", days, close=lambda i: 400.0)
    ex = days[-10]
    rawstore.write_rows("price_actions", [{
        "ticker": "SPY", "ex_date": ex, "action": "dividend", "value": 4.0, "session_date": ex,
        "source": "yfinance", "fetched_at": FETCHED, "run_id": "seed", "run_at": FETCHED}])
    spy = screen_common.load_spy(_Fallback())
    assert spy.loc[pd.Timestamp(days[-11]), "Close"] == pytest.approx(400.0 * (1 - 4.0 / 400.0))
    assert spy.loc[pd.Timestamp(ex), "Close"] == pytest.approx(400.0)


def test_the_baseline_window_is_the_old_two_years(rawstore_tmp_db, clock):
    days = _sessions(D, 700)
    _seed_d01("SPY", days)
    spy = screen_common.load_spy(_Fallback())
    assert spy.index[0] >= pd.Timestamp(D - timedelta(days=screen_common.SPY_BASELINE_DAYS))
    assert len(spy) >= 252 + 1                                  # enough for the 12-month RS and the 200-day mean


def test_the_baseline_and_the_freeze_anchor_agree(rawstore_tmp_db, clock):
    """Both use ``ledger.d01_has_session``: a baseline from D01 is never refused by the anchor, and back."""
    old = _Fallback()
    _seed_d01("SPY", _sessions(D - timedelta(days=1), 5))
    assert screen_common.load_spy(old) is old.frame and ledger.d01_has_session(D, "SPY") is False
    _seed_d01("SPY", [D])
    assert screen_common.load_spy(old) is not old.frame
    assert ledger.spy_anchor_problem(D) is None


# ── the two boards are wired to it ──────────────────────────────────────────

@pytest.mark.parametrize("board", ["swing", "fundamentals"])
def test_each_board_takes_its_spy_baseline_from_d01(rawstore_tmp_db, clock, monkeypatch, board):
    """The board's ``load_spy`` hook goes through ``screen_common.load_spy``: with yfinance failing and SPY's cache
    a day old, the baseline is D01's, not stale."""
    import time

    from RevenueChaser.module_fundamentals import pipeline as fp
    from RevenueChaser.module_swing_potential import pipeline as sp
    mod = {"swing": sp, "fundamentals": fp}[board]

    class _Failing:
        def __init__(self, t):
            pass

        def history(self, *a, **k):
            raise RuntimeError("HTTP Error 401: Invalid Crumb")

    monkeypatch.setattr("yfinance.Ticker", _Failing)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    rawstore.write_rows("swing_ohlc", [{
        "ticker": "SPY", "ts": f"{d.isoformat()}T00:00:00", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
        "volume": 1.0, "cached_at": datetime(2026, 10, 13, 21, 0, tzinfo=timezone.utc).timestamp()}
        for d in _sessions(D - timedelta(days=1), 300)])
    _seed_d01("SPY", _sessions(D, 300))
    seen = {}

    def run_screen(**kw):
        seen["spy"] = kw["load_spy"]()
        return {}

    monkeypatch.setattr(mod.screen_common, "run_screen", run_screen)
    mod.run_scan()
    assert not seen["spy"].attrs.get("stale")
    assert seen["spy"].index[-1] == pd.Timestamp(D)
