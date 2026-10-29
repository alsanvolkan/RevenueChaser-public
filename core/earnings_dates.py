"""The next earnings date a screener card shows: UW's calendar on the options layer first, then the stored D26
calendar, Yahoo's own value otherwise (#304, G6).

The cards (Asymmetric Growth, Swing, Fundamentals) and their week filter read ``quote["earningsTimestampStart"]``
(epoch seconds). The stored calendar (``earnings_calendar``, the nightly D26 sweep) covers about a third of the names
Yahoo has a date for, so a name the store has no date for keeps Yahoo's value: a hard switch would drop it from the
week filter. The overlay only changes the row a score reads, never the stored universe row.

Since G6 the first source is ``options.duckdb``'s ``option_screener_daily`` (``uw/option_screener``, G8): the same UW
calendar endpoints, every name reporting in the 28 days after its session. Its newest session is read when it is no
older than ``MAX_AGE_DAYS``; otherwise (no file yet, a stale or future session, a failed read) the D26 store is read.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from RevenueChaser.core import calendar, rawstore

logger = logging.getLogger(__name__)

# A stored calendar older than this (calendar days) is not read: a stale sweep must not outrank Yahoo.
MAX_AGE_DAYS = 4


def stored_next_dates(today: date | None = None) -> dict[str, date]:
    """``{ticker: next earnings date on or after today}`` from the newest stored calendar session, ``{}`` when the store
    has none, a stale one, or cannot be read."""
    today = today or calendar.today_ny()      # the New York date: after midnight in Amsterdam it is still yesterday there
    layer = _layer_next_dates(today)
    if layer is not None:
        return layer
    try:
        rows = rawstore.query(
            "SELECT ticker, min(earnings_date) AS d FROM earnings_calendar "
            "WHERE session_date = (SELECT max(session_date) FROM earnings_calendar "
            "WHERE session_date <= ? AND session_date >= ?) AND earnings_date >= ? GROUP BY ticker",
            [today, today - timedelta(days=MAX_AGE_DAYS), today], db="raw")
    except Exception as exc:  # noqa: BLE001 - a missing table or an unreadable store leaves Yahoo's value in place
        logger.warning("stored earnings dates could not be read (%s); the cards keep Yahoo's dates", exc)
        return {}
    return {str(r["ticker"]).strip().upper(): r["d"] for r in rows if r.get("ticker") and r.get("d")}


def _layer_next_dates(today: date) -> dict[str, date] | None:
    """``{ticker: coming report date, on or after today}`` from the options layer's newest screener session, ``None``
    when that session is missing, older than ``MAX_AGE_DAYS`` or after ``today``, or the file cannot be read."""
    try:
        from RevenueChaser.rcdata import read
        rows = read.option_screener()
    except Exception as exc:  # noqa: BLE001 - the D26 store is the fallback, as before G6
        logger.warning("the options layer's earnings calendar could not be read (%s); the stored D26 calendar is "
                       "read", exc)
        return None
    session = max((r["session_date"] for r in rows if r.get("session_date")), default=None)
    if session is None or not today - timedelta(days=MAX_AGE_DAYS) <= session <= today:
        return None
    # One row per session and ticker (the table's key), so a name has one report date in the session.
    return {str(r["ticker"]).strip().upper(): r["report_date"] for r in rows
            if r.get("ticker") and r.get("report_date") and r["report_date"] >= today}


def epoch(day: date) -> float:
    """Noon UTC of ``day``: the same calendar date in every timezone the week filter can run in."""
    return datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).timestamp()


def with_stored_date(row: dict, stored: dict[str, date]) -> dict:
    """``row`` with the stored date in its quote, or ``row`` itself when the store has none for the ticker."""
    day = stored.get(str(row.get("ticker") or "").strip().upper())
    if day is None:
        return row
    return {**row, "quote": {**(row.get("quote") or {}), "earningsTimestampStart": epoch(day)}}
