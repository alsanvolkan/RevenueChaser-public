"""The next earnings date a screener card shows: the stored D26 calendar first, Yahoo's own value otherwise (#304).

The cards (Asymmetric Growth, Swing, Fundamentals) and their week filter read ``quote["earningsTimestampStart"]``
(epoch seconds). The stored calendar (``earnings_calendar``, the nightly D26 sweep) covers about a third of the names
Yahoo has a date for, so a name the store has no date for keeps Yahoo's value: a hard switch would drop it from the
week filter. The overlay only changes the row a score reads, never the stored universe row.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from RevenueChaser.core import rawstore

# A stored calendar older than this (calendar days) is not read: a stale sweep must not outrank Yahoo.
MAX_AGE_DAYS = 4


def stored_next_dates(today: date | None = None) -> dict[str, date]:
    """``{ticker: next earnings date on or after today}`` from the newest stored calendar session, ``{}`` when the store
    has none, a stale one, or cannot be read."""
    today = today or date.today()
    try:
        rows = rawstore.query(
            "SELECT ticker, min(earnings_date) AS d FROM earnings_calendar "
            "WHERE session_date = (SELECT max(session_date) FROM earnings_calendar "
            "WHERE session_date <= ? AND session_date >= ?) AND earnings_date >= ? GROUP BY ticker",
            [today, today - timedelta(days=MAX_AGE_DAYS), today])
    except Exception:  # noqa: BLE001 - a missing table or an unreadable store leaves Yahoo's value in place
        return {}
    return {str(r["ticker"]).strip().upper(): r["d"] for r in rows if r.get("ticker") and r.get("d")}


def epoch(day: date) -> float:
    """Noon UTC of ``day``: the same calendar date in every timezone the week filter can run in."""
    return datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).timestamp()


def with_stored_date(row: dict, stored: dict[str, date]) -> dict:
    """``row`` with the stored date in its quote, or ``row`` itself when the store has none for the ticker."""
    day = stored.get(str(row.get("ticker") or "").strip().upper())
    if day is None:
        return row
    return {**row, "quote": {**(row.get("quote") or {}), "earningsTimestampStart": epoch(day)}}
