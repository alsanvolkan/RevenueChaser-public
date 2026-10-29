"""A trailing year from a filer's quarters: the fiscal year plus the newest year to date minus the same a year earlier.

Fundamentals' Quality and Asymmetric Growth read the trailing twelve months when a 10-Q follows a fiscal year (G6,
build plan PR 13 and 14; Asymmetric Growth review SF-4). The rule is the Rule of 40's TTM
(``module_fundamentals.pipeline._ttm_point``): a flow over the trailing year is the fiscal year's figure, plus the
cumulative figure of the newest 10-Q after it (its ``ytd``: the start, the months and the value), minus the same
cumulative figure one fiscal year earlier. The balance sheet and the share count are the newest quarter's.

One record or none: every flow the year carries must come through on one quarter and one length (3, 6 or 9 months),
except that a gross profit the filer does not tag is revenue minus cost of revenue (the extraction's
``gross_profit_derived``). A board that cannot get every figure the year gives stays on the year, never a mix.

Records are the shape ``sec_edgar.extract_annual`` / ``extract_quarterly`` write (and ``core/fundamentals_layer``
rebuilds): a key per field, ``period_ends`` / ``period_starts`` maps on a year, ``period`` and ``ytd`` on a quarter.
"""
from __future__ import annotations

from datetime import date, timedelta

NEXT_START_DAYS = (1, 7)    # a fiscal year starts 1-7 days after the last one ended (sec_edgar.py:501-503)
YEAR_DAYS = (350, 380)      # sec_edgar._YEAR_DAYS: the same period one fiscal year earlier
NEAR_DAYS = 10              # a quarter "at" a day ends within this many days of it (52/53-week filers)
YTD_MONTHS = (3, 6, 9)


def _float(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _day(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _days(a, b) -> int | None:
    da, db = _day(a), _day(b)
    return None if da is None or db is None else (db - da).days


def ytd(rec: dict, field: str):
    """``(start, months, value)`` of the cumulative fact stored for ``field`` on quarter ``rec``; else None."""
    e = (rec.get("ytd") or {}).get(field)
    if not e:
        return None
    v = _float(e.get("val"))
    return None if v is None else (e.get("start"), e.get("months"), v)


def ytd_point(quarterly: list[dict], fields, start: str, months: int, near_end: str):
    """``(end, {field: value})`` of the quarter that ends one fiscal year before ``near_end`` and whose ``ytd``
    holds, for every field, the ``months`` fact that starts on ``start``; else None."""
    for q in reversed(quarterly):
        end = str(q.get("period"))[:10]
        gap = _days(end, near_end)
        if gap is None or not YEAR_DAYS[0] <= gap <= YEAR_DAYS[1]:
            continue
        vals = {}
        for f in fields:
            y = ytd(q, f)
            if not y or y[0] != start or y[1] != months:
                break
            vals[f] = y[2]
        else:
            return end, vals
    return None


def trailing_year(annual: list[dict], quarterly: list[dict], i: int, flows, balance=(), *,
                  months: int | None = None, near: str | None = None) -> dict | None:
    """The trailing-year record on fiscal year ``annual[i]``, or None.

    ``flows`` are summed over the trailing year (each one the year carries must come through; ``cost_of_revenue``
    only stands in for ``gross_profit``); ``balance`` fields are the quarter's (each one the year carries must be
    on it, and a UW preliminary figure is none). The quarter is the newest one after the year whose revenue year to date starts the day after the year
    ends; ``months`` pins its length and ``near`` its end (within ``NEAR_DAYS``), for the trailing year a year
    before another one. The record carries ``period`` (its end), ``months``, ``period_ends`` (every field: that
    end) and ``fiscal_year`` (the year it starts from)."""
    if i is None or not 0 <= i < len(annual):
        return None
    a = annual[i]
    starts, ends = a.get("period_starts") or {}, a.get("period_ends") or {}
    present = [f for f in flows if _float(a.get(f)) is not None and starts.get(f) and ends.get(f)]
    if "revenue" not in present:
        return None
    e0, s0 = str(ends["revenue"])[:10], starts["revenue"]
    ordered = sorted(quarterly or [], key=lambda r: str(r.get("period")))
    cur = None
    for q in reversed(ordered):
        end = str(q.get("period"))[:10]
        if end <= e0:
            break
        if near is not None:
            off = _days(end, near)
            if off is None or abs(off) > NEAR_DAYS:
                continue
        y = ytd(q, "revenue")
        gap = _days(e0, y[0]) if y else None
        if (gap is not None and NEXT_START_DAYS[0] <= gap <= NEXT_START_DAYS[1] and y[1] in YTD_MONTHS
                and (months is None or y[1] == months)):
            cur = q
            break
    if cur is None:
        return None
    cur_start, m = ytd(cur, "revenue")[:2]
    cur_end = str(cur.get("period"))[:10]
    # Delta review D1: a figure UW filled ahead of the 10-Q (``preliminary``) is no SEC figure: the year stands.
    prelim = set(cur.get("preliminary") or [])
    rec: dict = {}
    for f in present:
        y = ytd(cur, f) if f not in prelim else None
        prior = (ytd_point(ordered, (f,), s0, m, cur_end)
                 if y and y[0] == cur_start and y[1] == m and starts[f] == s0 else None)
        if prior is not None:
            rec[f] = _float(a[f]) + y[2] - prior[1][f]
    if "gross_profit" not in rec and "cost_of_revenue" in rec and "revenue" in rec:
        rec["gross_profit"] = rec["revenue"] - rec["cost_of_revenue"]
    if any(f not in rec for f in present if f != "cost_of_revenue"):
        return None
    for f in balance:
        if _float(a.get(f)) is not None:
            v = _float(cur.get(f)) if f not in prelim else None
            if v is None:
                return None
            rec[f] = v
    return {**rec, "period": cur_end, "months": m, "fiscal_year": a.get("fiscal_year"),
            "period_ends": {f: cur_end for f in rec}}


def year_before(day: str) -> str:
    """The day one year before ISO ``day`` (365 days: ``trailing_year``'s ``near`` allows the 52/53-week slack)."""
    return (_day(day) - timedelta(days=365)).isoformat()
