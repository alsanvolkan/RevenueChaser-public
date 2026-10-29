"""The fundamentals boards' statements, from the fundamentals layer (build plan PR 13-15, G6).

Fundamentals, Asymmetric Growth and Financials read a company's statements as the old ``edgar_financials`` row:
``annual`` records by fiscal year and ``quarterly`` records by period end, each a dict with a key per field and the
maps the extraction writes beside them (``core/providers/sec_edgar.py``: ``period_ends``, ``period_starts``,
``filed``, ``accn``, ``derived`` and, on a quarter, ``ytd``). This module rebuilds that shape from
``fundamental_fact`` (``fundamentals.duckdb``, read through ``rcdata.read``), so the board code reads the layer
without a second set of rules. What the layer changes, on purpose:

- **The rows are current.** ``sec/companyfacts`` reads a company's facts again on the day the SEC daily index shows
  a statement it does not hold yet, so a record's age is the filer's, not the age of a row nobody refreshed
  (Fundamentals review F3; the old row was never fetched again, #43 C14).
- **Total debt** is the layer's: the filer's own debt lines, each counted once (``rcdata/build/fundamentals/facts``),
  not the old two-term sum.
- **A board never downloads facts itself** on this path: the builder does (D1). A company the layer does not hold
  reads as "not available", exactly as a store-only read of a missing old row did.

``meta`` keeps the old keys (``source`` ``sec_edgar``, ``entity_name``, ``is_us``, ``available``,
``extract_version``, ``non_usd_units``) and adds the layer's: ``layer`` (True), ``cik``, ``facts_as_of`` (the day
the facts were last read), ``submissions_as_of`` (the day the filing list was last read), ``newest_filed`` and
``newest_period_end`` (the company's newest statement), and ``preliminary`` (the quarter ends that hold a UW
press-release figure the SEC has not filed yet).

What the rebuilt shape does not carry: the extraction's per-period flags (``<field>_suspect``,
``short_term_investments_basis``), which the layer does not serve and no board reads.

**The source** (the pattern of ``core/universe.py``): ``RC_FUNDAMENTALS_SOURCE`` unset (or ``layer``) reads the
layer; any other value (``store`` by convention) reads the old ``edgar_financials`` rows, the escape hatch kept until
the old path is retired (plan PR 21). With the layer asked for but no ``fundamentals.duckdb``, or no
``sec/companyfacts`` build yet, the old store is read and a warning is logged once (``source_state``); a failed
newest check keeps the layer's last good build.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta

SOURCE_ENV = "RC_FUNDAMENTALS_SOURCE"
LAYER = "layer"
STORE = "store"
FACTS_DATASET = "sec/companyfacts"

# How much history a board record carries. Fundamentals reads an anchor year, the year before it and the trailing
# quarters (a TTM, the TTM a year earlier and two years earlier for the Rule of 40's growth); Asymmetric Growth the
# same plus the year before that; Financials up to five years.
# shortcut: a fixed window, not every year the SEC holds; widen it when a board reads a longer history.
ANNUAL_YEARS = 8
QUARTERS = 16

# Consecutive fiscal quarters end ~91 days apart (``module_fundamentals.pipeline._QUARTER_GAP_DAYS``), and a fiscal
# year starts 1-7 days after the one before it ends (``sec_edgar``'s own rule).
QUARTER_GAP_DAYS = (80, 102)
NEXT_START_DAYS = (1, 7)

logger = logging.getLogger(__name__)
_FALLBACK_LOGGED: set[tuple[str, str]] = set()


def source_state() -> dict:
    """``{requested, effective, reason, variable}`` of the statements source, read at call time (``core.universe``'s
    rule): unset or ``layer`` asks for the layer, honoured while ``fundamentals.duckdb`` is there and
    ``sec/companyfacts`` has built it at least once; any other value asks for the old store.

    Unlike the universe's rule, a failed newest check does not send the boards back (Opus review S3): the served
    file is then still the last good build, and the old ``edgar_financials`` rows are never refreshed at all. The
    failure is logged once. Default chosen, change if avo prefers the universe's stricter rule."""
    variable = (os.getenv(SOURCE_ENV) or "").strip() or None
    if variable is not None and variable.lower() != LAYER:
        return {"requested": STORE, "effective": STORE, "reason": None, "variable": variable}
    from ..rcdata import ops, serving
    reason = None
    asked = (f"{SOURCE_ENV}={LAYER} is set" if variable
             else f"The fundamentals boards read the layer by default ({SOURCE_ENV} unset)")
    if not serving.live_path("fundamentals").exists():
        reason = "no fundamentals.duckdb yet"
    elif not ops.ever_built(FACTS_DATASET):
        reason = f"{FACTS_DATASET} has not built yet"
    else:
        last = ops.latest_unskipped_check(FACTS_DATASET)
        if last and last["result"] not in ("changed", "unchanged"):
            note = f"{FACTS_DATASET}'s last check {last['result']}"
            if (asked, note) not in _FALLBACK_LOGGED:
                _FALLBACK_LOGGED.add((asked, note))
                logger.warning("%s; %s, so they read its last good build", asked, note)
    if reason and (asked, reason) not in _FALLBACK_LOGGED:
        _FALLBACK_LOGGED.add((asked, reason))
        logger.warning("%s, but they fall back to the old store: %s", asked, reason)
    return {"requested": LAYER, "effective": STORE if reason else LAYER, "reason": reason, "variable": variable}


