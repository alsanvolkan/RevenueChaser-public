"""Which fiscal years a score reads from a list of annual records (oldest first): one anchor year and the year
exactly before it (#150 U8, shared with Fundamentals by #148's review fix).

``annual`` records carry ``fiscal_year`` (EDGAR) or none (yfinance shapes). A gap or a record without a revenue
figure never turns into a growth figure over two or more years.

#247 (Common K5): an EDGAR record also carries ``period_ends`` (#239). Its key is the calendar year of the period end
(``sec_edgar._period_ends``), so a 52/53-week filer's two fiscal years can share a key and key-adjacent records can be
two fiscal years apart. Two records with period ends count as one year apart only when the ends are
``ADJACENT_DAYS`` apart; a record without them keeps the key rule.

C18 (Common K5): the comparability rules the screeners adopt in their own waves (AG S1, Fundamentals F1): a growth
over a prior that is not positive is unknown, a growth above ``SUSPICIOUS_GROWTH`` is flagged, gross profit above
revenue is unknown and flagged, and a filer whose newest record ended more than ``STALE_DAYS`` before the run is
stale. Every figure rule reads the one anchor record; staleness reads the newest record, because a filer whose newer
years carry no revenue figure (a revenue concept the extractor misses) has an old anchor but is not a stale filer
(#380, the PC measurement on 4d2782a: 341 of 384 "stale" names).
"""
from __future__ import annotations

from datetime import date

from .config import to_float

ADJACENT_DAYS = (350, 380)      # period ends one fiscal year apart, inclusive (#247)
SUSPICIOUS_GROWTH = 5.0         # a year-on-year change above +500 % is flagged (C18)
STALE_DAYS = 470                # a newest record that ended longer ago than this is stale (C18, #64 B6)


def fy(rec: dict | None) -> int | None:
    try:
        return int(rec.get("fiscal_year")) if rec else None
    except (TypeError, ValueError):
        return None


def anchor(annual: list[dict]) -> int | None:
    """Index of the one fiscal year a score reads: the newest record with a revenue figure.

    Without any revenue the newest record stands in, so the cash and balance-sheet rules still see a year.
    """
    for i in range(len(annual) - 1, -1, -1):
        if to_float(annual[i].get("revenue")) is not None:
            return i
    return len(annual) - 1 if annual else None


def period_end(rec: dict | None) -> date | None:
    """The period end of an annual record: its revenue's end, else the newest end of any field (EDGAR's
    ``period_ends``), else the record's one ``period_end`` (the yfinance shape: Asymmetric Growth review SF-2, PR 6,
    so the staleness and adjacent-year rules apply to a Yahoo-scored name too); None without any (records stored
    before #239) or with an unreadable date."""
    ends = (rec or {}).get("period_ends") or {}
    if not isinstance(ends, dict):
        return None
    raw = ends.get("revenue") or max((v for v in ends.values() if v), default=None) or (rec or {}).get("period_end")
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def prior(annual: list[dict], i: int | None) -> int | None:
    """Index of the record exactly one fiscal year before ``annual[i]``, else None.

    Records without a ``fiscal_year`` (yfinance shapes) fall back to the previous position. When both records
    carry period ends, they must be ``ADJACENT_DAYS`` apart (#247), so a gap or a shared calendar key never reads
    as one year.
    """
    if i is None or i <= 0:
        return None
    year = fy(annual[i])
    if year is None:
        j = i - 1
    else:
        j = next((k for k in range(i - 1, -1, -1) if fy(annual[k]) == year - 1), None)
        if j is None:
            return None
    cur, prev = period_end(annual[i]), period_end(annual[j])
    if cur is not None and prev is not None:
        lo, hi = ADJACENT_DAYS
        if not lo <= (cur - prev).days <= hi:
            return None
    return j


# ── C18: comparability rules (helpers; the boards adopt them in their own waves) ─────────────────────────────────
def growth(cur, prev) -> tuple[float | None, list[str]]:
    """``(year-on-year change, flags)``. A prior that is not positive gives an unknown growth
    (``prior_nonpositive``); a change above ``SUSPICIOUS_GROWTH`` keeps its value and is flagged
    (``suspicious_growth``). A missing figure is unknown with no flag."""
    cur, prev = to_float(cur), to_float(prev)
    if cur is None or prev is None:
        return None, []
    if prev <= 0:
        return None, ["prior_nonpositive"]
    value = (cur - prev) / prev
    return value, (["suspicious_growth"] if value > SUSPICIOUS_GROWTH else [])


def gross_margin(rec: dict | None) -> tuple[float | None, list[str]]:
    """``(gross margin, flags)`` of one record: unknown and flagged when revenue is not positive
    (``revenue_nonpositive``) or gross profit exceeds revenue (``gross_profit_exceeds_revenue``)."""
    rev, gp = to_float((rec or {}).get("revenue")), to_float((rec or {}).get("gross_profit"))
    if rev is None or gp is None:
        return None, []
    if rev <= 0:
        return None, ["revenue_nonpositive"]
    if gp > rev:
        return None, ["gross_profit_exceeds_revenue"]
    return gp / rev, []


def stale_days(rec: dict | None, as_of: date) -> int | None:
    """Days from the record's period end to ``as_of``; None without a period end."""
    end = period_end(rec)
    return (as_of - end).days if end is not None else None


def is_stale(rec: dict | None, as_of: date) -> bool:
    """True when the record ended more than ``STALE_DAYS`` before ``as_of`` (excluded by an adopting board). A
    record without a period end is not judged here."""
    days = stale_days(rec, as_of)
    return days is not None and days > STALE_DAYS


def latest_is_stale(annual: list[dict], as_of: date) -> bool:
    """True when the newest record (the filer's latest annual report) ended more than ``STALE_DAYS`` before
    ``as_of``. The revenue anchor never decides: its newer years may only lack a revenue figure. A newest record
    without a period end is not judged."""
    return bool(annual) and is_stale(annual[-1], as_of)


def comparability(annual: list[dict], as_of: date) -> dict:
    """Every C18 rule: the figure rules read the one anchor record and its adjacent prior year; ``stale_days`` and
    ``stale`` read the newest record (``latest_is_stale``).

    ``{anchor, prior, period_end (the anchor's, ISO or None), stale_days, stale, rev_growth, gross_margin, flags}``.
    """
    i = anchor(annual)
    j = prior(annual, i)
    cur = annual[i] if i is not None else None
    rev_growth, flags = growth(cur.get("revenue"), annual[j].get("revenue")) if j is not None else (None, [])
    margin, margin_flags = gross_margin(cur)
    end = period_end(cur)
    return {"anchor": i, "prior": j, "period_end": end.isoformat() if end else None,
            "stale_days": stale_days(annual[-1] if annual else None, as_of), "stale": latest_is_stale(annual, as_of),
            "rev_growth": rev_growth, "gross_margin": margin, "flags": [*flags, *margin_flags]}
