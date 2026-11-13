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
  total: a current line alone is not the whole. **When none of those lines is tagged at all** (#737), a second pass
  reads the balances a filer may tag instead (``FALLBACK_*``, ``_debt_lines``): a whole or noncurrent line of its
  own ladder, plus a current and a short-term line. The second pass never runs for a company the first one answers,
  so no total that exists today moves. Measured on the real-data check of 2026-10-28 (PR 249): the earlier
  three-term sum served HEI 3.4 million instead of 2.17 billion (its long-term line is
  ``LongTermDebtAndCapitalLeaseObligations``), no total for ORCL (``LongTermNotesPayable`` + ``DebtCurrent``), AAPL
  without its 7.98 billion of commercial paper, and JPM its 72 billion of short-term borrowings alone (its 461
  billion long-term line is the including-current tag above). The total sits on the period end of the newest line;
  its provenance is the newest-filed line's, and it is ``derived``. The old extraction's two-term ``total_debt`` is
  not served.
- **D&A in four rungs** (#271): the extraction's two (``DepreciationDepletionAndAmortization``, then
  ``DepreciationAmortizationAndAccretionNet``), else ``DepreciationAndAmortization``, else ``Depreciation`` plus
  ``AmortizationOfIntangibleAssets`` of the same span (``_depreciation``; ``derived`` only when a line is a YTD
  difference), else ``Depreciation`` alone when no goodwill or intangible balance is held. The old store's records
  keep the first two only. ``current_assets`` / ``current_liabilities`` are served as tagged.
- **EPS** (``eps_basic``, ``eps_diluted``: ``EarningsPerShareBasic`` / ``...Diluted``, else
  ``EarningsPerShareBasicAndDiluted``, unit ``USD/shares``), which the old extraction did not read: per period the
  first concept that has one, a reported quarter only (EPS is not additive, so no YTD difference).
- **The first-reported value** (#277 A2, avo 10 Nov 2026): next to the served value (the newest filing's), the value
  the first filing gave for the same tag and span, with that filing's accession and filed date (``first_value``,
  ``first_accn``, ``first_filed``). Equal to the served value unless a later filing restated it. A derived value (a
  YTD difference, a sum) has none: no filing ever reported it as such.
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
# #737: the debt balances read ONLY when the lines above give no total at all. Each tuple is a strict priority, not
# a sum: two of these tags can describe one borrowing (``SeniorNotes`` beside ``SecuredDebt``), and which pairs
# overlap is not countable from the facts. One line of a ladder is taken, the LARGEST positive one (``_best_of``),
# so the order below is only the grouping, never a preference. A filer that tags several genuinely different
# borrowings from one ladder is therefore still understated: that is a known gap, measured on 13 Nov 2026 and
# written up in the PR, not something this rule claims to solve.
FALLBACK_WHOLE_DEBT = ("debt_and_lease_total", "convertible_debt", "convertible_notes_payable", "senior_notes",
                       "secured_debt", "notes_payable", "line_of_credit", "other_lt_debt")
FALLBACK_LONG_TERM_DEBT = ("convertible_debt_noncurrent", "senior_notes_noncurrent", "loans_payable_noncurrent",
                           "secured_debt_noncurrent", "bank_loans_noncurrent", "line_of_credit_noncurrent",
                           "other_lt_debt_noncurrent")
FALLBACK_CURRENT_DEBT = ("line_of_credit_current", "loans_payable_current", "senior_notes_current",
                         "notes_payable_current")
FALLBACK_SHORT_TERM_DEBT = ("short_term_bank_loans",)
FALLBACK_DEBT_LINES = (FALLBACK_WHOLE_DEBT + FALLBACK_LONG_TERM_DEBT + FALLBACK_CURRENT_DEBT
                       + FALLBACK_SHORT_TERM_DEBT)
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
        ("source", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT"),
        ("first_value", "DOUBLE"), ("first_accn", "TEXT"), ("first_filed", "DATE")]
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
    balance sheet can all end on one day, so a lookup asks for its own span first (the Opus review's N5). Per
    ``(concept, start, end, unit)`` also the oldest ``(filed, accn, value)``: the first-reported value (#277 A2)."""

    def __init__(self, facts: dict):
        self.form: dict[str, str] = {}
        self.at_end: dict[tuple[str, str], tuple[str, str]] = {}
        self.at_span: dict[tuple[str, str, str], tuple[str, str]] = {}
        self.usd_at_span: dict[tuple[str, str, str], float] = {}
        self.first_at_span: dict[tuple[str, str, str, str], tuple[str, str, float]] = {}
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
                        val = f.get("val")
                        if isinstance(val, (int, float)) and (filed, accn) < self.first_at_span.get(
                                span + (unit,), ("~", "~", 0.0))[:2]:
                            self.first_at_span[span + (unit,)] = (filed, accn, float(val))
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


def _first(index: _Index, concepts, unit: str, start, end, served: tuple[str, str] | None) -> dict:
    """The first-reported columns of a served value (#277 A2): the oldest filing of the same span, in the tag the
    served filing used (else the first of ``concepts`` that has the span). Empty when the facts hold no such span."""
    spans = [(c, start or "", str(end), unit) for c in concepts if (c, start or "", str(end), unit) in index.first_at_span]
    if not spans:
        return {}
    own = next((s for s in spans if index.at_span.get(s[:3]) == served), spans[0])
    filed, accn, value = index.first_at_span[own]
    return {"first_value": value, "first_accn": accn, "first_filed": _day(filed)}


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
                      "derived": field in derived,
                      **({} if field in derived else _first(index, concepts, unit, starts.get(field), end, p))}
    da = None if rec.get("depreciation_suspect") else _depreciation(out, index)
    if da:
        out["depreciation"] = da
    balance_end = rec["period"] if quarterly else ends.get("total_assets")
    terms = _debt_lines(out, index, balance_end)
    if terms:
        newest = max(terms, key=lambda r: (r["filed"], r["accn"]))
        out["total_debt"] = {"field": "total_debt", "period_type": period_type, "fiscal_year": fiscal_year,
                             "period_start": None, "period_end": max(r["period_end"] for r in terms),
                             "value": sum(r["value"] for r in terms), "unit": "USD", "accn": newest["accn"],
                             "filed": newest["filed"], "form": newest["form"], "derived": True}
    return list(out.values())


# The balances whose presence means a filer has something to amortize (#271, avo 8 Nov 2026).
INTANGIBLE_BALANCES = ("Goodwill", "IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet",
                       "IndefiniteLivedIntangibleAssetsExcludingGoodwill", "IntangibleAssetsNetIncludingGoodwill")


def _depreciation(out: dict[str, dict], index: _Index) -> dict | None:
    """The D&A row when the extraction's two rungs gave none for the period (#271): rung 3
    ``DepreciationAndAmortization``, else rung 4 ``Depreciation`` + ``AmortizationOfIntangibleAssets`` of the same
    span, each line read once. ``Depreciation`` alone is the whole D&A only when the filer holds no goodwill or
    intangible balance (``INTANGIBLE_BALANCES``, none tagged or all zero) on the period's end: there is then
    nothing to amortize (avo, 8 Nov 2026: 305 of the 384 rung-4 filers with no amortization line). A negative line is
    never summed, and the sum is ``derived`` only when a line is (a YTD difference), so a sum of two reported
    quarters stays in the board's TTM. The caller skips a period the extraction flagged ``depreciation_suspect``."""
    if "depreciation" in out:
        return None
    if "depreciation_and_amortization" in out:
        return {**out["depreciation_and_amortization"], "field": "depreciation"}
    dep, amort = out.get("depreciation_tangible"), out.get("amortization_intangibles")
    if dep and not amort and dep["value"] >= 0:
        end = dep["period_end"].isoformat()
        held = [r for c in INTANGIBLE_BALANCES if (r := _tagged(index, c, end)) and r["value"]]
        return None if held else {**dep, "field": "depreciation"}
    if (not dep or not amort or dep["value"] < 0 or amort["value"] < 0
            or (dep["period_start"], dep["period_end"]) != (amort["period_start"], amort["period_end"])):
        return None
    newest = max((dep, amort), key=lambda r: (r["filed"], r["accn"]))
    return {**newest, "field": "depreciation", "value": dep["value"] + amort["value"],
            "derived": dep["derived"] or amort["derived"], "first_value": None, "first_accn": None, "first_filed": None}


def _debt_lines(out: dict[str, dict], index: _Index, balance_end: str | None) -> list[dict]:
    """The rows total debt sums (module docstring): each debt line of the balance sheet once, or none when no
    long-term line is tagged (a current line alone is not the whole, so the total stays unknown).

    Every line comes from the one balance sheet dated ``balance_end`` (the record's ``total_assets`` end; the newest
    debt line's end when it has none). An annual record is keyed by calendar year and each field keeps its own end,
    so a filer that moved its year end holds two balance dates in one record; a line of the other date is left out,
    as ``sec_edgar._fill_gross_profit`` leaves out a cost of another period (the Opus review's M1).

    Two passes (#737). The first reads the lines of #239 and is unchanged, so **a company that has a total today
    keeps exactly the value it has today**: the second pass runs only when the first returns nothing. The second
    adds the fallback ladders (``FALLBACK_*``), the balances a filer may tag instead of ``LongTermDebtNoncurrent``
    and its neighbours. Measured on the stored companyfacts, 12 Nov 2026 (claude-coordinator
    ``notes/revenuechaser/2026-11-12_1400_fundamentals-tags-and-ifrs-measurement.md``): 654 scored fiscal years
    carry no total, and 104 of them carry one of these balances. The rule that a current line alone is not the
    whole is **kept** in both passes, so a year whose only fallback line is a current one still has no total.
    """
    return (_lines_on_one_sheet(out, index, balance_end, DEBT_LINES, fallback=False)
            or _lines_on_one_sheet(out, index, balance_end, DEBT_LINES + FALLBACK_DEBT_LINES, fallback=True))


def _best_of(lines: dict[str, dict], fields: tuple[str, ...]) -> dict | None:
    """The largest positive line of a fallback ladder, or None (#737, the real-data check of 13 Nov 2026).

    **Largest, not first.** The ladder's own order says nothing about size, and a filer that tags several of these
    is served only one of them, so taking the first understated 14 of the 157 companies the fallback fills: BABA
    tags ``SeniorLongTermNotes`` 17.0 bn, ``ConvertibleDebtNoncurrent`` 8.1 bn and ``LongTermLoansFromBank`` 6.9 bn
    on 31 Mar 2026, and the first of those in the ladder is the 8.1 bn one. The largest is never a double count:
    whichever two of these tags overlap, the larger is the more complete statement of the same borrowing.

    **A zero line is skipped.** ``total_debt = 0`` is read downstream as debt-free, which a NULL is not, and a tag
    carried at zero said nothing: 26 of the 157 gains were zero totals, MHO's off a ``LineOfCredit`` of 0 while it
    carried ``OtherLongTermDebt`` 52 m. This applies to the fallback ladders only — a zero in the lines of #239 is
    a filer's own statement and still counts (NVIDIA's restated ``LongTermDebt`` of 0, #264).
    """
    found = [lines[f] for f in fields if f in lines and lines[f]["value"] > 0]
    return max(found, key=lambda r: r["value"]) if found else None


def _lines_on_one_sheet(out: dict[str, dict], index: _Index, balance_end: str | None, fields: tuple[str, ...],
                        fallback: bool) -> list[dict]:
    """One pass of ``_debt_lines`` over ``fields``; ``fallback`` adds the #737 ladders to each of the three slots."""
    lines = {f: r for f, r in out.items() if f in fields}
    if balance_end is None and lines:
        balance_end = max(r["period_end"] for r in lines.values()).isoformat()
    lines = {f: r for f, r in lines.items() if r["period_end"] == _day(balance_end)}
    short = (lines.get("short_term_borrowings") or _tagged(index, COMMERCIAL_PAPER, balance_end)
             or (_best_of(lines, FALLBACK_SHORT_TERM_DEBT) if fallback else None))
    short = [short] if short else []
    long_term = next((lines[f] for f in LONG_TERM_DEBT if f in lines), None)
    whole = lines.get("lt_debt_total") or _tagged(index, WHOLE_LONG_TERM_DEBT, balance_end)
    if fallback and long_term is None and whole is None:
        # The fallback asks for a WHOLE line before a noncurrent one, the other way round from the lines above.
        # There, the current portion has its own standard tag (``DebtCurrent``, ``LongTermDebtCurrent``) and a
        # noncurrent line plus that tag is the more exact pair. Here it has not: a filer that tags
        # ``ConvertibleDebt`` 300 and ``ConvertibleDebtNoncurrent`` 280 and no current tag at all states its whole
        # debt only in the first, and reading the second would quietly drop the 20 due within the year.
        whole = _best_of(lines, FALLBACK_WHOLE_DEBT)
        if whole is None:
            long_term = _best_of(lines, FALLBACK_LONG_TERM_DEBT)
    convertible = lines.get("convertible_debt_current")
    if long_term is not None:
        # #264: convertible notes due within the year are the current line only when no other current line is
        # tagged (DebtCurrent and LongTermDebtCurrent hold them when they are), so they are never read twice.
        current_line = (lines.get("lt_debt_current") or convertible
                        or (_best_of(lines, FALLBACK_CURRENT_DEBT) if fallback else None))
        current = [lines["debt_current"]] if "debt_current" in lines else ([current_line] if current_line else []) + short
        return [long_term] + current
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
                            "derived": False, **_first(index, concepts, unit, info.get("start"), end, p)})
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