def reads_layer() -> bool:
    return source_state()["effective"] == LAYER


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, date) else str(value)[:10]


def _day(value) -> date | None:
    if value is None:
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _unavailable(company: dict | None = None) -> dict:
    company = company or {}
    return {"source": "sec_edgar", "entity_name": company.get("entity_name") or company.get("name") or "",
            "is_us": True if company.get("is_us") is None else bool(company["is_us"]), "available": False,
            "layer": True}


def _meta(company: dict, has_rows: bool, preliminary: list[str]) -> dict:
    from .providers import sec_edgar
    units = [u for u in str(company.get("non_usd_units") or "").split(",") if u]
    meta = {"source": "sec_edgar", "entity_name": company.get("entity_name") or company.get("name") or "",
            "is_us": True if company.get("is_us") is None else bool(company["is_us"]), "available": has_rows,
            "extract_version": sec_edgar.EXTRACT_VERSION, "layer": True, "cik": company.get("cik"),
            "facts_as_of": _iso(company.get("facts_as_of")), "submissions_as_of": _iso(company.get("submissions_as_of")),
            "newest_filed": _iso(company.get("newest_filed")),
            "newest_period_end": _iso(company.get("newest_period_end"))}
    # #46: the currencies a money concept was tagged in instead of USD. The layer measures it on every read of the
    # facts, so the key is always there once the facts were read (``facts_as_of``); before that it is "not measured".
    if company.get("facts_as_of") is not None:
        meta["non_usd_units"] = units
    if preliminary:
        meta["preliminary"] = preliminary
    return meta


