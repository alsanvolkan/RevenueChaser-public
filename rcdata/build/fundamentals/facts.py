"""A company's XBRL facts as fact rows: every value with its accession, filed date, form and period (#43 C14).

The extraction rules are ``core/providers/sec_edgar.py``'s (``extract_annual`` / ``extract_quarterly``: the concept
priority, the YTD-derived quarters of #158, the add-only concepts), so a value here is the value the boards read
today. What this layer adds:

- **One row per value**, long: ``(cik, field, period_type, period_end)`` with ``period_type`` ``annual`` or
  ``quarterly`` on every row, the period's start (flows), the unit, and ``derived`` (a YTD difference or a sum).
- **Provenance on every row.** The accession and filed date the extraction records per field (``rec["accn"]`` /
  ``rec["filed"]``), and the form of that accession (read from the facts themselves). A field the extraction keeps
  no provenance for gets it here: a derived ``gross_profit`` (revenue minus cost) the newer of its two facts';
  ``short_term_investments`` (a sum of tags) the newest fact of its tags ending on the period end; anything else
  the newest fact of the field's concepts ending on the period end. A value still without an accession is not
  served (counted as ``no_provenance``).
- **Total debt summed from three terms** (#239): ``total_debt = lt_debt + lt_debt_current + short_term_borrowings``
  (``LongTermDebtNoncurrent``, ``LongTermDebtCurrent`` or ``LongTermDebtAndCapitalLeaseObligationsCurrent``,
  ``ShortTermBorrowings``), the terms present summed, on the period end of the newest term; its provenance is the
  newest-filed term's, and it is ``derived``. The old extraction's two-term ``total_debt`` is not served. The three
  are distinct lines of a balance sheet (``DebtCurrent``, their sum, is never added in).
- **EPS** (``eps_basic``, ``eps_diluted``: ``EarningsPerShareBasic`` / ``...Diluted``, else
  ``EarningsPerShareBasicAndDiluted``, unit ``USD/shares``), which the old extraction did not read: per period the
  first concept that has one, a reported quarter only (EPS is not additive, so no YTD difference).
"""
from __future__ import annotations

from datetime import date

from ....core.providers import sec_edgar

ANNUAL, QUARTERLY = "annual", "quarterly"
SOURCE = "sec/companyfacts"
DEBT_TERMS = ("lt_debt", "lt_debt_current", "short_term_borrowings")
EPS_CONCEPTS = {
    "eps_basic": (["EarningsPerShareBasic", "EarningsPerShareBasicAndDiluted"], "USD/shares"),
    "eps_diluted": (["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"], "USD/shares"),
}
# The forms whose facts are statements: a re-read of the company's facts is due when one of them is filed.
STATEMENT_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "10-KT", "10-KT/A", "10-QT", "10-QT/A",
                             "20-F", "20-F/A", "40-F", "40-F/A"})
# The served fields: the extraction's, the three-term total debt and EPS.
FIELDS = tuple(sec_edgar.CONCEPTS) + ("total_debt",) + tuple(EPS_CONCEPTS)

