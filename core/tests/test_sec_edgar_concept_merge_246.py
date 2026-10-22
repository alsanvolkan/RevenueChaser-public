"""#246: a filer's tag switch must not drop the old tag's history (annual series only).

The rule under test: the primary series is exactly today's pick (newest year, then longest, list
order on ties) and keeps every year it holds, value and end date. Other tags may only add a year
the primary lacks: a hole inside the primary's span, or a contiguous run directly below its
earliest year. Each added year must end 350-380 days from its adjacent year (52/53-week filers
included), so a key one year apart that is two fiscal years apart is never chained. The chain stops
at the first year that is missing or fails. Quarterly series are not merged.

Synthetic companyfacts only, no network. ``CONCEPTS["revenue"]`` lists, in order:
``Revenues``, ``RevenueFromContractWithCustomerExcludingAssessedTax``, ``SalesRevenueNet``, and
last ``RevenueFromContractWithCustomerIncludingAssessedTax``, which never competes for the pick: it
only adds the years those three lack (#435).
"""
from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from RevenueChaser.core.providers import sec_edgar as se

REV = "Revenues"
RFC = "RevenueFromContractWithCustomerExcludingAssessedTax"
SALES = "SalesRevenueNet"
_LT = "LongTermDebtNoncurrent"
_LTC_FIRST = "LongTermDebtCurrent"
_LTC_SECOND = "LongTermDebtAndCapitalLeaseObligationsCurrent"
_COST = "CostOfRevenue"


def _flow(end: str, val: float) -> dict:
    start = (date.fromisoformat(end) - timedelta(days=364)).isoformat()
    return {"start": start, "end": end, "val": val, "form": "10-K", "fp": "FY",
            "filed": f"{int(end[:4]) + 1}-03-01"}


def _instant(end: str, val: float) -> dict:
    return {"end": end, "val": val, "form": "10-K", "fp": "FY", "filed": f"{int(end[:4]) + 1}-03-01"}


def _facts(flow: dict | None = None, inst: dict | None = None) -> dict:
    """``{concept: {end: val}}`` -> companyfacts (flow facts carry a ``start``, instants do not)."""
    gaap = {name: {"units": {"USD": [_flow(e, v) for e, v in series.items()]}}
            for name, series in (flow or {}).items()}
    gaap.update({name: {"units": {"USD": [_instant(e, v) for e, v in series.items()]}}
                 for name, series in (inst or {}).items()})
    return {"entityName": "Synthetic Co", "facts": {"us-gaap": gaap}}


def _s(years, base: float) -> dict:
    """December year ends -> ``base + year``, so a value names the tag that supplied it."""
    return {f"{y}-12-31": float(base + y) for y in years}


def _recs(facts: dict) -> dict:
    return {r["fiscal_year"]: r for r in se.extract_annual(facts)}


def _rev(facts: dict) -> dict:
    return {fy: r["revenue"] for fy, r in _recs(facts).items() if r["revenue"] is not None}


def _expect(**by_base) -> dict:
    """``_expect(**{"10000": range(2017, 2025), "20000": [2025]})`` -> {year: base + year}."""
    return {str(y): float(int(base) + y) for base, years in by_base.items() for y in years}


# ── the tag switch keeps the old tag's contiguous history ───────────────────

def test_a_contiguous_history_below_the_new_tag_is_restored():
    facts = _facts(flow={SALES: _s(range(2017, 2025), 10000), REV: _s([2025, 2026], 20000)})
    assert _rev(facts) == _expect(**{"10000": range(2017, 2025), "20000": [2025, 2026]})


def test_a_one_year_new_tag_gets_the_whole_old_history_below_it():
    facts = _facts(flow={SALES: _s(range(2017, 2026), 10000), REV: _s([2026], 20000)})
    assert _rev(facts) == _expect(**{"10000": range(2017, 2026), "20000": [2026]})


def test_a_missing_year_below_the_primary_is_never_bridged():
    # KTCC shape: the old tag stops at 2024, the new tag starts at 2026. 2025 is in neither.
    facts = _facts(flow={SALES: _s(range(2017, 2025), 10000), REV: _s([2026], 20000)})
    assert _rev(facts) == _expect(**{"20000": [2026]})


def test_the_chain_crosses_two_old_tags():
    facts = _facts(flow={REV: _s([2025, 2026], 10000), RFC: _s([2023, 2024], 20000),
                         SALES: _s([2021, 2022], 30000)})
    assert _rev(facts) == _expect(**{"30000": [2021, 2022], "20000": [2023, 2024],
                                     "10000": [2025, 2026]})