def _annual_records(rows: list[dict]) -> list[dict]:
    """The annual records of one company's annual rows, oldest first, in the extraction's shape."""
    from .providers import sec_edgar
    omit = set(sec_edgar._OMIT_WHEN_ABSENT)
    by_year: dict[int, list[dict]] = {}
    for r in rows:
        if r.get("fiscal_year") is not None:
            by_year.setdefault(int(r["fiscal_year"]), []).append(r)
    out = []
    for year in sorted(by_year):
        rec: dict = {"fiscal_year": str(year)}     # the extraction keys a year by its text, "2025"
        ends, starts, filed, accn, derived = {}, {}, {}, {}, []
        for r in by_year[year]:
            f = r["field"]
            rec[f] = float(r["value"])
            # ``sec_edgar._period_ends``: the extraction's fields only, and the bank top lines carry no end, so a
            # bank's record end stays its other fields' (``core/fiscal_year.period_end`` reads ``max`` of them).
            if f in sec_edgar.CONCEPTS and f not in omit:
                ends[f] = _iso(r["period_end"])
            if f not in sec_edgar.CONCEPTS:
                continue        # total debt and EPS: the value only, as the extraction's maps hold its own fields
            if r.get("period_start") is not None:
                starts[f] = _iso(r["period_start"])
            if r.get("filed") is not None and not (f == "gross_profit" and r.get("derived")):
                filed[f] = _iso(r["filed"])
            if r.get("accn") and not (f == "gross_profit" and r.get("derived")):
                accn[f] = r["accn"]
            if r.get("derived") and f != "gross_profit":
                derived.append(f)
        if rec.get("gross_profit") is not None and any(r["field"] == "gross_profit" and r.get("derived")
                                                       for r in by_year[year]):
            rec["gross_profit_derived"] = True
        rec["period_ends"] = ends
        for key, value in (("filed", filed), ("accn", accn), ("derived", derived), ("period_starts", starts)):
            if value:
                rec[key] = value
        out.append(rec)
    return out


def _quarterly_records(rows: list[dict], annual_rows: list[dict]) -> tuple[list[dict], list[str]]:
    """``(records, preliminary period ends)`` of one company's quarterly rows, oldest first. A preliminary value
    (UW's, ahead of the 10-Q) fills a field only where the SEC has none for that quarter."""
    from .providers import sec_edgar
    by_end: dict[str, dict[str, dict]] = {}
    for r in sorted(rows, key=lambda r: bool(r.get("preliminary"))):      # the SEC's rows first
        by_end.setdefault(_iso(r["period_end"]), {}).setdefault(r["field"], r)
    out, preliminary = [], []
    # Only a money flow adds up over quarters: not a share count (a weighted average) and not EPS.
    additive = {r["field"] for r in rows if r.get("unit") == "USD" and r.get("period_start") is not None}
    for end in sorted(by_end):
        rec: dict = {"period": end}
        starts, filed, accn, derived, prelim = {}, {}, {}, [], []
        for f, r in by_end[end].items():
            rec[f] = float(r["value"])
            if r.get("preliminary"):
                prelim.append(f)
            if f not in sec_edgar.CONCEPTS:
                continue
            if r.get("period_start") is not None:
                starts[f] = _iso(r["period_start"])
            if r.get("filed") is not None and not (f == "gross_profit" and r.get("derived")):
                filed[f] = _iso(r["filed"])
            if r.get("accn") and not (f == "gross_profit" and r.get("derived")):
                accn[f] = r["accn"]
            if r.get("derived") and f != "gross_profit":
                derived.append(f)
        for key, value in (("filed", filed), ("accn", accn), ("derived", derived), ("period_starts", starts)):
            if value:
                rec[key] = value
        if rec.get("gross_profit") is not None and by_end[end]["gross_profit"].get("derived"):
            rec["gross_profit_derived"] = True
        if prelim:
            rec["preliminary"] = sorted(prelim)
            preliminary.append(end)
        out.append(rec)
    _add_ytd(out, annual_rows, additive)
    return out, preliminary


