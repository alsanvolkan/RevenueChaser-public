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
- **Total debt from the debt lines the filer uses** (#239): a long-term part plus a current part, each line read
  once. The long-term part is the first of ``LongTermDebtNoncurrent``, ``LongTermDebtAndCapitalLeaseObligations``
  and ``LongTermNotesPayable``; the current part is then ``DebtCurrent`` (short-term borrowings and the current
  portion together) when tagged, else ``LongTermDebtCurrent`` plus the short-term line. A filer whose long-term line
  includes the current portion (``LongTermDebt``, ``LongTermDebtAndCapitalLeaseObligationsIncludingCurrent
  Maturities``) gets that plus the short-term line. The short-term line is ``ShortTermBorrowings``, else
  ``CommercialPaper`` (which ``ShortTermBorrowings`` already holds when both are tagged). Convertible notes due within
  the year (``ConvertibleNotesPayableCurrent``, ``ConvertibleDebtCurrent``) are the current line when neither
  ``DebtCurrent`` nor ``LongTermDebtCurrent`` is tagged, and are added to a whole long-term line only when that line
  is below them, so it cannot hold them (#264: NVIDIA's restated 0 for 31 Jan 2016). A line of credit is not
  added: the filers that tag one carry it inside their long-term line. With no long-term line tagged there is no
  total: a current line alone is not the whole. Measured on the real-data check of 2026-10-28 (PR 249): the earlier
  three-term sum served HEI 3.4 million instead of 2.17 billion (its long-term line is
  ``LongTermDebtAndCapitalLeaseObligations``), no total for ORCL (``LongTermNotesPayable`` + ``DebtCurrent``), AAPL
  without its 7.98 billion of commercial paper, and JPM its 72 billion of short-term borrowings alone (its 461
  billion long-term line is the including-current tag above). The total sits on the period end of the newest line;
  its provenance is the newest-filed line's, and it is ``derived``. The old extraction's two-term ``total_debt`` is
  not served.
- **EPS** (``eps_basic``, ``eps_diluted``: ``EarningsPerShareBasic`` / ``...Diluted``, else
  ``EarningsPerShareBasicAndDiluted``, unit ``USD/shares``), which the old extraction did not read: per period the
  first concept that has one, a reported quarter only (EPS is not additive, so no YTD difference).
"""
from __future__ import annotations

from datetime import date

from ....core.providers import sec_edgar

ANNUAL, QUARTERLY = "annual", "quarterly"
SOURCE = "sec/companyfacts"
# Total debt (#239, see the module docstring): the long-term lines in priority order, and the one tag read here
# that the extraction does not collect.
LONG_TERM_DEBT = ("lt_debt", "lt_debt_and_lease_noncurrent", "notes_payable_noncurrent")
COMMERCIAL_PAPER = "CommercialPaper"
WHOLE_LONG_TERM_DEBT = "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"
DEBT_LINES = LONG_TERM_DEBT + ("lt_debt_total", "debt_current", "lt_debt_current", "short_term_borrowings",
                               "convertible_debt_current")
EPS_CONCEPTS = {
    "eps_basic": (["EarningsPerShareBasic", "EarningsPerShareBasicAndDiluted"], "USD/shares"),
    "eps_diluted": (["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"], "USD/shares"),
}
# The forms whose facts are statements: a re-read of the company's facts is due when one of them is filed.
STATEMENT_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "10-KT", "10-KT/A", "10-QT", "10-QT/A",
                             "20-F", "20-F/A", "40-F", "40-F/A"})
# The served fields: the extraction's, total debt and EPS.
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
    """What the facts say about every accession and fact span: the form of an accession, per ``(concept, start,
    end)`` the newest ``(filed, accn)`` and the USD value that newest filing gave (``start`` is ``""`` for an
    instant), and per ``(concept, end)`` the newest of any span ending that day. A quarter, its fiscal year and the
    balance sheet can all end on one day, so a lookup asks for its own span first (the Opus review's N5)."""

    def __init__(self, facts: dict):
        self.form: dict[str, str] = {}
        self.at_end: dict[tuple[str, str], tuple[str, str]] = {}
        self.at_span: dict[tuple[str, str, str], tuple[str, str]] = {}
        self.usd_at_span: dict[tuple[str, str, str], float] = {}
        for taxonomy in (facts.get("facts") or {}).values():
            for concept, body in (taxonomy or {}).items():
                for unit, units in ((body or {}).get("units") or {}).items():
                    for f in units or []:
                        accn, filed, end = f.get("accn"), f.get("filed") or "", f.get("end") or ""
                        if accn and f.get("form"):
                            self.form.setdefault(accn, f["form"])
                        if not (accn and filed and end):
                            continue
                        if (filed, accn) > self.at_end.get((concept, end), ("", "")):
                            self.at_end[(concept, end)] = (filed, accn)
                        span = (concept, f.get("start") or "", end)
                        if (filed, accn) > self.at_span.get(span, ("", "")):
                            self.at_span[span] = (filed, accn)
                            if unit == "USD" and isinstance(f.get("val"), (int, float)):
                                self.usd_at_span[span] = float(f["val"])
                            else:
                                self.usd_at_span.pop(span, None)

    def newest(self, concepts, end: str | None, start: str | None = None) -> tuple[str, str] | None:
        """The newest ``(filed, accn)`` of ``concepts`` over ``start`` (None: an instant) to ``end``; when the facts
        hold no such span, the newest of any span ending on ``end``."""
        if not end:
            return None
        found = [self.at_span[(c, start or "", end)] for c in concepts if (c, start or "", end) in self.at_span]
        found = found or [self.at_end[(c, end)] for c in concepts if (c, end) in self.at_end]
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
            p = index.newest(concepts, end, starts.get(field))
        prov[field] = p
        if p is None or end is None:
            counts["no_provenance"] = counts.get("no_provenance", 0) + 1
            continue
        out[field] = {"field": field, "period_type": period_type, "fiscal_year": fiscal_year,
                      "period_start": _day(starts.get(field)), "period_end": _day(end), "value": float(value),
                      "unit": unit, "accn": p[1], "filed": _day(p[0]), "form": index.form.get(p[1]),
                      "derived": field in derived}
    balance_end = rec["period"] if quarterly else ends.get("total_assets")
    terms = _debt_lines(out, index, balance_end)
    if terms:
        newest = max(terms, key=lambda r: (r["filed"], r["accn"]))
        out["total_debt"] = {"field": "total_debt", "period_type": period_type, "fiscal_year": fiscal_year,
                             "period_start": None, "period_end": max(r["period_end"] for r in terms),
                             "value": sum(r["value"] for r in terms), "unit": "USD", "accn": newest["accn"],
                             "filed": newest["filed"], "form": newest["form"], "derived": True}
    return list(out.values())


def _debt_lines(out: dict[str, dict], index: _Index, balance_end: str | None) -> list[dict]:
    """The rows total debt sums (module docstring): each debt line of the balance sheet once, or none when no
    long-term line is tagged (a current line alone is not the whole, so the total stays unknown).

    Every line comes from the one balance sheet dated ``balance_end`` (the record's ``total_assets`` end; the newest
    debt line's end when it has none). An annual record is keyed by calendar year and each field keeps its own end,
    so a filer that moved its year end holds two balance dates in one record; a line of the other date is left out,
    as ``sec_edgar._fill_gross_profit`` leaves out a cost of another period (the Opus review's M1)."""
    lines = {f: r for f, r in out.items() if f in DEBT_LINES}
    if balance_end is None and lines:
        balance_end = max(r["period_end"] for r in lines.values()).isoformat()
    lines = {f: r for f, r in lines.items() if r["period_end"] == _day(balance_end)}
    short = lines.get("short_term_borrowings") or _tagged(index, COMMERCIAL_PAPER, balance_end)
    short = [short] if short else []
    long_term = next((lines[f] for f in LONG_TERM_DEBT if f in lines), None)
    convertible = lines.get("convertible_debt_current")
    if long_term is not None:
        # #264: convertible notes due within the year are the current line only when no other current line is
        # tagged (DebtCurrent and LongTermDebtCurrent hold them when they are), so they are never read twice.
        current_line = lines.get("lt_debt_current") or convertible
        current = [lines["debt_current"]] if "debt_current" in lines else ([current_line] if current_line else []) + short
        return [long_term] + current
    whole = lines.get("lt_debt_total") or _tagged(index, WHOLE_LONG_TERM_DEBT, balance_end)
    if not whole:
        return []
    # A whole long-term line holds its current portion, so it is never below the convertible notes due within the
    # year; one that is (NVIDIA's LongTermDebt of 31 Jan 2016 as restated: 0, beside 1.41 billion of them) leaves
    # them out, and they are added (#264).
    return [whole] + ([convertible] if convertible and whole["value"] < convertible["value"] else []) + short


def _tagged(index: _Index, concept: str, end: str | None) -> dict | None:
    """A tag the extraction does not collect, on ``end``, as a row (its USD value and newest filing)."""
    value, p = index.usd_at_span.get((concept, "", end or "")), index.at_span.get((concept, "", end or ""))
    if value is None or p is None:
        return None
    return {"field": concept, "value": value, "period_end": _day(end), "accn": p[1], "filed": _day(p[0]),
            "form": index.form.get(p[1])}


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
                    index.newest(concepts, end, info.get("start"))
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