# ── holes inside the primary's span ─────────────────────────────────────────

def test_a_hole_inside_the_primary_span_is_filled_and_the_primary_keeps_its_own_years():
    facts = _facts(flow={REV: _s([2019, 2021, 2022], 20000), SALES: _s([2018, 2020, 2021], 10000)})
    assert _rev(facts) == _expect(**{"10000": [2018, 2020], "20000": [2019, 2021, 2022]})


@pytest.mark.parametrize("old_years, filled", [([2019], False), ([2019, 2020], True)])
def test_a_two_year_hole_is_filled_only_when_the_whole_run_closes(old_years, filled):
    facts = _facts(flow={REV: _s([2018, 2021, 2022], 20000), SALES: _s(old_years, 10000)})
    expected = {"20000": [2018, 2021, 2022]}
    if filled:
        expected["10000"] = old_years
    assert _rev(facts) == _expect(**expected)


def test_a_hole_candidate_must_be_adjacent_to_the_older_neighbour():
    # 2020 ends one year before the 2021 year end but 549 days after the 2019 one (a June year end).
    facts = _facts(flow={REV: {"2019-06-30": 1.0, "2021-12-31": 2.0}, SALES: {"2020-12-31": 5.0}})
    assert _rev(facts) == {"2019": 1.0, "2021": 2.0}


def test_a_hole_candidate_must_be_adjacent_to_the_newer_neighbour():
    facts = _facts(flow={REV: {"2019-12-31": 1.0, "2021-06-30": 2.0}, SALES: {"2020-12-31": 5.0}})
    assert _rev(facts) == {"2019": 1.0, "2021": 2.0}


# ── conflicts ───────────────────────────────────────────────────────────────

def test_on_a_conflict_the_primary_wins_even_when_the_other_tag_is_earlier_in_the_list():
    facts = _facts(flow={REV: _s([2023, 2024], 10000), RFC: _s([2023, 2024, 2025], 20000)})
    assert _rev(facts) == _expect(**{"20000": [2023, 2024, 2025]})


def test_two_backfill_tags_on_one_year_resolve_in_list_order():
    facts = _facts(flow={SALES: _s([2024, 2025], 30000), REV: _s([2022, 2023], 10000),
                         RFC: _s([2022, 2023], 20000)})
    assert _rev(facts) == _expect(**{"10000": [2022, 2023], "30000": [2024, 2025]})


def test_a_candidate_whose_end_fails_is_skipped_for_the_next_tag():
    facts = _facts(flow={RFC: _s([2024, 2025], 20000), REV: {"2023-06-30": 1.0},
                         SALES: {"2023-12-31": 3.0}})
    rec = _recs(facts)["2023"]
    assert rec["revenue"] == 3.0
    assert rec["period_ends"]["revenue"] == "2023-12-31"


# ── the end-date guard (52/53-week and fiscal-year-end changes) ─────────────

def test_a_52_53_week_boundary_collision_is_not_chained():
    # The old tag's key "2015" ends 2015-01-03: the FY2014 of a 52/53-week filer, 728 days before
    # the primary's earliest year end. FY2015 itself is shadowed by the "2016" key.
    facts = _facts(flow={REV: {"2016-12-31": 1.0, "2017-12-30": 2.0, "2018-12-29": 3.0},
                         SALES: {"2015-01-03": 9.0}})
    assert _rev(facts) == {"2016": 1.0, "2017": 2.0, "2018": 3.0}


def test_a_52_53_week_chain_with_364_and_371_day_steps_is_added():
    facts = _facts(flow={REV: {"2024-12-28": 4.0},
                         SALES: {"2023-12-30": 3.0, "2022-12-24": 2.0, "2021-12-25": 1.0}})
    assert _rev(facts) == {"2021": 1.0, "2022": 2.0, "2023": 3.0, "2024": 4.0}


def test_a_fiscal_year_end_change_at_the_boundary_stops_the_chain():
    # December year ends, then June: 182 days between 2019-12-31 and 2020-06-30.
    facts = _facts(flow={REV: {"2020-06-30": 4.0, "2021-06-30": 5.0},
                         SALES: _s([2017, 2018, 2019], 10000)})
    assert _rev(facts) == {"2020": 4.0, "2021": 5.0}


