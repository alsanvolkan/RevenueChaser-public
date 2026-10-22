"""#435: the ASC 606 including-assessed-tax revenue tag only ADDS years the older tags lack.

341 US filers have a fresh annual record but no revenue value in their newest years, and 25 of 39
measured tag only ``RevenueFromContractWithCustomerIncludingAssessedTax`` in the newest annual
period. The gate then measured that letting that tag compete for the primary pick REGRESSES filers
that already had revenue (Regal Rexnord FY2022 3.81bn became 5.22bn, AST SpaceMobile FY2024 moved by
-68%, one filer lost eleven years), because the tag can win the pick and then replace years the older
tags already reported. The two tags do NOT differ only by the assessed tax, so neither figure is
"the narrow one".

The rule under test: the tag never takes over a year the older revenue tags (``Revenues``,
``...ExcludingAssessedTax``, ``SalesRevenueNet``) already hold. The pick stays exactly as it was at
6250a89 over those three, and the including-tax series then adds every year that pick lacks (older,
inside a hole, or newer). When the older tags hold no series at all, the including-tax series IS the
primary, so the target class keeps gaining revenue.

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


# ── C1: the target class gains every year it reports ────────────────────────

def test_c1_the_newest_year_comes_from_the_including_tax_sibling():
    """C1 — today the newest revenue year of this filer is 2018; it must become 2025.

    No old tag reports any year, so the including-tax series is the primary.
    """
    recs = _recs(_akam_acel_facts())
    assert max(recs) == "2025"
    assert recs["2025"]["revenue"] == 22025.0
    assert recs["2025"]["period_ends"]["revenue"] == "2025-12-31"


# ── C2: the #246 backfill still restores the old tag's history ──────────────

def test_c2_the_pre_2019_years_keep_their_salesrevenuenet_values():
    """C2 — every SalesRevenueNet year keeps its value, and 2019-2025 are added."""
    assert _rev(_akam_acel_facts()) == {**{str(y): 10000.0 + y for y in range(2010, 2019)},
                                        **{str(y): 20000.0 + y for y in range(2019, 2026)}}


# ── C3: shapes whose numbers must not move ──────────────────────────────────

def test_c3_the_excluding_tax_sibling_wins_when_both_tax_tags_cover_the_same_years():
    """C3 — one value per year, and on a shared year it is the older tag's, never the new one's."""
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


# ── F1 / R1 / R2: the tag only adds years, never replaces one ───────────────

def test_f1_the_longer_including_tax_series_never_replaces_an_overlapping_year():
    """F1 — both siblings over 2024-2025: those two keep the older tag's figure, 2019-2023 are added.

    Every year's value names its source (``base + year``): 2024-2025 must keep the excluding-tax
    figure (``10000 + y``) they already held, and 2019-2023 are the years only the including-tax
    tag reports (``20000 + y``).
    """
    facts = _facts(flow={RFC: _s(range(2024, 2026), 10000), RFC_INC: _s(range(2019, 2026), 20000)})
    assert _rev(facts) == {
        "2019": 22019.0, "2020": 22020.0, "2021": 22021.0, "2022": 22022.0, "2023": 22023.0,
        "2024": 12024.0, "2025": 12025.0}
    assert max(_recs(facts)) == "2025"


def test_r1_a_newer_and_longer_including_tax_series_only_adds_the_missing_years():
    """R1 — the Regal Rexnord shape the gate measured: the new tag ends later and covers 2018-2026.

    It would win the newest-year/longest pick, so it must be kept out of the pick: 2019-2023 keep
    the Revenues figure (``10000 + y``), and only 2018, 2024, 2025 and 2026 are added from it
    (``20000 + y``). Nothing above the old series existed before, so this is the one shape where
    revenue does grow upward.
    """
    facts = _facts(flow={REV: _s(range(2019, 2024), 10000), RFC_INC: _s(range(2018, 2027), 20000)})
    assert _rev(facts) == {
        "2018": 22018.0,
        "2019": 12019.0, "2020": 12020.0, "2021": 12021.0, "2022": 12022.0, "2023": 12023.0,
        "2024": 22024.0, "2025": 22025.0, "2026": 22026.0}
    assert max(_recs(facts)) == "2026"


def test_r2_a_hole_in_the_old_tag_series_is_filled_and_no_other_year_moves():
    """R2 — the no-sibling shape: the old tag has a hole at 2021 that only the new tag reports.

    The #246 backfill cannot close it (no other OLD tag reports 2021), so the hole is exactly what
    the including-tax addition is for. Every other year keeps the Revenues figure.
    """
    facts = _facts(flow={REV: _s([2018, 2019, 2020, 2022, 2023], 10000),
                         RFC_INC: _s([2021], 20000)})
    assert _rev(facts) == {
        "2018": 12018.0, "2019": 12019.0, "2020": 12020.0, "2021": 22021.0,
        "2022": 12022.0, "2023": 12023.0}
    assert max(_recs(facts)) == "2023"