FACT = [("cik", "TEXT"), ("field", "TEXT"), ("period_type", "TEXT"), ("fiscal_year", "INTEGER"),
        ("period_start", "DATE"), ("period_end", "DATE"), ("value", "DOUBLE"), ("unit", "TEXT"), ("accn", "TEXT"),
        ("filed", "DATE"), ("form", "TEXT"), ("derived", "BOOLEAN"), ("preliminary", "BOOLEAN"),
        ("source", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
COMPANY = [("cik", "TEXT"), ("entity_name", "TEXT"), ("is_us", "BOOLEAN"), ("non_usd_units", "TEXT"),
           ("us_gaap", "BOOLEAN"), ("facts_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]


def _day(text) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10]) if text else None
    except ValueError:
        return None


class _Index:
    """What the facts say about every accession and fact end: the form of an accession, and per ``(concept, end)``
    the newest ``(filed, accn)``."""

    def __init__(self, facts: dict):
        self.form: dict[str, str] = {}
        self.at_end: dict[tuple[str, str], tuple[str, str]] = {}
        for taxonomy in (facts.get("facts") or {}).values():
            for concept, body in (taxonomy or {}).items():
                for units in ((body or {}).get("units") or {}).values():
                    for f in units or []:
                        accn, filed, end = f.get("accn"), f.get("filed") or "", f.get("end") or ""
                        if accn and f.get("form"):
                            self.form.setdefault(accn, f["form"])
                        if accn and filed and end:
                            cur = self.at_end.get((concept, end))
                            if cur is None or (filed, accn) > cur:
                                self.at_end[(concept, end)] = (filed, accn)

    def newest(self, concepts, end: str | None) -> tuple[str, str] | None:
        found = [self.at_end[(c, end)] for c in concepts if end and (c, end) in self.at_end]
        return max(found) if found else None


def _newer(a: tuple[str, str] | None, b: tuple[str, str] | None) -> tuple[str, str] | None:
    return max(x for x in (a, b) if x) if (a or b) else None


def _rows_of(facts: dict, rec: dict, period_type: str, index: _Index, counts: dict) -> list[dict]:
    """The fact rows of one extracted record (an annual year or a quarter)."""
    quarterly = period_type == QUARTERLY
    ends = {} if quarterly else (rec.get("period_ends") or {})
    starts = rec.get("period_starts") or {}
    filed, accn = rec.get("filed") or {}, rec.get("accn") or {}
    derived = set(rec.get("derived") or [])
    fiscal_year = None if quarterly else int(rec["fiscal_year"])
    out: dict[str, dict] = {}
    prov: dict[str, tuple[str, str] | None] = {}
    # A derived gross profit takes its provenance from revenue and cost, so it comes last.
    order = sorted(sec_edgar.CONCEPTS, key=lambda f: f == "gross_profit")
    for field in order:
        concepts, unit = sec_edgar.CONCEPTS[field]
        value = rec.get(field)
        if value is None:
            continue
        end = rec["period"] if quarterly else ends.get(field)
        if end is None:
            # A field the record keeps no end for (the bank top lines, ``_OMIT_WHEN_ABSENT``): its own series' end.
            end = _annual_end(facts, rec, concepts, unit)
        if field in filed and field in accn:
            p = (filed[field], accn[field])
        elif field == "gross_profit" and rec.get("gross_profit_derived"):
            p = _newer(prov.get("revenue"), prov.get("cost_of_revenue"))
            derived.add(field)
        else:
            p = index.newest(concepts, end)
        prov[field] = p
        if p is None or end is None:
            counts["no_provenance"] = counts.get("no_provenance", 0) + 1
            continue
        out[field] = {"field": field, "period_type": period_type, "fiscal_year": fiscal_year,
                      "period_start": _day(starts.get(field)), "period_end": _day(end), "value": float(value),
                      "unit": unit, "accn": p[1], "filed": _day(p[0]), "form": index.form.get(p[1]),
                      "derived": field in derived}
    terms = [out[t] for t in DEBT_TERMS if t in out]
    if terms:
        newest = max(terms, key=lambda r: (r["filed"], r["accn"]))
        out["total_debt"] = {**newest, "field": "total_debt", "period_start": None, "unit": "USD",
                             "value": sum(r["value"] for r in terms),
                             "period_end": max(r["period_end"] for r in terms), "derived": True}
    return list(out.values())


def _annual_end(facts: dict, rec: dict, concepts: list[str], unit: str) -> str | None:
    series = sec_edgar._annual_facts(facts, concepts, unit)
    hit = series.get(str(rec["fiscal_year"]))
    return hit[1] if hit else None


def _eps_rows(facts: dict, index: _Index) -> list[dict]:
    out = []
    for field, (concepts, unit) in EPS_CONCEPTS.items():
        for period_type, fn in ((ANNUAL, sec_edgar._annual_facts), (QUARTERLY, sec_edgar._quarterly_facts)):
            # sec_edgar's own per-period priority rule (a private helper of the one extraction, read, not changed).
            for key, fact in sec_edgar._series_by_priority(fn, facts, concepts, unit).items():
                value, end = fact
                info = getattr(fact, "info", {}) or {}
                p = (info["filed"], info["accn"]) if info.get("filed") and info.get("accn") else \
                    index.newest(concepts, end)
                if p is None:
                    continue
                out.append({"field": field, "period_type": period_type,
                            "fiscal_year": int(key) if period_type == ANNUAL else None,
                            "period_start": _day(info.get("start")), "period_end": _day(end), "value": float(value),
                            "unit": unit, "accn": p[1], "filed": _day(p[0]), "form": index.form.get(p[1]),
                            "derived": False})
    return out


def fact_rows(facts: dict, cik: str) -> tuple[list[dict], dict, dict]:
    """``(rows, company, counts)`` of one company's ``companyfacts`` answer (no stamps: the caller adds them).

    ``company``: ``entity_name``, ``is_us``, ``non_usd_units`` (the currencies a money concept was tagged in instead
    of USD, #46) and ``us_gaap`` (False: no us-gaap facts at all, an IFRS filer, whose statements this layer cannot
    read yet). ``counts``: ``no_provenance``, values left out for want of an accession.
    """
    counts: dict[str, int] = {}
    index = _Index(facts)
    non_usd: set[str] = set()
    annual = sec_edgar.extract_annual(facts, non_usd)
    quarterly = sec_edgar.extract_quarterly(facts, non_usd)
    rows: list[dict] = []
    for rec in annual:
        rows += _rows_of(facts, rec, ANNUAL, index, counts)
    for rec in quarterly:
        rows += _rows_of(facts, rec, QUARTERLY, index, counts)
    rows += _eps_rows(facts, index)
    for r in rows:
        r["cik"] = cik
    company = {"cik": cik, "entity_name": facts.get("entityName"), "is_us": sec_edgar._is_us_filer(facts),
               "non_usd_units": ",".join(sorted(non_usd)) or None, "us_gaap": bool(sec_edgar._gaap(facts))}
    return rows, company, counts
