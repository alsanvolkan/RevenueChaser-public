"""#450: a bank's top line gets its own fields, and ``revenue`` still means revenue.

A bank reports its income-statement top line as ``RevenuesNetOfInterestExpense`` (interest income net
of interest expense) or ``InterestAndDividendIncomeOperating`` (gross interest and dividend income).
Neither is the ASC 606 revenue the other fields, the growth curves and the peer comparison mean, so
they are read into NEW fields and never into ``revenue``: no filer's existing revenue value moves.

They are TWO fields, one tag each, not one field with two tags. The two tags differ by the whole
interest expense line, so a single field would let the #246 backfill take a year from the other tag
and store a series that steps wherever a filer switched tags — the same reason the two revenue tax
tags may not merge (``sec_edgar.py``, the ``CONCEPTS`` comment).

The rules under test:

- each field carries exactly its own tag, and ``CONCEPTS["revenue"]`` keeps exactly the four tags it
  had (#435), with no bank tag added;
- neither bank tag ever contributes a year to the other field;
- a filer that reports ``Revenues`` and a bank tag keeps today's revenue AND gains the new field;
- a filer with neither bank tag gets NO such key (not ``None``) on an annual or a quarterly record,
  so a reader cannot confuse "this filer reports no bank top line" with a stored zero — while a
  filer that reports a real zero keeps the key;
- neither field adds a record on its own (``_NEVER_A_RECORD``);
- neither field reaches ``period_ends``, so neither can move ``fiscal_year.period_end`` — which
  decides the staleness and adjacency gates behind a card;
- the quarterly path carries them from the same extractor, including the YTD-derived quarters, and a
  negative derived quarter is kept (these tags can be genuinely negative).

Synthetic companyfacts only, no network. Fixtures are the #246 and #158 helpers, as in #435.
"""
from __future__ import annotations

from datetime import date, timedelta

from RevenueChaser.core import fiscal_year
from RevenueChaser.core.providers import sec_edgar as se
from RevenueChaser.core.tests.test_sec_edgar_concept_merge_246 import _facts, _recs, _rev, _s
from RevenueChaser.core.tests.test_sec_edgar_quarters_158 import _f, _q
from RevenueChaser.core.tests.test_sec_edgar_quarters_158 import _facts as _qfacts

REV = "Revenues"
RFC = "RevenueFromContractWithCustomerExcludingAssessedTax"
SALES = "SalesRevenueNet"
RFC_INC = "RevenueFromContractWithCustomerIncludingAssessedTax"
RNOI = "RevenuesNetOfInterestExpense"
IODI = "InterestAndDividendIncomeOperating"
NET = "revenue_net_of_interest_expense"
GROSS = "interest_and_dividend_income"
NI = "NetIncomeLoss"


def _field(facts: dict, field: str) -> dict:
    """``{fiscal_year: value}`` for the records that hold ``field``."""
    return {fy: r[field] for fy, r in _recs(facts).items() if r.get(field) is not None}


# ── the table: two new fields, one tag each, and ``revenue`` untouched ──────

def test_revenue_keeps_its_concepts_and_each_bank_tag_has_its_own_field():
    assert se.CONCEPTS["revenue"][0] == [REV, RFC, SALES, RFC_INC]
    assert se.CONCEPTS[NET] == ([RNOI], "USD")
    assert se.CONCEPTS[GROSS] == ([IODI], "USD")
    assert not set(se.CONCEPTS["revenue"][0]) & {RNOI, IODI}


def test_neither_bank_tag_can_take_a_year_from_the_other():
    # A filer that switched tags: net of interest expense 2022-2023, gross income 2023-2025. One
    # field with both tags would store 2022 from one and 2023-2025 from the other, stepping by the
    # whole interest expense line at the seam. Two fields cannot.
    facts = _facts(flow={RNOI: _s([2022, 2023], 10000), IODI: _s([2023, 2024, 2025], 20000),
                         NI: _s(range(2020, 2026), 500)})
    assert _field(facts, NET) == {"2022": 12022.0, "2023": 12023.0}
    assert _field(facts, GROSS) == {"2023": 22023.0, "2024": 22024.0, "2025": 22025.0}