@pytest.mark.parametrize("days, added", [(349, False), (350, True), (380, True), (381, False)])
def test_the_adjacency_window_is_350_to_380_days(days, added):
    older = date(2022, 6, 15)
    newer = older + timedelta(days=days)
    facts = _facts(flow={REV: {newer.isoformat(): 2.0}, SALES: {older.isoformat(): 1.0}})
    assert _rev(facts) == ({"2022": 1.0, "2023": 2.0} if added else {"2023": 2.0})


def test_an_unparseable_end_is_never_added():
    bad = {"units": {"USD": [{"start": "2022-01-01", "end": "2023-99-99", "val": 3.0, "form": "10-K",
                              "fp": "FY", "filed": "2024-03-01"}]}}
    facts = _facts(flow={REV: {"2024-12-31": 4.0}})
    facts["facts"]["us-gaap"][SALES] = bad
    assert _rev(facts) == {"2024": 4.0}


def test_a_backfilled_value_carries_the_end_of_the_fact_that_supplied_it():
    facts = _facts(flow={REV: {"2024-12-28": 4.0}, SALES: {"2023-12-30": 3.0}})
    recs = _recs(facts)
    assert recs["2023"]["period_ends"]["revenue"] == "2023-12-30"
    assert recs["2024"]["period_ends"]["revenue"] == "2024-12-28"


# ── what must not change ────────────────────────────────────────────────────

def test_one_concept_is_never_filled_from_itself():
    facts = _facts(flow={REV: _s([2017, 2019, 2020], 10000)})
    assert _rev(facts) == _expect(**{"10000": [2017, 2019, 2020]})


def test_one_concept_covering_all_years_is_unchanged():
    facts = _facts(flow={REV: _s(range(2017, 2027), 10000)})
    assert _rev(facts) == _expect(**{"10000": range(2017, 2027)})


def test_quarterly_series_are_not_merged():
    def q(end, val):
        start = (date.fromisoformat(end) - timedelta(days=90)).isoformat()
        return {"start": start, "end": end, "val": val, "form": "10-Q", "fp": "Q2", "filed": f"{end[:4]}-08-01"}
    facts = {"entityName": "Synthetic Co", "facts": {"us-gaap": {
        SALES: {"units": {"USD": [q("2022-03-31", 1.0), q("2022-06-30", 2.0)]}},
        REV: {"units": {"USD": [q("2022-09-30", 3.0), q("2022-12-31", 4.0)]}}}}}
    got = {r["period"]: r["revenue"] for r in se.extract_quarterly(facts) if r["revenue"] is not None}
    assert got == {"2022-09-30": 3.0, "2022-12-31": 4.0}


def test_the_newest_record_never_changes_when_an_older_tag_is_added():
    base = {REV: _s([2025, 2026], 20000)}
    with_old = {**base, SALES: _s(range(2017, 2025), 10000)}
    assert se.extract_annual(_facts(flow=with_old))[-1] == se.extract_annual(_facts(flow=base))[-1]


def test_total_debt_gains_a_backfilled_current_portion_in_existing_records_too():
    # 2021 and 2022 are existing records (the long-term debt is there already): the derived total_debt of
    # such a record moves by exactly the backfilled current portion. The newest record (2024) is the pick's
    # own and is never touched: it is the one the readers take.
    inst = {_LT: {f"{y}-12-31": 100.0 for y in range(2021, 2025)},
            _LTC_FIRST: {"2021-12-31": 5.0, "2022-12-31": 6.0},
            _LTC_SECOND: {"2023-12-31": 7.0, "2024-12-31": 8.0}}
    recs = _recs(_facts(inst=inst))
    assert {y: recs[y]["lt_debt"] for y in ("2021", "2022", "2023", "2024")} == dict.fromkeys(
        ("2021", "2022", "2023", "2024"), 100.0)
    assert {y: recs[y]["total_debt"] for y in ("2021", "2022", "2023", "2024")} == {
        "2021": 105.0, "2022": 106.0, "2023": 107.0, "2024": 108.0}


def test_the_gross_margin_pairing_still_needs_the_same_end():
    flow = {REV: {"2024-12-31": 100.0}, SALES: {"2022-12-31": 80.0, "2023-12-31": 90.0},
            _COST: {"2022-12-30": 40.0, "2023-12-31": 45.0, "2024-12-31": 50.0}}
    recs = _recs(_facts(flow=flow))
    assert recs["2023"]["gross_profit"] == 45.0 and recs["2023"]["gross_profit_derived"] is True
    assert recs["2024"]["gross_profit"] == 50.0
    assert recs["2022"]["gross_profit"] is None  # cost ends 2022-12-30, restored revenue 2022-12-31


