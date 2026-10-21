"""E0d, D27 (#305, the FRED part): eight daily FRED series, stored by observation date.

One request per series (8 a night, inside the FRED cap of 10), the newest ``LIMIT`` observations each; the first
write of (series_id, obs_date, source) wins, so a night adds only the observations it has not stored. ``session_date``
is the New York session on or before ``obs_date`` (None outside the holiday table). D27 is owned here (E0); Rotation
2.10 adopts this writer for its own series (yfinance proxies carry ``proxy_for``; none here).
"""
from __future__ import annotations

import time
from datetime import date

from RevenueChaser.core import calendar, nightly_inputs
from RevenueChaser.module_earnings_radar import macro_watch
from RevenueChaser.module_earnings_radar.inputs._common import CALL_CAP, error_of, outcome

TABLE = "macro_series"
SERIES = ("DGS2", "DGS5", "DGS10", "DGS20", "DGS30", "T10Y2Y", "T10YIE", "DCOILWTICO")
LIMIT = 80


def _session_on_or_before(d: date) -> date | None:
    try:
        return calendar.previous_trading_day(d)
    except ValueError:
        return None


def run(ctx) -> dict:
    if not macro_watch.FRED_API_KEY:
        return outcome(len(SERIES), ["missing_key"] * len(SERIES), **nightly_inputs.record_first(TABLE, []))
    budget = ctx.budget("fred")
    rows, errors = [], []
    for series_id in SERIES:
        if not budget.take():
            errors.append(CALL_CAP)
            continue
        res = macro_watch.fred_daily_result(series_id, limit=LIMIT)
        if res.status != "ok":
            errors.append(error_of(res))
            continue
        fetched = time.time()
        for obs, value in res.data:
            d = date.fromisoformat(obs)
            rows.append({"series_id": series_id, "obs_date": d, "source": "fred", "value": value,
                         "frequency": "daily", "proxy_for": None, "session_date": _session_on_or_before(d),
                         **ctx.stamps(fetched)})
    return outcome(len(SERIES), errors, **nightly_inputs.record_first(TABLE, rows))


SWEEP = nightly_inputs.Sweep("D27", (TABLE,), run)