def test_a_filer_reporting_both_tags_fills_both_fields_with_their_own_values():
    facts = _facts(flow={RNOI: _s([2024, 2025], 10000), IODI: _s([2024, 2025], 20000),
                         NI: _s(range(2020, 2026), 500)})
    assert _field(facts, NET) == {"2024": 12024.0, "2025": 12025.0}
    assert _field(facts, GROSS) == {"2024": 22024.0, "2025": 22025.0}


# ── a bank that reports only its top line gains the field and no revenue ────

def test_a_bank_tag_alone_fills_its_field_and_leaves_revenue_empty():
    # A bank also reports net income, which is what makes the period a record: the bank top line
    # only rides on it.
    facts = _facts(flow={IODI: _s([2024, 2025], 20000), NI: _s([2024, 2025], 500)})
    recs = _recs(facts)
    assert _field(facts, GROSS) == {"2024": 22024.0, "2025": 22025.0}
    assert all(r["revenue"] is None for r in recs.values())


# ── the acceptance test: no existing revenue value moves ────────────────────

def test_revenue_is_exactly_what_it_is_today_and_the_new_field_is_filled_beside_it():
    # The bank tag is newer AND longer than ``Revenues`` here, so a bank tag added to
    # ``CONCEPTS["revenue"]`` would take the pick and this filer's revenue would move.
    facts = _facts(flow={REV: _s([2020, 2021], 30000), IODI: _s(range(2020, 2026), 20000),
                         NI: _s(range(2020, 2026), 500)})
    assert _rev(facts) == {"2020": 32020.0, "2021": 32021.0}
    assert _field(facts, GROSS) == {str(y): 20000.0 + y for y in range(2020, 2026)}


# ── absent is absent; a reported zero is a value ────────────────────────────

def test_a_filer_with_neither_bank_tag_gets_no_key_at_all():
    facts = _facts(flow={REV: _s([2024, 2025], 30000)})
    recs = _recs(facts)
    assert len(recs) == 2
    assert all(NET not in r and GROSS not in r for r in recs.values())


def test_a_filer_with_neither_bank_tag_gets_no_key_on_a_quarterly_record_either():
    """The quarterly path must omit the key too: ``_ttm_metrics`` reads quarterly records."""
    recs = _q(_qfacts(**{REV: [_f("2025-01-01", "2025-03-31", 10.0, "2025-05-01", "a-01")]}))
    assert recs
    assert all(NET not in r and GROSS not in r for r in recs.values())


def test_a_reported_zero_keeps_its_key_and_is_not_an_absent_field():
    """The whole point of ``_OMIT_WHEN_ABSENT``: "reports none" and "reports 0" must differ."""
    facts = _facts(flow={RNOI: {"2025-12-31": 0.0}, NI: _s([2025], 500)})
    rec = _recs(facts)["2025"]
    assert NET in rec
    assert rec[NET] == 0.0


def test_neither_bank_field_ever_creates_a_record():
    """Both are in ``_NEVER_A_RECORD``: a period holding only them is no period (#450)."""
    assert _recs(_facts(flow={IODI: _s([2024, 2025], 20000)})) == {}
    assert _recs(_facts(flow={RNOI: _s([2024, 2025], 10000)})) == {}
    assert se._record_periods({NET: {"2024-12-31": 1.0}, GROSS: {"2024-12-31": 2.0}}) == []


# ── the fields stay out of ``period_ends``, so the period end cannot move ──

def _flow_ending(end: str, val: float) -> dict:
    start = (date.fromisoformat(end) - timedelta(days=364)).isoformat()
    return {"start": start, "end": end, "val": val, "form": "10-K", "fp": "FY",
            "filed": f"{int(end[:4]) + 1}-03-01"}


def test_a_bank_field_is_absent_from_period_ends_and_leaves_the_period_end_alone():
    """``fiscal_year.period_end`` is ``ends.get("revenue") or max(ends.values())``. A bank has no
    ``revenue``, so any end date these fields put in ``period_ends`` would move the record's period
    end — and with it ``latest_is_stale``, the card anchor and ``prior()``'s 350-380 day adjacency.
    The fields have no reader, so they must not reach that ``max()``.
    """
    facts = {"entityName": "Synthetic Bank", "facts": {"us-gaap": {
        NI: {"units": {"USD": [_flow_ending("2025-12-28", 500.0)]}},
        RNOI: {"units": {"USD": [_flow_ending("2025-12-31", 900.0)]}}}}}
    rec = _recs(facts)["2025"]
    assert rec[NET] == 900.0                       # the value is stored
    assert NET not in rec["period_ends"]           # its end date is not
    assert rec["period_ends"]["net_income"] == "2025-12-28"
    assert fiscal_year.period_end(rec) == date(2025, 12, 28)


