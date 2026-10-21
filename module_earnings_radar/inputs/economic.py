"""E0c, D37 (#307): the UW economic calendar (this week and next), stored per run session.

One request a night. Key (session_date, event_ts, event, type); ``event_ts`` is UW's ``time`` (UTC) as epoch seconds.
``forecast``, ``prev`` and ``reported_period`` stay text as served; ``raw_row`` keeps the UW row. A row without a time,
an event or a type has no key and is not stored (counted as ``unkeyed``).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from RevenueChaser.core import nightly_inputs
from RevenueChaser.module_earnings_radar.inputs._common import error_of, outcome, text

TABLE = "economic_calendar"


def event_ts(value) -> float | None:
    """UW's UTC time text (``2026-10-16 12:30:00+00:00`` or ``...Z``) as epoch seconds; None when unreadable."""
    raw = text(value)
    if raw is None:
        return None
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


def event_row(item: dict, ctx, fetched_at: float) -> dict | None:
    ts, event, kind = event_ts(item.get("time")), text(item.get("event")), text(item.get("type"))
    if ts is None or event is None or kind is None:
        return None
    return {"session_date": ctx.session_date, "event_ts": ts, "event": event, "type": kind,
            "forecast": text(item.get("forecast")), "prev": text(item.get("prev")),
            "reported_period": text(item.get("reported_period")), "raw_row": item, "source": "uw",
            **ctx.stamps(fetched_at)}


def run(ctx) -> dict:
    res = ctx.uw().economic_calendar_result()
    if res.status != "ok":
        return outcome(1, [error_of(res)], **nightly_inputs.record_first(TABLE, []))
    fetched = time.time()
    items = [i for i in res.data if isinstance(i, dict)]
    rows = [r for i in items if (r := event_row(i, ctx, fetched)) is not None]
    return outcome(1, [], unkeyed=len(items) - len(rows), **nightly_inputs.record_first(TABLE, rows))


SWEEP = nightly_inputs.Sweep("D37", (TABLE,), run)
