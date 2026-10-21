"""Earnings reaction anchors: which regular-session close a print is measured from.

Pure functions over a list of regular-session bars (``{"date", "close"}`` dicts), so
the bar source (UW today, the D01 daily closes later) can change without touching the
rule. Promoted from ``scripts/research/34_spillover_edges.py`` (#197 E3).

A report is digested by the close of its *reaction session*: the report date for a
before-the-open print (BMO), the next session for an after-the-close print (AMC).
Measuring an AMC print from the report-date close (the pre-report close) overstates
the move by the whole gap.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

_BMO = frozenset({"premarket", "pre-market", "bmo", "before", "am"})
_AMC = frozenset({"afterhours", "after-hours", "postmarket", "post-market", "amc", "after", "pm"})


def timing_kind(timing) -> Optional[str]:
    """``"bmo"``, ``"amc"`` or None (unknown / unconfirmed) for a provider timing label."""
    t = str(timing or "").lower().strip()
    if t in _BMO:
        return "bmo"
    if t in _AMC:
        return "amc"
    return None


def session_closes(bars) -> tuple[list[str], dict[str, float]]:
    """``(sorted session dates, {date: close})`` from bars; bars without a close are skipped."""
    by_date: dict[str, float] = {}
    for c in bars or []:
        d = str(c.get("date") or c.get("datetime") or "")[:10]
        close = c.get("close")
        if d and close:
            by_date[d] = float(close)
    return sorted(by_date), by_date


def reaction_anchor(bars, report_date: str, timing) -> Optional[tuple[str, float]]:
    """``(session date, close)`` of the print's reaction session, or None.

    - BMO: the first session on or after the report date.
    - AMC: the first session strictly after the report date (None while that session
      has no bar yet).
    - unknown timing: the first session on or after the report date (legacy read).
    - None when the report predates the first bar: a report older than the window must
      not silently anchor on the window's first bar.
    """
    dates, by_date = session_closes(bars)
    rd = str(report_date or "")[:10]
    if not dates or not rd or rd < dates[0]:
        return None
    strictly_after = timing_kind(timing) == "amc"
    for d in dates:
        if d > rd or (d == rd and not strictly_after):
            return d, by_date[d]
    return None


def reaction_session(acc: str, dates):
    """acceptanceDateTime (UTC ISO string) -> ``(session index in dates or None, tag, ET datetime)``.

    after 16:00 ET: the next session; before 09:30 ET: the same session; in between: the same session, tagged
    "intraday". A filing on a non-session day (weekend / holiday) goes to the next session. ``dates`` is a sorted
    ``datetime64[D]`` array (the SPY sessions).
    """
    import numpy as np

    dt = datetime.fromisoformat(acc.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    et = dt.astimezone(ET)
    d = np.datetime64(et.date().isoformat(), "D")
    if d < dates[0]:
        return None, "before_history", et
    t = et.time()
    if t > dtime(16, 0, 0):
        i, tag = int(np.searchsorted(dates, d, side="right")), "afterclose"
    elif t < dtime(9, 30, 0):
        i, tag = int(np.searchsorted(dates, d, side="left")), "premarket"
    else:
        i, tag = int(np.searchsorted(dates, d, side="left")), "intraday"
    return (i if i < len(dates) else None), tag, et