# ── the quarterly path falls out of the same extractor ─────────────────────

def test_the_quarterly_path_carries_the_field_with_no_quarterly_specific_code():
    def q(end: str, val: float) -> dict:
        start = (date.fromisoformat(end) - timedelta(days=90)).isoformat()
        return {"start": start, "end": end, "val": val, "form": "10-Q", "fp": "Q2",
                "filed": f"{end[:4]}-08-01"}
    facts = {"entityName": "Synthetic Bank", "facts": {"us-gaap": {
        RNOI: {"units": {"USD": [q("2025-03-31", 1.0), q("2025-06-30", 2.0)]}},
        NI: {"units": {"USD": [q("2025-03-31", 9.0), q("2025-06-30", 9.0)]}}}}}
    got = {r["period"]: r[NET] for r in se.extract_quarterly(facts)}
    assert got == {"2025-03-31": 1.0, "2025-06-30": 2.0}
    assert all(r["revenue"] is None for r in se.extract_quarterly(facts))


def test_the_ytd_derived_quarter_path_runs_for_a_bank_field():
    """A 3M and a 6M fact sharing a start: Q2 is the difference, marked derived, YTD kept.

    Net income rides along because the bank field adds no record of its own, on the quarterly path
    as on the annual one.
    """
    recs = _q(_qfacts(**{
        RNOI: [_f("2025-01-01", "2025-03-31", 10.0, "2025-05-01", "a-01", fp="Q1"),
               _f("2025-01-01", "2025-06-30", 25.0, "2025-08-01", "a-02", fp="Q2")],
        NI: [_f("2025-01-01", "2025-03-31", 9.0, "2025-05-01", "a-01", fp="Q1"),
             _f("2025-01-01", "2025-06-30", 18.0, "2025-08-01", "a-02", fp="Q2")]}))
    assert recs["2025-03-31"][NET] == 10.0
    assert recs["2025-06-30"][NET] == 15.0
    assert NET in recs["2025-06-30"]["derived"]
    ytd = recs["2025-06-30"]["ytd"][NET]
    assert ytd["months"] == 6
    assert ytd["start"] == "2025-01-01"
    assert ytd["val"] == 25.0


def test_a_negative_derived_bank_quarter_is_kept_and_carries_no_suspect_flag():
    """Unlike ``revenue``, ``RevenuesNetOfInterestExpense`` can be genuinely negative (net interest
    expense plus securities losses), so it is deliberately out of ``_NON_NEGATIVE_FLOWS``: a
    negative YTD difference is stored as it is, not dropped and flagged.
    """
    assert NET not in se._NON_NEGATIVE_FLOWS
    assert GROSS not in se._NON_NEGATIVE_FLOWS
    recs = _q(_qfacts(**{
        RNOI: [_f("2025-01-01", "2025-03-31", 50.0, "2025-05-01", "a-01", fp="Q1"),
               _f("2025-01-01", "2025-06-30", 90.0, "2025-08-01", "a-02", fp="Q2"),
               _f("2025-01-01", "2025-09-30", 130.0, "2025-11-01", "a-03", fp="Q3"),
               _f("2025-01-01", "2025-12-31", 100.0, "2026-02-15", "a-04", form="10-K", fp="FY")],
        NI: [_f("2025-01-01", "2025-03-31", 9.0, "2025-05-01", "a-01", fp="Q1"),
             _f("2025-01-01", "2025-06-30", 18.0, "2025-08-01", "a-02", fp="Q2"),
             _f("2025-01-01", "2025-09-30", 27.0, "2025-11-01", "a-03", fp="Q3"),
             _f("2025-01-01", "2025-12-31", 36.0, "2026-02-15", "a-04", form="10-K", fp="FY")]}))
    assert recs["2025-12-31"][NET] == -30.0
    assert f"{NET}_suspect" not in recs["2025-12-31"]
