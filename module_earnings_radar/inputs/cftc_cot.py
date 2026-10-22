"""E0f, D29 (#305, the CFTC part): the weekly E-mini S&P 500 Traders-in-Financial-Futures row.

One Socrata read a night (inside the CFTC cap of 5) of the newest ``WEEKS`` TFF rows for the E-mini S&P 500, so a
missed night never leaves a hole and a re-run adds nothing. The parse is SPX Gamma's own (``module_spx_gamma/cftc``
``_map_row``): this writer reuses it rather than keeping a second reading of the Socrata fields.

``report_date`` is the Tuesday the report covers; ``release_date`` is the Friday CFTC publishes it (the report date + 3
days, unless the source row states its own); ``session_date`` is the New York session on or before ``report_date``.
``raw_row`` keeps the source row as served; the first write of (report_type, market, report_date) wins.
"""
from __future__ import annotations

import os
import time
from datetime import date, timedelta

import httpx

from RevenueChaser.core import calendar, nightly_inputs
from RevenueChaser.module_earnings_radar.inputs._common import CALL_CAP, json_safe, outcome, text
from RevenueChaser.module_spx_gamma.cftc import _map_row, _market_where

TABLE = "cftc_cot"
REPORT_TYPE = "TFF"
MARKET = "E-MINI S&P 500"
CFTC_BASE = os.getenv("RC_CFTC_BASE") or "https://publicreporting.cftc.gov/resource/udgc-27he.json"
USER_AGENT = "RevenueChaser/1.0 (nightly inputs: CFTC TFF)"
TIMEOUT = 30.0
WEEKS = 4                     # the newest rows asked for: enough that a missed night is not a hole
RELEASE_LAG_DAYS = 3          # a Tuesday report is published on the Friday


def parse_day(value) -> date | None:
    """A Socrata date cell (``2026-10-06T00:00:00.000``) as a date; None when unreadable."""
    raw = text(value)
    try:
        return date.fromisoformat(raw[:10]) if raw else None
    except ValueError:
        return None


def _release_date(raw: dict, report: date) -> date:
    """The source's own release date when it states one, else the TFF lag: the report date + 3 days."""
    return parse_day(raw.get("release_date")) or report + timedelta(days=RELEASE_LAG_DAYS)


def _session_on_or_before(d: date) -> date | None:
    try:
        return calendar.previous_trading_day(d)
    except ValueError:
        return None


def fetch_rows() -> tuple[list[dict] | None, str | None]:
    """``(source rows newest first, why)``: one Socrata GET of the E-mini's newest TFF rows, no retry."""
    params = {"$where": _market_where(), "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": WEEKS}
    try:
        resp = httpx.get(CFTC_BASE, params=params, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT},
                         follow_redirects=True)
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"[:200]
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    try:
        data = resp.json()
    except ValueError:
        return None, "not_json"
    if not isinstance(data, list):
        return None, "not_json"
    return [row for row in data if isinstance(row, dict)], None


def row_from(raw: dict, ctx, fetched_at: float) -> dict | None:
    """One ``cftc_cot`` row from a source row, or None when it has no report date or no usable positions."""
    mapped = _map_row(raw)
    report = parse_day(mapped.get("report_date")) if mapped else None
    if report is None:
        return None
    am, lf, deal = mapped["asset_manager"], mapped["leveraged_funds"], mapped["dealer"]
    return {"report_type": REPORT_TYPE, "market": MARKET, "report_date": report,
            "release_date": _release_date(raw, report), "open_interest": mapped["open_interest"],
            "asset_mgr_long": am["long"], "asset_mgr_short": am["short"],
            "asset_mgr_change_long": am["change"],
            "lev_money_long": lf["long"], "lev_money_short": lf["short"],
            "lev_money_change_long": lf["change"],
            "dealer_long": deal["long"], "dealer_short": deal["short"],
            "dealer_change_long": deal["change"],
            "raw_row": json_safe(raw), "session_date": _session_on_or_before(report), "source": "cftc",
            **ctx.stamps(fetched_at)}


def run(ctx, fetch=None) -> dict:
    fetch = fetch or fetch_rows
    if not ctx.budget("cftc").take():
        return outcome(1, [CALL_CAP], **nightly_inputs.record_first(TABLE, []))
    raws, why = fetch()
    if raws is None:
        return outcome(1, [f"CFTC: {why or 'failed'}"], **nightly_inputs.record_first(TABLE, []))
    fetched = time.time()
    rows = [r for raw in raws if (r := row_from(raw, ctx, fetched)) is not None]
    return outcome(1, [], unkeyed=len(raws) - len(rows), **nightly_inputs.record_first(TABLE, rows))


SWEEP = nightly_inputs.Sweep("D29", (TABLE,), run)
