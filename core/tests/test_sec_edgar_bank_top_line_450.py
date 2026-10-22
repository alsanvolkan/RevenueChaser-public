"""#450: a bank's top line gets its own field, and ``revenue`` still means revenue.

A bank reports its income-statement top line as ``RevenuesNetOfInterestExpense`` (interest income net
of interest expense) or ``InterestAndDividendIncomeOperating`` (interest and dividend income). Neither
is the ASC 606 revenue the other fields, the growth curves and the peer comparison mean, so they are
read into a NEW field and never into ``revenue``: no filer's existing revenue value moves.

The rules under test:

- the new field's tags are picked by the module's own rule (newest year, then longest series, list
  order on ties, #246), with ``RevenuesNetOfInterestExpense`` listed first because it is the closer
  analogue of a top line when a filer reports both;
- ``CONCEPTS["revenue"]`` keeps exactly the four tags it had (#435), not one bank tag added;
- a filer that reports ``Revenues`` and a bank tag keeps today's revenue AND gains the new field;
- a filer with neither bank tag gets NO such key (not ``None``), so a reader cannot confuse "this
  filer reports no bank top line" with a stored zero;
- the quarterly path carries it too, from the same extractor, with no quarterly-specific code.

Synthetic companyfacts only, no network. Fixtures are the #246 helpers, as in #435.
"""
from __future__ import annotations

from datetime import date, timedelta

from RevenueChaser.core.providers import sec_edgar as se
from RevenueChaser.core.tests.test_sec_edgar_concept_merge_246 import _facts, _recs, _rev, _s

REV = "Revenues"
RFC = "RevenueFromContractWithCustomerExcludingAssessedTax"
SALES = "SalesRevenueNet"
RFC_INC = "RevenueFromContractWithCustomerIncludingAssessedTax"
RNOI = "RevenuesNetOfInterestExpense"
IODI = "InterestAndDividendIncomeOperating"
BANK = "interest_income_top_line"


def _bank(facts: dict) -> dict:
    """{fiscal_year: interest_income_top_line} for the records that hold one."""
    return {fy: r[BANK] for fy, r in _recs(facts).items() if r.get(BANK) is not None}


# ── the table: one new field, and ``revenue`` untouched ─────────────────────

def test_revenue_keeps_its_concepts_and_the_bank_tags_have_their_own_field():
    assert se.CONCEPTS["revenue"][0] == [REV, RFC, SALES, RFC_INC]
    assert se.CONCEPTS[BANK] == ([RNOI, IODI], "USD")
    assert not set(se.CONCEPTS["revenue"][0]) & {RNOI, IODI}


# ── a bank that reports only its top line gains the field and no revenue ────

def test_a_bank_tag_alone_fills_the_new_field_and_leaves_revenue_empty():
    facts = _facts(flow={IODI: _s([2024, 2025], 20000)})
    recs = _recs(facts)
    assert _bank(facts) == {"2024": 22024.0, "2025": 22025.0}
    assert all(r["revenue"] is None for r in recs.values())
    assert recs["2025"]["period_ends"][BANK] == "2025-12-31"


# ── the list order decides a tie, and only a tie ────────────────────────────

def test_net_of_interest_expense_wins_when_the_two_tags_cover_the_same_years():
    facts = _facts(flow={RNOI: _s([2024, 2025], 10000), IODI: _s([2024, 2025], 20000)})
    assert _bank(facts) == {"2024": 12024.0, "2025": 12025.0}


def test_the_modules_newest_then_longest_pick_still_beats_the_list_order():
    # The order above is a tie-break, not a priority: a filer that switched tags keeps the newer
    # series as the primary and the older tag only adds the contiguous year below it (#246).
    facts = _facts(flow={RNOI: _s([2022, 2023], 10000), IODI: _s([2023, 2024, 2025], 20000)})
    assert _bank(facts) == {"2022": 12022.0, "2023": 22023.0, "2024": 22024.0, "2025": 22025.0}


# ── the acceptance test: no existing revenue value moves ────────────────────

def test_revenue_is_exactly_what_it_is_today_and_the_new_field_is_filled_beside_it():
    # The bank tag is newer AND longer than ``Revenues`` here, so a bank tag added to
    # ``CONCEPTS["revenue"]`` would take the pick and this filer's revenue would move.
    facts = _facts(flow={REV: _s([2020, 2021], 30000), IODI: _s(range(2020, 2026), 20000)})
    assert _rev(facts) == {"2020": 32020.0, "2021": 32021.0}
    assert _bank(facts) == {str(y): 20000.0 + y for y in range(2020, 2026)}


# ── absent is absent, never zero ─────────────────────────────────────────────

def test_a_filer_with_neither_bank_tag_gets_no_key_at_all():
    facts = _facts(flow={REV: _s([2024, 2025], 30000)})
    recs = _recs(facts)
    assert len(recs) == 2
    assert all(BANK not in r for r in recs.values())


# ── the quarterly path falls out of the same extractor ─────────────────────

def test_the_quarterly_path_carries_the_field_with_no_quarterly_specific_code():
    def q(end: str, val: float) -> dict:
        start = (date.fromisoformat(end) - timedelta(days=90)).isoformat()
        return {"start": start, "end": end, "val": val, "form": "10-Q", "fp": "Q2",
                "filed": f"{end[:4]}-08-01"}
    facts = {"entityName": "Synthetic Bank", "facts": {"us-gaap": {
        RNOI: {"units": {"USD": [q("2025-03-31", 1.0), q("2025-06-30", 2.0)]}}}}}
    got = {r["period"]: r[BANK] for r in se.extract_quarterly(facts)}
    assert got == {"2025-03-31": 1.0, "2025-06-30": 2.0}
    assert all(r["revenue"] is None for r in se.extract_quarterly(facts))
