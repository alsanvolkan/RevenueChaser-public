"""Daily bar normalisation shared by every OHLC consumer (review X2, plan MB4).

Unusual Whales' candle endpoints return up to three rows per trading day —
pre-market (``market_time == "pr"``), regular (``"r"``) and after-hours
(``"po"``) — and sometimes newest-first. Treating each row as a day made the
Earnings realized vol, the Rotation trail/backfill and every "last N days"
window wrong. This is the one place that turns raw rows into one
regular-session bar per trading day, oldest first.
"""
from __future__ import annotations

import math

REGULAR = "r"
EXTENDED = {"pr", "po"}


def _finite(v) -> bool:
    if v is None:
        return False
    try:
        return not math.isnan(float(v))
    except (TypeError, ValueError):
        return False


def _day(row: dict) -> str:
    return str(row.get("date") or row.get("datetime") or "")[:10]


def regular_session_bars(rows, sessions_only: bool = True) -> list[dict]:
    """One bar per date, oldest first.

    A row with ``market_time == "r"`` wins its date; a row without the field is
    treated as regular (other feeds, stored rows). With ``sessions_only`` a date
    that has only pre-/after-hours rows — typically today before the open — is
    dropped rather than passed off as a completed session. Rows without a date
    or a finite close are skipped. The row dicts are returned unchanged (only
    ``date`` is filled from ``datetime`` when missing).
    """
    by_date: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        d = _day(row)
        if not d or not _finite(row.get("close")):
            continue
        mt = row.get("market_time")
        extended = mt in EXTENDED
        if extended and sessions_only:
            continue
        prev = by_date.get(d)
        if prev is None or prev.get("market_time") in EXTENDED or not extended:
            by_date[d] = row if row.get("date") else {**row, "date": d}
    return [by_date[d] for d in sorted(by_date)]


__all__ = ["regular_session_bars"]
