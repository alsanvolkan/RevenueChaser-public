"""E0a, D26 (#304): the UW earnings calendar of the next four weeks, stored per run session.

One request per trading day and report session (``/earnings/premarket`` and ``/earnings/afterhours``), about 40 a
night. The row shape is the Earnings Fetch's (``app/runners.py`` ``_fetch_calendar_days``) without ``earnings_time``,
which follows from ``report_session``; ``item`` keeps the full UW row. Key (session_date, earnings_date, ticker,
report_session); the first write of a key wins, so a second run of the night skips the days it already holds.
UW is the only source stored here: the next-date fallbacks (UW screener, then Yahoo) are a reader rule (2.6-3).
"""
from __future__ import annotations

import time
from datetime import date, timedelta

from RevenueChaser.core import calendar, nightly_inputs, rawstore
from RevenueChaser.module_earnings_radar.inputs._common import error_of, num, outcome, text

TABLE = "earnings_calendar"
SESSIONS = ("premarket", "afterhours")
DAYS_AHEAD = 28


def days_ahead(session: date) -> list[date]:
    """The trading days after ``session`` up to ``DAYS_AHEAD`` calendar days later."""
    out, d, end = [], session, session + timedelta(days=DAYS_AHEAD)
    while (d := calendar.next_trading_day(d)) <= end:
        out.append(d)
    return out


def calendar_row(item: dict, earnings_date: date, report_session: str, ctx, fetched_at: float) -> dict | None:
    ticker = text(item.get("symbol") or item.get("ticker"))
    if not ticker:
        return None
    return {"session_date": ctx.session_date, "earnings_date": earnings_date, "ticker": ticker,
            "report_session": report_session, "sector": text(item.get("sector")),
            "full_name": text(item.get("full_name")), "marketcap": num(item.get("marketcap")),
            "expected_move_perc": num(item.get("expected_move_perc")),
            "street_mean_est": num(item.get("street_mean_est")),
            "pre_earnings_close": num(item.get("pre_earnings_close")),
            "post_earnings_close": num(item.get("post_earnings_close")),
            "report_time": text(item.get("report_time")), "item": item, "source": "uw", **ctx.stamps(fetched_at)}


def _stored_days(session: date) -> set[tuple[date, str]]:
    rows = rawstore.query(f"SELECT DISTINCT earnings_date, report_session FROM {TABLE} WHERE session_date = ?",
                          [session])
    return {(r["earnings_date"], r["report_session"]) for r in rows}


def run(ctx) -> dict:
    uw = ctx.uw()
    stored = _stored_days(ctx.session_date)
    rows, errors, asked = [], [], 0
    for d in days_ahead(ctx.session_date):
        for session in SESSIONS:
            if (d, session) in stored:
                continue
            asked += 1
            res = uw.calendar_day_raw_result(d, session)
            if res.status != "ok":
                errors.append(error_of(res))
                continue
            fetched = time.time()
            rows += [r for item in res.data if isinstance(item, dict)
                     and (r := calendar_row(item, d, session, ctx, fetched)) is not None]
    out = outcome(asked, errors, **nightly_inputs.record_first(TABLE, rows))
    return out


SWEEP = nightly_inputs.Sweep("D26", (TABLE,), run)