# ── property: today's pick is only ever extended ────────────────────────────

_REVENUE_TAGS = se.CONCEPTS["revenue"][0]
_INCLUDING_TAX = "RevenueFromContractWithCustomerIncludingAssessedTax"
_OLD_REVENUE_TAGS = tuple(t for t in _REVENUE_TAGS if t != _INCLUDING_TAX)


def _pick_parts(facts: dict) -> tuple[dict, dict]:
    """``(primary, backfilled)`` of today's pick over the THREE older revenue tags, as at 6250a89.

    One series wins: the newest year, then the longest, then list order; ``_backfill_history``
    extends it with the other OLD tags' holes and contiguous runs. The including-tax tag (#435) is
    left out on purpose: it may only ADD years to this result, never change one it holds, which is
    the regression this oracle has to catch.
    """
    best: dict = {}
    found: list = []
    for concept in _OLD_REVENUE_TAGS:
        series = se._annual_facts(facts, [concept], "USD")
        if not series:
            continue
        found.append(series)
        if not best or max(series) > max(best) or (max(series) == max(best) and len(series) > len(best)):
            best = series
    return best, (se._backfill_history(best, found) if best else best)


def _today_pick(facts: dict) -> dict:
    """The 6250a89 pick: the primary series plus the years ``_backfill_history`` added."""
    return _pick_parts(facts)[1]


def _days(newer_end: str, older_end: str) -> int:
    return (date.fromisoformat(newer_end) - date.fromisoformat(older_end)).days


def _random_facts(rng: random.Random) -> dict:
    flow = {}
    for tag in _REVENUE_TAGS:
        if rng.random() < 0.3:
            continue
        first = rng.randint(2012, 2020)
        years = [y for y in range(first, first + rng.randint(1, 10)) if rng.random() > 0.15]
        series = {}
        for y in years:
            roll = rng.random()
            end = (f"{y}-06-30" if roll < 0.08 else f"{y}-01-03" if roll < 0.14
                   else f"{y}-12-27" if roll < 0.30 else f"{y}-12-31")
            series[end] = float(rng.randint(1, 10 ** 6))
        if series:
            flow[tag] = series
    return _facts(flow=flow)


@pytest.mark.parametrize("seed", range(600))
def test_property_todays_pick_is_only_ever_extended(seed):
    facts = _random_facts(random.Random(seed))
    primary, old = _pick_parts(facts)  # the three old tags only: the 6250a89 pick
    inc = se._annual_facts(facts, [_INCLUDING_TAX], "USD")  # what the #435 tag reports
    new = se._annual_facts(facts, _REVENUE_TAGS, "USD")
    assert {y: new[y] for y in old} == old  # no year the old tags hold changes, value or end
    if not old:  # the target class: no old tag, so the including-tax series IS the primary
        assert new == inc
        return
    assert set(new) == set(old) | set(inc)  # the new tag only ADDS years ...
    assert all(new[y] == inc[y] for y in set(new) - set(old))  # ... with its own values
    # #246 still governs the years _backfill_history added (neither the primary's nor the #435 tag's):
    # they sit below or inside the primary's span, and no gap is bridged.
    added = set(old) - set(primary)
    assert all(y < min(primary) or min(primary) < y < max(primary) for y in added)  # nothing above, only holes and below
    primary_pairs = set(zip(sorted(primary), sorted(primary)[1:]))
    merged = sorted(old)
    assert all((a, b) in primary_pairs for a, b in zip(merged, merged[1:]) if int(b) - int(a) > 1)  # no new gap
    for y in added:  # every added year ends one fiscal year from each neighbour that exists
        for neighbour in (str(int(y) - 1), str(int(y) + 1)):
            if neighbour in old:
                older, newer = sorted((y, neighbour))
                assert 350 <= _days(old[newer][1], old[older][1]) <= 380


def test_the_property_run_adds_something():
    # Guards the property above against a generator that never exercises the merge.
    grown = backfilled = 0
    for seed in range(600):
        facts = _random_facts(random.Random(seed))
        primary, old = _pick_parts(facts)
        grown += len(se._annual_facts(facts, _REVENUE_TAGS, "USD")) > len(old)
        backfilled += bool(set(old) - set(primary))
    assert grown >= 50
    # ... and against one where the restored #246 assertions would hold vacuously (no added year)
    assert backfilled >= 50

