"""Watchlist augmentation of the earnings calendar (one helper for the CLI and the dashboard Fetch).

A watchlist ticker whose earnings time is "unknown" or whose date is only an estimate is
missing from the ``/earnings/{premarket,afterhours}`` session calendar. The screener row
carries its ``next_earnings_date``; those names are added to the run so a watchlist name
never silently drops off the board (#198 E15).
"""
from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)


def _augment_watchlist(events: list, raw_cal: dict, wl: set, screener: dict, start: date, end: date) -> None:
    """Add watchlist tickers reporting in [start,end] but missing from the session calendar.

    Covers tickers whose earnings time is "unknown" or whose date is only an
    estimate, which the /earnings/{premarket,afterhours} calendar omits.
    """
    for t, s in screener.items():
        if t in raw_cal:
            continue
        nd = s.get("next_earnings_date")
        if not nd:
            continue
        try:
            ed = date.fromisoformat(str(nd)[:10])
        except ValueError:
            continue
        if not (start <= ed <= end):
            continue

        er_time = (s.get("er_time") or "").lower()
        if er_time in ("premarket", "bmo", "before"):
            time_label = "BMO"
        elif er_time in ("postmarket", "afterhours", "amc", "after"):
            time_label = "AMC"
        else:
            time_label = "Other"

        ev = {
            "ticker": t,
            "earnings_date": ed,
            "earnings_time": time_label,
            "sector": s.get("sector", ""),
            "full_name": s.get("full_name", ""),
            "marketcap": s.get("marketcap", ""),
            "expected_move_perc": s.get("implied_move_perc"),
            "street_mean_est": "",
            "report_time": s.get("er_time", ""),
            "source": "screener",
        }
        events.append(ev)
        raw_cal[t] = ev


def augment_calendar(uw, events: list, raw_cal: dict, wl: set, start: date, end: date, days=None) -> None:
    """Add the watchlist names reporting in ``[start, end]`` that the calendar lacks.

    ``days`` (optional) narrows that to a discrete day list. One screener batch for the
    missing names; a failure is logged and the run goes on without the extra names.
    """
    missing = [t for t in sorted(wl or ()) if t not in raw_cal]
    if not missing:
        return
    try:
        before = {e["ticker"] for e in events}
        _augment_watchlist(events, raw_cal, wl, uw.screener_batch(missing), start, end)
        if days is not None:
            allowed = set(days)
            for ev in [e for e in events if e["ticker"] not in before and e["earnings_date"] not in allowed]:
                events.remove(ev)
                raw_cal.pop(ev["ticker"], None)
    except Exception as e:  # noqa: BLE001 - the calendar run must not die on the extras
        logger.warning(f"Watchlist augmentation failed: {e}")