def _add_ytd(quarterly: list[dict], annual_rows: list[dict], additive: set[str]) -> None:
    """Each quarter's ``ytd``: per money flow (``additive``), ``{start, months, val}`` of the fiscal year to date that ends on it,
    the sum of the year's quarters up to it (``module_fundamentals.pipeline``'s TTM Rule of 40 reads it).

    The extraction kept the filer's own cumulative fact; the layer serves the discrete quarters (a quarter the filer
    reports only as a year to date is the difference of two of them), so the year to date is their sum. The fiscal
    year is the annual record's span when the layer holds one, else it starts with the first quarter that begins
    ``NEXT_START_DAYS`` after the newest fiscal year end. A run that is not consecutive, or that does not begin on the
    year's first day, has no year to date, nor has one over a UW preliminary figure. Only 3, 6 and 9 months are given:
    twelve is the annual record itself.

    shortcut: the sum of the discrete quarters, not the filer's own cumulative fact; equal unless a later filing
    restated one quarter and not the others. Serve the cumulative facts from the layer if the real-data check ever
    shows the two apart."""
    spans: dict[str, list[tuple[date, date]]] = {}
    for r in annual_rows:
        if r.get("period_start") is not None and r.get("period_end") is not None:
            spans.setdefault(r["field"], []).append((_day(r["period_start"]), _day(r["period_end"])))
    year_ends = sorted({_day(r["period_end"]) for r in annual_rows if r.get("period_end") is not None})
    lo, hi = QUARTER_GAP_DAYS
    for i, rec in enumerate(quarterly):
        end = _day(rec["period"])
        ytd = {}
        for f, s in (rec.get("period_starts") or {}).items():
            if f not in additive:
                continue
            fy_start = next((a for a, b in spans.get(f, ()) if a <= _day(s) and end <= b), None)
            if fy_start is None:
                prior = [e for e in year_ends if e < end]
                if not prior:
                    continue
                last = prior[-1]
                # A quarter of a year no annual record covers yet starts the year 1-7 days after the newest year end.
                fy_start = next((_day(q["period_starts"][f]) for q in quarterly
                                 if f in (q.get("period_starts") or {}) and last < _day(q["period"]) <= end
                                 and NEXT_START_DAYS[0] <= (_day(q["period_starts"][f]) - last).days
                                 <= NEXT_START_DAYS[1]), None)
                if fy_start is None:
                    continue
            run = [q for q in quarterly[:i + 1] if f in q and f in (q.get("period_starts") or {})
                   and _day(q["period_starts"][f]) >= fy_start]
            if not run or _day(run[0]["period_starts"][f]) != fy_start or len(run) > 3:
                continue
            # Delta review D1: a figure UW filled ahead of the 10-Q (``preliminary``) is no reported quarter, so no
            # year to date runs over it (``module_fundamentals.pipeline._quarter_run``'s rule).
            if any(f in (q.get("preliminary") or []) for q in run):
                continue
            ends = [_day(q["period"]) for q in run]
            if any(not lo <= (b - a).days <= hi for a, b in zip(ends, ends[1:])):
                continue
            ytd[f] = {"start": fy_start.isoformat(), "months": 3 * len(run), "val": sum(float(q[f]) for q in run)}
        if ytd:
            rec["ytd"] = ytd


def records_many(tickers) -> dict[str, tuple[list[dict], list[dict], dict]]:
    """``{ticker: (annual, quarterly, meta)}`` from the layer for every ticker it names a company for (a ticker it
    does not hold is absent). Two reads for the whole list: the companies, then their statements."""
    from ..rcdata import read
    companies = {str(c["ticker"]).strip().upper(): c for c in read.fundamental_companies(tickers)}
    if not companies:
        return {}
    rows_of: dict[str, list[dict]] = {}
    for r in read.statements_many({c["cik"] for c in companies.values()}, ANNUAL_YEARS, QUARTERS):
        rows_of.setdefault(str(r["cik"]), []).append(r)
    out = {}
    for ticker, company in companies.items():
        rows = rows_of.get(str(company["cik"]), [])
        annual_rows = [r for r in rows if r["period_type"] == "annual" and not r.get("preliminary")]
        annual = _annual_records(annual_rows)
        quarterly, preliminary = _quarterly_records([r for r in rows if r["period_type"] == "quarterly"],
                                                    annual_rows)
        out[ticker] = (annual, quarterly, _meta(company, bool(annual or quarterly), preliminary))
    return out


def records(ticker: str) -> tuple[list[dict], list[dict], dict]:
    """One ticker's ``(annual, quarterly, meta)`` from the layer; empty lists and an unavailable meta when the layer
    holds no company for it."""
    found = records_many([ticker]).get(str(ticker).upper())
    return found if found else ([], [], _unavailable())


def newest_statement_filed(meta: dict | None) -> date | None:
    """The day the company's newest statement was filed (the layer's meta), None off the layer."""
    return _day((meta or {}).get("newest_filed"))

