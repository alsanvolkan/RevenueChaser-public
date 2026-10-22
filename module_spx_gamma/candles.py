"""SPX daily candles for the 8-week price window.

The rows come from the store: ``index_levels_daily``, written once a night by ``core/index_levels.py`` from yfinance
``^GSPC`` (UW index OHLC is permission-denied, G2-06; D03). The snapshot reads here and never calls Yahoo itself, so
a snapshot run costs no candle request and cannot mix an unfinished session into the window (rule B: only complete
sessions are stored).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

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
    rows = index_levels.levels_between(window_start(asof_date), asof_date, symbol=ticker)
    # Row order is load-bearing: the snapshot's spot is candles[-1]["close"] and price_window_sessions does not
    # re-sort. Sorting here keeps that true whatever a reader's query does (#293 review S-1).
    rows.sort(key=lambda r: r["date"])
    return rows


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
        expected = min(nyse_calendar.latest_complete_session(), asof_date)
        if not nyse_calendar.is_trading_day(expected):
            # the holiday table starts at 2025; an older as-of must not read as a failed window
            expected = nyse_calendar.previous_trading_day(expected)
    except (ValueError, ImportError):
        return None, None
    last = max(r["date"] for r in rows)
    return (expected.isoformat(), last) if last < expected.isoformat() else (None, None)


def live_spot(ticker: str = "^GSPC", now: datetime | None = None) -> tuple[float | None, str | None]:
    """The live index level during an open US session, as ``(spot, error)``; ``(None, None)`` when closed.

    The 8-week window always comes from the store (no provider call for it). Spot is the exception the owner
    asked for on 2026-10-22: it sets the +/-25% strike window for the UW spot-exposure calls and the board's
    ``current_level``, so during an open session the last COMPLETE close can be a full day stale — measured at
    103 points, 1.33%, on 22 Oct. **One** yfinance request, only while the session is open. A failure returns
    its reason so the caller can fall back to the stored close with a visible warning, never silently.
    """
    from RevenueChaser.core import calendar as nyse_calendar
    try:
        if not nyse_calendar.in_us_session_window(now):
            return None, None                  # market closed: the stored close is the right answer, no warning
    except (ValueError, ImportError) as exc:
        # past the holiday table (2029+) or a broken import: that is not "the market is closed", so say so
        return None, f"session window unknown ({type(exc).__name__}: {exc})"[:200]
    try:
        import yfinance as yf
        frame = yf.Ticker(ticker).history(period="1d", interval="1m", auto_adjust=False)
        closes = [float(v) for v in frame["Close"].tolist() if v == v] if frame is not None else []
        if not closes:
            return None, "no live bar returned"
        return closes[-1], None
    except Exception as exc:  # noqa: BLE001 - the caller falls back to the stored close
        return None, f"{type(exc).__name__}: {exc}"[:200]


def price_window_sessions(rows: list[dict], asof: date | str) -> list[dict]:
    """Completed sessions within the 8-week window ending on (or before) ``asof``."""
    asof_date = asof if isinstance(asof, date) else date.fromisoformat(asof)
    start = week_window_start(asof_date)
    return [
        r for r in rows
        if r.get("close") is not None
        and start.isoformat() <= r["date"] <= asof_date.isoformat()
    ]
