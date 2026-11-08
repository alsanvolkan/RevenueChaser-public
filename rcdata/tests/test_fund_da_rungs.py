"""#271 and #105 (data half, avo 5 Nov 2026): the D&A rungs 3 and 4, current assets and liabilities, and the utility
revenue tag, as the fundamentals layer serves them (``facts.fact_rows``)."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata.build.fundamentals import facts

from . import fund_fixture as fx

END, START, ACCN, FILED = "2025-12-31", "2025-01-01", "0000000001-26-000001", "2026-02-01"


def _filer(**tags):
    """One FY2025 10-K with ``tags`` (USD): flows over the year, instants (``AssetsCurrent``...) at its end."""
    instants = {"AssetsCurrent", "LiabilitiesCurrent"}
    gaap = {"Revenues": {"units": {"USD": [fx._fact(1_000, END, ACCN, FILED, "10-K", "FY", 2025, START)]}}}
    for tag, val in tags.items():
        start = None if tag in instants else START
        gaap[tag] = {"units": {"USD": [fx._fact(val, END, ACCN, FILED, "10-K", "FY", 2025, start)]}}
    return {"cik": 1, "entityName": "X", "facts": {"us-gaap": gaap}}


def _value(doc, field):
    rows = {(r["field"], r["period_type"], r["period_end"]): r for r in facts.fact_rows(doc, "1")[0]}
    row = rows.get((field, "annual", date(2025, 12, 31)))
    return row and (row["value"], row["derived"], row["accn"])


@pytest.mark.parametrize("tags, served", [
    ({"DepreciationDepletionAndAmortization": 50, "DepreciationAndAmortization": 70}, (50, False)),  # rung 1 holds
    ({"DepreciationAmortizationAndAccretionNet": 55, "DepreciationAndAmortization": 70}, (55, False)),  # rung 2
    ({"DepreciationAndAmortization": 70, "Depreciation": 40, "AmortizationOfIntangibleAssets": 20}, (70, False)),
    ({"Depreciation": 40, "AmortizationOfIntangibleAssets": 20}, (60, False)),      # rung 4: a sum, each line once
    # Depreciation alone leaves amortization out, so it is no D&A (default chosen, change if avo prefers: 384 of
    # the 792 rung-4 filers of 6 Nov 2026 tag no AmortizationOfIntangibleAssets).
    ({"Depreciation": 40}, None),
    ({"AmortizationOfIntangibleAssets": 20}, None),
])
def test_depreciation_reads_the_rungs_in_order(tags, served):
    got = _value(_filer(**tags), "depreciation")
    assert (got and got[:2]) == served
    if served:
        assert got[2] == ACCN


def test_the_old_extraction_keeps_its_two_rungs():
    # The old store's records do not move: rungs 3 and 4 are the layer's (``facts.py``).
    annual = sec_edgar.extract_annual(_filer(DepreciationAndAmortization=70))
    assert annual[-1]["depreciation"] is None


def test_current_assets_and_liabilities_are_served():
    doc = _filer(AssetsCurrent=300, LiabilitiesCurrent=120)
    assert _value(doc, "current_assets")[:2] == (300, False)
    assert _value(doc, "current_liabilities")[:2] == (120, False)


def test_the_utility_total_adds_a_year_and_never_takes_one():
    gaap = {
        "Revenues": {"units": {"USD": [fx._fact(900, "2024-12-31", ACCN, FILED, "10-K", "FY", 2024, "2024-01-01")]}},
        "RegulatedAndUnregulatedOperatingRevenue": {"units": {"USD": [
            fx._fact(950, "2024-12-31", ACCN, FILED, "10-K", "FY", 2024, "2024-01-01"),
            fx._fact(1_100, END, ACCN, FILED, "10-K", "FY", 2025, START)]}},
    }
    by_year = {r["fiscal_year"]: r["revenue"] for r in sec_edgar.extract_annual(
        {"cik": 1, "entityName": "X", "facts": {"us-gaap": gaap}})}
    assert by_year == {"2024": 900, "2025": 1_100}             # 2024 stays the Revenues value; 2025 is added


def test_the_utility_total_is_the_revenue_of_a_filer_that_tags_nothing_else():
    gaap = {"RegulatedAndUnregulatedOperatingRevenue": {"units": {"USD": [
        fx._fact(1_100, END, ACCN, FILED, "10-K", "FY", 2025, START)]}}}
    annual = sec_edgar.extract_annual({"cik": 1, "entityName": "X", "facts": {"us-gaap": gaap}})
    assert [r["revenue"] for r in annual] == [1_100]


def test_the_utility_total_never_takes_a_quarterly_pick():
    q = lambda v, end, start: fx._fact(v, end, ACCN, FILED, "10-Q", "Q1", 2025, start)
    gaap = {
        "Revenues": {"units": {"USD": [q(200, "2025-03-31", "2025-01-01")]}},
        "RegulatedAndUnregulatedOperatingRevenue": {"units": {"USD": [
            q(210, "2025-03-31", "2025-01-01"), q(220, "2025-06-30", "2025-04-01")]}},   # newer and longer
    }
    quarterly = sec_edgar.extract_quarterly({"cik": 1, "entityName": "X", "facts": {"us-gaap": gaap}})
    assert {r["period"]: r["revenue"] for r in quarterly} == {"2025-03-31": 200}


def _quarter_rows(gaap):
    rows = facts.fact_rows({"cik": 1, "entityName": "X", "facts": {"us-gaap": gaap}}, "1")[0]
    return {(r["field"], r["period_end"]): r for r in rows if r["period_type"] == "quarterly"}


def test_a_rung_4_sum_of_two_reported_quarters_is_not_derived():
    # Review of PR 545: the Fundamentals board drops a derived quarter from its TTM (module_fundamentals/pipeline.py
    # _is_derived), and a sum of two reported 3M lines is no YTD difference.
    q = lambda v: fx._fact(v, "2025-03-31", ACCN, FILED, "10-Q", "Q1", 2025, "2025-01-01")
    gaap = {"Revenues": {"units": {"USD": [q(1_000)]}}, "Depreciation": {"units": {"USD": [q(10)]}},
            "AmortizationOfIntangibleAssets": {"units": {"USD": [q(5)]}}}
    row = _quarter_rows(gaap)[("depreciation", date(2025, 3, 31))]
    assert (row["value"], row["derived"]) == (15, False)


def test_a_negative_line_is_never_summed():
    assert _value(_filer(Depreciation=40, AmortizationOfIntangibleAssets=-20), "depreciation") is None
    assert _value(_filer(Depreciation=-40, AmortizationOfIntangibleAssets=20), "depreciation") is None


def test_a_lower_rung_never_fills_a_period_the_extraction_called_suspect():
    # Rung 1's Q4 (FY minus 9M) came out negative: the extraction drops it and flags depreciation_suspect, so no rung
    # serves that quarter either.
    k = lambda v, end, start, fp, form="10-Q": fx._fact(v, end, ACCN, FILED, form, fp, 2025, start)
    gaap = {"Revenues": {"units": {"USD": [k(1_000, "2025-12-31", "2025-10-01", "Q4", "10-K")]}},
            "DepreciationDepletionAndAmortization": {"units": {"USD": [
                k(30, "2025-09-30", "2025-01-01", "Q3"), k(20, "2025-12-31", "2025-01-01", "FY", "10-K")]}},
            "DepreciationAndAmortization": {"units": {"USD": [k(13, "2025-12-31", "2025-10-01", "Q4", "10-K")]}}}
    assert sec_edgar.extract_quarterly({"cik": 1, "entityName": "X", "facts": {"us-gaap": gaap}})[-1].get(
        "depreciation_suspect")
    assert ("depreciation", date(2025, 12, 31)) not in _quarter_rows(gaap)
