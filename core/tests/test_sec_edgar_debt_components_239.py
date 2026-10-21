"""#239: EDGAR debt component fields are stored wide; ``total_debt`` stays exactly as before.

"Data now, verdicts later" (decision 6-A on #159, user choice on #239): this build only
collects the components and the annual ``period_ends``. ``total_debt`` is still
``LongTermDebtNoncurrent + LongTermDebtCurrent`` with the same 0-fill, and no reader changes.
Synthetic companyfacts only, no network; balance-sheet facts are instants (no ``start``).
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import edgar_bulk
from RevenueChaser.core.providers import sec_edgar as se

_ENDS = {"annual": ("2022-12-31", "2023-12-31"), "quarterly": ("2023-03-31", "2023-06-30")}

# (field, tag): every tag the build adds. Several fields have two alternates, listed in priority order.
FIELD_TAGS = [
    ("debt_and_lease_total", "DebtAndCapitalLeaseObligations"),
    ("lt_debt_total", "LongTermDebt"),
    ("debt_current", "DebtCurrent"),
    ("short_term_borrowings", "ShortTermBorrowings"),
    ("lt_debt_and_lease_noncurrent", "LongTermDebtAndCapitalLeaseObligations"),
    ("convertible_debt_noncurrent", "ConvertibleDebtNoncurrent"),
    ("convertible_debt_noncurrent", "ConvertibleLongTermNotesPayable"),
    ("convertible_debt_current", "ConvertibleNotesPayableCurrent"),
    ("convertible_debt_current", "ConvertibleDebtCurrent"),
    ("notes_payable_noncurrent", "LongTermNotesPayable"),
    ("notes_payable_current", "NotesPayableCurrent"),
    ("line_of_credit", "LineOfCredit"),
    ("line_of_credit_noncurrent", "LongTermLineOfCredit"),
    ("finance_lease_noncurrent", "FinanceLeaseLiabilityNoncurrent"),
    ("finance_lease_current", "FinanceLeaseLiabilityCurrent"),
    ("operating_lease_noncurrent", "OperatingLeaseLiabilityNoncurrent"),
    ("operating_lease_current", "OperatingLeaseLiabilityCurrent"),
]
DEBT_FIELDS = sorted({field for field, _ in FIELD_TAGS})
NEW_TAGS = [tag for _, tag in FIELD_TAGS]
DEBT_FIELDS_NO_LEASE_DIAGNOSTIC = [f for f in DEBT_FIELDS if not f.startswith("operating_lease")]


def _instant(end: str, val: float, kind: str, filed: str | None = None) -> dict:
    fp = "FY" if kind == "annual" else "Q2"
    default_filed = f"{int(end[:4]) + 1}-02-01" if kind == "annual" else f"{end[:4]}-08-01"
    return {"end": end, "val": val, "form": "10-K" if kind == "annual" else "10-Q", "fp": fp,
            "filed": filed or default_filed}


def _facts(kind: str, **concepts) -> dict:
    """``{concept: {end: val}}`` -> companyfacts with balance-sheet instants (no ``start``)."""
    return {"entityName": "Synthetic Co", "facts": {"us-gaap": {
        name: {"units": {"USD": [_instant(e, v, kind) for e, v in series.items()]}}
        for name, series in concepts.items()}}}


def _recs(kind: str, facts: dict) -> dict:
    if kind == "annual":
        return {r["fiscal_year"]: r for r in se.extract_annual(facts)}
    return {r["period"]: r for r in se.extract_quarterly(facts)}


def _key(kind: str, end: str) -> str:
    return end[:4] if kind == "annual" else end


@pytest.fixture(params=["annual", "quarterly"])
def kind(request):
    return request.param


@pytest.fixture
def ends(kind):
    return _ENDS[kind]


# ── every new tag lands in its own field ────────────────────────────────────

@pytest.mark.parametrize("field, tag", FIELD_TAGS)
def test_each_new_tag_lands_in_its_field(kind, ends, field, tag):
    facts = _facts(kind, Assets={ends[1]: 1000.0}, **{tag: {ends[1]: 7.0}})
    rec = _recs(kind, facts)[_key(kind, ends[1])]
    assert rec[field] == 7.0
    assert [f for f in DEBT_FIELDS if f != field and rec[f] is not None] == []


def test_notespayablenoncurrent_is_not_a_us_gaap_element_and_is_not_mapped(kind, ends):
    facts = _facts(kind, Assets={ends[1]: 1000.0}, NotesPayableNoncurrent={ends[1]: 7.0})
    rec = _recs(kind, facts)[_key(kind, ends[1])]
    assert [f for f in DEBT_FIELDS if rec[f] is not None] == []


def test_line_of_credit_and_its_noncurrent_part_are_two_fields(kind, ends):
    facts = _facts(kind, Assets={ends[1]: 1000.0}, LineOfCredit={ends[1]: 50.0},
                   LongTermLineOfCredit={ends[1]: 30.0})
    rec = _recs(kind, facts)[_key(kind, ends[1])]
    assert rec["line_of_credit"] == 50.0  # the total of the current and noncurrent parts
    assert rec["line_of_credit_noncurrent"] == 30.0


# ── alternates inside one field resolve per period ──────────────────────────

@pytest.mark.parametrize("field, hi, lo", [
    ("convertible_debt_noncurrent", "ConvertibleDebtNoncurrent", "ConvertibleLongTermNotesPayable"),
    ("convertible_debt_current", "ConvertibleNotesPayableCurrent", "ConvertibleDebtCurrent"),
])
def test_the_higher_priority_alternate_wins_within_a_period(kind, ends, field, hi, lo):
    facts = _facts(kind, Assets={ends[1]: 1000.0}, **{hi: {ends[1]: 7.0}, lo: {ends[1]: 9.0}})
    assert _recs(kind, facts)[_key(kind, ends[1])][field] == 7.0


@pytest.mark.parametrize("field, hi, lo", [
    ("convertible_debt_noncurrent", "ConvertibleDebtNoncurrent", "ConvertibleLongTermNotesPayable"),
    ("convertible_debt_current", "ConvertibleNotesPayableCurrent", "ConvertibleDebtCurrent"),
])
def test_the_lower_alternate_is_used_in_a_period_where_the_higher_one_is_missing(kind, ends, field, hi, lo):
    old, new = ends
    facts = _facts(kind, Assets={old: 1000.0, new: 1000.0}, **{lo: {old: 4.0}, hi: {new: 7.0}})
    recs = _recs(kind, facts)
    assert recs[_key(kind, old)][field] == 4.0  # a per-series pick would keep only the newer tag
    assert recs[_key(kind, new)][field] == 7.0


# ── total_debt is byte-identical to main ────────────────────────────────────

_LT, _LTC = "LongTermDebtNoncurrent", "LongTermDebtCurrent"


@pytest.mark.parametrize("facts_kw, expected", [
    ({_LT: 10.0, _LTC: 2.0}, 12.0),
    ({_LT: 10.0}, 10.0),
    ({_LTC: 2.0}, 2.0),
    ({}, None),
])
def test_total_debt_is_still_the_two_part_sum(kind, ends, facts_kw, expected):
    series = {tag: {ends[1]: val} for tag, val in facts_kw.items()}
    rec = _recs(kind, _facts(kind, Assets={ends[1]: 1000.0}, **series))[_key(kind, ends[1])]
    assert rec["total_debt"] == expected


@pytest.mark.parametrize("facts_kw", [{_LT: 10.0}, {_LTC: 2.0}, {}, {_LT: 10.0, _LTC: 2.0}])
def test_total_debt_ignores_every_new_tag(kind, ends, facts_kw):
    base = {tag: {ends[1]: val} for tag, val in facts_kw.items()}
    with_new = dict(base, **{tag: {ends[1]: 999.0} for tag in NEW_TAGS})
    old = _recs(kind, _facts(kind, Assets={ends[1]: 1000.0}, **base))
    new = _recs(kind, _facts(kind, Assets={ends[1]: 1000.0}, **with_new))
    assert set(old) == set(new)
    for period in old:
        assert json.dumps(new[period]["total_debt"]) == json.dumps(old[period]["total_debt"])
        for field in ("lt_debt", "lt_debt_current"):
            assert new[period][field] == old[period][field]


# ── operating leases are diagnostic only ────────────────────────────────────

def test_operating_lease_tags_never_appear_in_a_debt_field(kind, ends):
    facts = _facts(kind, Assets={ends[1]: 1000.0}, OperatingLeaseLiabilityNoncurrent={ends[1]: 70.0},
                   OperatingLeaseLiabilityCurrent={ends[1]: 8.0})
    rec = _recs(kind, facts)[_key(kind, ends[1])]
    assert rec["operating_lease_noncurrent"] == 70.0
    assert rec["operating_lease_current"] == 8.0
    assert [f for f in DEBT_FIELDS_NO_LEASE_DIAGNOSTIC if rec[f] is not None] == []
    assert rec["total_debt"] is None


# ── no new field adds a record; every tag is a bulk concept ────────────────

@pytest.mark.parametrize("tag", NEW_TAGS)
def test_a_period_with_only_debt_component_facts_adds_no_record(kind, ends, tag):
    old, new = ends
    facts = _facts(kind, Assets={old: 1000.0}, **{tag: {new: 7.0}})
    assert set(_recs(kind, facts)) == {_key(kind, old)}


def test_every_new_tag_is_in_the_bulk_concepts():
    assert [t for t in NEW_TAGS if t not in edgar_bulk.USED_CONCEPTS] == []


# ── annual period_ends ──────────────────────────────────────────────────────

def _fy(end: str, val: float, start: str, filed: str) -> dict:
    return {"start": start, "end": end, "val": val, "form": "10-K", "fp": "FY", "filed": filed}


def test_period_ends_holds_each_fields_own_fact_end():
    facts = {"entityName": "Synthetic Co", "facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [_fy("2023-12-31", 800.0, "2023-01-01", "2024-02-01")]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [_instant("2023-12-30", 100.0, "annual")]}},
        "ShortTermInvestments": {"units": {"USD": [_instant("2023-12-30", 40.0, "annual")]}},
        "LongTermDebtNoncurrent": {"units": {"USD": [_instant("2023-12-30", 300.0, "annual")]}},
        "ConvertibleDebtNoncurrent": {"units": {"USD": [_instant("2023-12-30", 50.0, "annual")]}},
    }}}
    rec = _recs("annual", facts)["2023"]
    assert rec["period_ends"] == {"revenue": "2023-12-31", "cash": "2023-12-30",
                                  "short_term_investments": "2023-12-30",
                                  "lt_debt": "2023-12-30", "convertible_debt_noncurrent": "2023-12-30"}


def test_period_ends_has_only_fields_that_have_a_value():
    facts = _facts("annual", Assets={"2023-12-31": 1000.0})
    assert _recs("annual", facts)["2023"]["period_ends"] == {"total_assets": "2023-12-31"}


def test_two_fiscal_year_ends_in_one_calendar_year_keep_the_one_that_supplied_the_value():
    # A 52/53-week filer: FY2021 ends 2022-01-01 (filed early 2022), FY2022 ends 2022-12-31.
    # Both fall in calendar year 2022, so they share a record; the later filing wins, and
    # period_ends names ITS end, never the other fiscal year's and never just "2022".
    facts = {"entityName": "Synthetic Co", "facts": {"us-gaap": {
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
            _instant("2022-01-01", 90.0, "annual", filed="2022-03-01"),
            _instant("2022-12-31", 100.0, "annual", filed="2023-03-01")]}},
        "ConvertibleDebtNoncurrent": {"units": {"USD": [
            _instant("2022-01-01", 11.0, "annual", filed="2022-03-01")]}},
    }}}
    rec = _recs("annual", facts)["2022"]
    assert rec["cash"] == 100.0
    assert rec["period_ends"]["cash"] == "2022-12-31"
    assert rec["convertible_debt_noncurrent"] == 11.0
    assert rec["period_ends"]["convertible_debt_noncurrent"] == "2022-01-01"  # its own end, unpaired


def test_a_derived_gross_profit_ends_with_the_revenue_it_was_derived_from():
    facts = {"entityName": "Synthetic Co", "facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [_fy("2023-12-31", 800.0, "2023-01-01", "2024-02-01")]}},
        "CostOfRevenue": {"units": {"USD": [_fy("2023-12-31", 300.0, "2023-01-01", "2024-02-01")]}},
    }}}
    rec = _recs("annual", facts)["2023"]
    assert rec["gross_profit_derived"] is True
    assert rec["period_ends"]["gross_profit"] == "2023-12-31"
    assert rec["period_ends"]["cost_of_revenue"] == "2023-12-31"


def test_quarterly_records_carry_no_period_ends():
    facts = _facts("quarterly", Assets={"2023-06-30": 1000.0})
    assert "period_ends" not in _recs("quarterly", facts)["2023-06-30"]
