"""#435: the ASC 606 including-assessed-tax revenue tag is read, last in the list.

341 US filers have a fresh annual record but no revenue value in their newest years. The
measured cause is a tag gap: their newest annual top line is
``RevenueFromContractWithCustomerIncludingAssessedTax``, the sibling of
``RevenueFromContractWithCustomerExcludingAssessedTax`` — the two differ only by the assessed
tax. Adding it must not move a filer whose including-tax series is the OLDER one (AMH, BESS,
WOLF), so the sibling goes LAST: the newest year wins the pick, and on a tie the narrower
excluding-tax figure wins.

Bank concepts (``InterestAndDividendIncomeOperating``, ``RevenuesNetOfInterestExpense``) stay
out of scope: interest income is a different definition of the top line.

Synthetic companyfacts only, no network. Helpers are the #246 fixtures.
"""
from __future__ import annotations

from RevenueChaser.core.providers import sec_edgar as se
from RevenueChaser.core.tests.test_sec_edgar_concept_merge_246 import _facts, _recs, _rev, _s

REV = "Revenues"
RFC = "RevenueFromContractWithCustomerExcludingAssessedTax"
RFC_INC = "RevenueFromContractWithCustomerIncludingAssessedTax"
SALES = "SalesRevenueNet"


def _akam_acel_facts() -> dict:
    """The AKAM / ACEL shape: the old tag up to 2018, the including-tax tag from 2019 on.

    The old series is the LONGER one (9 years against 7), so a pick that preferred length
    would keep 2018 — only "newest year first" reaches 2025.
    """
    return _facts(flow={SALES: _s(range(2010, 2019), 10000), RFC_INC: _s(range(2019, 2026), 20000)})


# ── C1: the newest year comes from the newly read tag ───────────────────────

def test_c1_the_newest_year_comes_from_the_including_tax_sibling():
    """C1 — today the newest revenue year of this filer is 2018; it must become 2025."""
    recs = _recs(_akam_acel_facts())
    assert max(recs) == "2025"
    assert recs["2025"]["revenue"] == 22025.0
    assert recs["2025"]["period_ends"]["revenue"] == "2025-12-31"


# ── C2: the #246 backfill still restores the old tag's history ──────────────

def test_c2_the_pre_2019_years_keep_their_salesrevenuenet_values():
    """C2 — the new tag's series is primary, so the old tag only adds years it lacks (#246)."""
    assert _rev(_akam_acel_facts()) == {**{str(y): 10000.0 + y for y in range(2010, 2019)},
                                        **{str(y): 20000.0 + y for y in range(2019, 2026)}}


# ── C3: shapes whose numbers must not move ──────────────────────────────────

def test_c3_the_excluding_tax_sibling_wins_when_both_tax_tags_cover_the_same_years():
    """C3 — on a tie the narrower figure wins: one value per year, never the including-tax one."""
    facts = _facts(flow={RFC: _s(range(2020, 2025), 10000), RFC_INC: _s(range(2020, 2025), 20000)})
    assert _rev(facts) == {str(y): 10000.0 + y for y in range(2020, 2025)}


def test_c3_an_older_including_tax_series_moves_nothing():
    """C3 — the AMH / BESS / WOLF shape: the including-tax series stops BEFORE the pick's newest year."""
    facts = _facts(flow={REV: _s(range(2016, 2021), 10000), RFC_INC: _s(range(2016, 2019), 20000)})
    assert _rev(facts) == {str(y): 10000.0 + y for y in range(2016, 2021)}
    assert max(_recs(facts)) == "2020"


# ── C4 / C5: what still yields no revenue ───────────────────────────────────

def test_c4_a_pre_revenue_filer_still_yields_no_revenue():
    """C4 — a filer that tags no revenue concept has records and still no revenue value."""
    facts = _facts(flow={"CostOfRevenue": _s([2024], 500)}, inst={"Assets": _s([2023, 2024], 1000)})
    recs = _recs(facts)
    assert recs and all(r["revenue"] is None for r in recs.values())
    assert _rev(facts) == {}


def test_c5_a_bank_concept_only_filer_still_yields_no_revenue():
    """C5 — interest income is a different top line and stays out of scope, so no revenue appears."""
    facts = _facts(flow={"InterestAndDividendIncomeOperating": _s([2024], 900),
                         "RevenuesNetOfInterestExpense": _s([2024], 800)},
                   inst={"Assets": _s([2024], 1000)})
    recs = _recs(facts)
    assert recs and all(r["revenue"] is None for r in recs.values())
    assert _rev(facts) == {}
