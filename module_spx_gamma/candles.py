"""SPX daily candles for the 8-week price window.

The rows come from the store: ``index_levels_daily``, written once a night by ``core/index_levels.py`` from yfinance
``^GSPC`` (UW index OHLC is permission-denied, G2-06; D03). The snapshot reads here and never calls Yahoo itself, so
a snapshot run costs no candle request and cannot mix an unfinished session into the window (rule B: only complete
sessions are stored).
"""
from __future__ import annotations

from datetime import date, timedelta

from RevenueChaser.core import index_levels

from .config import LOOKBACK_WEEKS


def week_window_start(asof: date | str) -> date:
    """Monday of the week ``LOOKBACK_WEEKS`` weeks before the as-of week.

    The as-of week is floored to its Monday, so any day within week N maps to
    the same start (week N - LOOKBACK_WEEKS).
    """
    asof_date = asof if isinstance(asof, date) else date.fromisoformat(asof)
    monday = asof_date - timedelta(days=asof_date.weekday())
    return monday - timedelta(weeks=LOOKBACK_WEEKS)


def window_start(asof: date | str) -> date:
    """First date the 8-week window may hold, with the 3-day holiday buffer the window has always carried."""
    return week_window_start(asof) - timedelta(days=3)


def stored_spx_candles(asof: date | str, ticker: str = "^GSPC") -> list[dict]:
    """Stored daily OHLC for ``ticker`` from the window start through ``asof`` (store only, no network).

    Defaults to ``^GSPC`` (the one symbol ``index_levels_daily`` holds). Nothing stored yet means an empty list; the
    snapshot then reports the window as unavailable instead of falling back to a live candle fetch.
    """
    asof_date = asof if isinstance(asof, date) else date.fromisoformat(asof)
    return index_levels.levels_between(window_start(asof_date), asof_date, symbol=ticker)


def window_lag(rows: list[dict], asof: date | str) -> tuple[str | None, str | None]:
    """``(expected_last_session, stored_last_session)`` when the window ends early, else ``(None, None)``.

    The stored table only holds complete sessions (rule B), so a Fetch asked for today legitimately ends on the
    last complete session. It is NOT legitimate for the window to end before that: the nightly
    ``--index-levels`` step missed one or more sessions and the board would show a stale window with a healthy
    source label (#293 review MUST). A calendar that cannot answer reports nothing rather than raising.
    """
    asof_date = asof if isinstance(asof, date) else date.fromisoformat(asof)
    if not rows:
        return None, None                      # the empty case has its own warning
    try:
        from RevenueChaser.core import calendar as nyse_calendar
        expected = nyse_calendar.latest_complete_session()
    except (ValueError, ImportError):
        return None, None
    expected = min(expected, asof_date)
    if not nyse_calendar.is_trading_day(expected):
        expected = nyse_calendar.previous_trading_day(expected)
    last = max(r["date"] for r in rows)
    return (expected.isoformat(), last) if last < expected.isoformat() else (None, None)


def price_window_sessions(rows: list[dict], asof: date | str) -> list[dict]:
    """Completed sessions within the 8-week window ending on (or before) ``asof``."""
    asof_date = asof if isinstance(asof, date) else date.fromisoformat(asof)
    start = week_window_start(asof_date)
    return [
        r for r in rows
        if r.get("close") is not None
        and start.isoformat() <= r["date"] <= asof_date.isoformat()
    ]
