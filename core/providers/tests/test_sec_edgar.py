"""SEC EDGAR extract_annual testleri (synthetic facts, ağ yok)."""
from __future__ import annotations

import pytest

from RevenueChaser.core.providers import sec_edgar as se


def _facts() -> dict:
    return {
        "entityName": "Test Co",
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [
                    {"start": "2022-01-01", "end": "2022-12-31", "val": 100.0, "form": "10-K", "fp": "FY", "filed": "2023-02-01"},
                    {"start": "2023-01-01", "end": "2023-12-31", "val": 123.0, "form": "10-K", "fp": "FY", "filed": "2024-02-01"},
                    {"start": "2023-01-01", "end": "2023-03-31", "val": 30.0, "form": "10-K", "fp": "FY", "filed": "2024-02-01"},  # çeyrek -> dışlanır
                ]}},
                "GrossProfit": {"units": {"USD": [
                    {"start": "2022-01-01", "end": "2022-12-31", "val": 50.0, "form": "10-K", "fp": "FY", "filed": "2023-02-01"},
                    {"start": "2023-01-01", "end": "2023-12-31", "val": 61.5, "form": "10-K", "fp": "FY", "filed": "2024-02-01"},
                ]}},
                "OperatingIncomeLoss": {"units": {"USD": [
                    {"start": "2023-01-01", "end": "2023-12-31", "val": 20.0, "form": "10-K", "fp": "FY", "filed": "2024-02-01"},
                ]}},
                "LongTermDebtNoncurrent": {"units": {"USD": [
                    {"start": "2023-01-01", "end": "2023-12-31", "val": 10.0, "form": "10-K", "fp": "FY", "filed": "2024-02-01"},
                ]}},
                "LongTermDebtCurrent": {"units": {"USD": [
                    {"start": "2023-01-01", "end": "2023-12-31", "val": 2.0, "form": "10-K", "fp": "FY", "filed": "2024-02-01"},
                ]}},
            }
        },
    }


def test_extract_annual():
    annual = se.extract_annual(_facts())
    assert [r["fiscal_year"] for r in annual] == ["2022", "2023"]
    latest = annual[-1]
    assert latest["revenue"] == 123.0  # çeyreklik 30.0 dışlanmış
    assert latest["gross_profit"] == 61.5
    assert latest["ebit"] == 20.0
    assert latest["total_debt"] == 12.0  # 10 + 2


def test_is_us_filer():
    assert se._is_us_filer(_facts()) is True
    # yabancı filer: 20-F formu
    foreign = _facts()
    for f in foreign["facts"]["us-gaap"]["Revenues"]["units"]["USD"]:
        f["form"] = "20-F"
    assert se._is_us_filer(foreign) is False


def test_concept_fallback_prefers_recent():
    facts = _facts()
    # ASC606 konsepti daha yeni yıla sahip -> onu seçmeli
    facts["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"] = {
        "units": {"USD": [
            {"start": "2024-01-01", "end": "2024-12-31", "val": 999.0, "form": "10-K", "fp": "FY", "filed": "2025-02-01"},
        ]}
    }
    annual = se.extract_annual(facts)
    assert annual[-1]["revenue"] == 999.0
    # #246: the older tag's years are no longer dropped, they sit below the newest year
    assert [(r["fiscal_year"], r["revenue"]) for r in annual] == [("2022", 100.0), ("2023", 123.0), ("2024", 999.0)]


def test_get_fundamentals_no_fetch_cache_miss(rawstore_tmp_db):
    """model-only refresh: no network when EDGAR cache is absent."""
    annual, quarterly, meta = se.get_fundamentals("ZZZZ", no_fetch=True)
    assert annual == [] and quarterly == []
    assert meta.get("available") is False


def test_get_fundamentals_no_fetch_serves_cache(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    rawstore.write_rows("edgar_financials", [{
        "ticker": "AAA", "annual": [{"revenue": 100}], "quarterly": [], "meta": {"available": True},
    }])
    annual, _, meta = se.get_fundamentals("AAA", no_fetch=True)
    assert annual == [{"revenue": 100}]
    assert meta.get("available") is True


# ── #234: gross_profit derived from revenue − cost of revenue ──
# Every fact below is synthetic; annual facts are keyed by fiscal year, quarterly
# facts by period end. The tests run once per kind ("annual" / "quarterly").
_PERIODS = {"annual": ("2022", "2023"), "quarterly": ("2023-03-31", "2023-06-30")}
_QSTART = {"2023-03-31": "2023-01-01", "2023-06-30": "2023-04-01"}
COST_PRIORITY = ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices"]
EXCLUDED_COSTS = ["CostsAndExpenses", "OperatingCostsAndExpenses",
                  "CostOfGoodsAndServicesSoldAndOperatingExpenses"]


def _fact(period: str, val: float) -> dict:
    if len(period) == 4:
        return {"start": f"{period}-01-01", "end": f"{period}-12-31", "val": val,
                "form": "10-K", "fp": "FY", "filed": f"{int(period) + 1}-02-01"}
    return {"start": _QSTART[period], "end": period, "val": val,
            "form": "10-Q", "fp": "Q2", "filed": "2023-08-01"}


def _facts_of(**concepts) -> dict:
    """``{concept: {period: val}}`` → a minimal companyfacts payload."""
    return {"entityName": "Synthetic Co", "facts": {"us-gaap": {
        name: {"units": {"USD": [_fact(p, v) for p, v in series.items()]}}
        for name, series in concepts.items()}}}


def _recs(kind: str, facts: dict) -> dict:
    if kind == "annual":
        return {r["fiscal_year"]: r for r in se.extract_annual(facts)}
    return {r["period"]: r for r in se.extract_quarterly(facts)}


@pytest.fixture(params=["annual", "quarterly"])
def kind(request):
    return request.param


@pytest.fixture
def periods(kind):
    return _PERIODS[kind]


def test_gross_profit_derived_from_revenue_minus_cost(kind, periods):
    p1, p2 = periods
    recs = _recs(kind, _facts_of(Revenues={p1: 100.0, p2: 123.0}, CostOfRevenue={p1: 60.0, p2: 40.0}))
    assert recs[p2]["gross_profit"] == pytest.approx(83.0)
    assert recs[p2]["gross_profit_derived"] is True
    assert recs[p2]["cost_of_revenue"] == 40.0
    assert recs[p1]["gross_profit"] == pytest.approx(40.0)
    assert "gross_profit_suspect" not in recs[p2]


@pytest.mark.parametrize("reported", [60.0, 0.0])
def test_reported_gross_profit_always_wins(kind, periods, reported):
    """A filer that tags GrossProfit keeps it, even when a cost concept implies another number."""
    p = periods[1]
    recs = _recs(kind, _facts_of(Revenues={p: 100.0}, GrossProfit={p: reported}, CostOfRevenue={p: 10.0}))
    assert recs[p]["cost_of_revenue"] == 10.0  # extracted, but not used
    assert recs[p]["gross_profit"] == reported
    assert "gross_profit_derived" not in recs[p]


@pytest.mark.parametrize("concept", COST_PRIORITY)
def test_each_cost_concept_alone_derives(kind, periods, concept):
    p = periods[1]
    recs = _recs(kind, _facts_of(Revenues={p: 100.0}, **{concept: {p: 30.0}}))
    assert recs[p]["gross_profit"] == pytest.approx(70.0)
    assert recs[p]["gross_profit_derived"] is True


@pytest.mark.parametrize("hi,lo", [(COST_PRIORITY[i], COST_PRIORITY[j])
                                   for i in range(4) for j in range(i + 1, 4)])
def test_higher_priority_cost_concept_wins_within_a_period(kind, periods, hi, lo):
    p = periods[1]
    recs = _recs(kind, _facts_of(Revenues={p: 100.0}, **{lo: {p: 90.0}, hi: {p: 30.0}}))
    assert recs[p]["cost_of_revenue"] == 30.0
    assert recs[p]["gross_profit"] == pytest.approx(70.0)


def test_cost_priority_is_resolved_per_period_not_per_series(kind, periods):
    """The generic picker takes the longest series; cost of revenue is chosen period by period."""
    p1, p2 = periods
    facts = _facts_of(Revenues={p1: 100.0, p2: 100.0},
                      CostOfRevenue={p2: 30.0},
                      CostOfGoodsAndServicesSold={p1: 55.0, p2: 80.0})
    recs = _recs(kind, facts)
    assert recs[p2]["cost_of_revenue"] == 30.0  # the higher priority concept, where it has the period
    assert recs[p1]["cost_of_revenue"] == 55.0  # the fallback, where it does not


@pytest.mark.parametrize("concept", EXCLUDED_COSTS)
def test_excluded_cost_concepts_are_never_used(kind, periods, concept):
    """These contain operating expenses; using them would overstate cost and understate margin."""
    p = periods[1]
    recs = _recs(kind, _facts_of(Revenues={p: 100.0}, **{concept: {p: 80.0}}))
    assert recs[p]["gross_profit"] is None
    assert recs[p]["cost_of_revenue"] is None
    assert "gross_profit_derived" not in recs[p]


@pytest.mark.parametrize("concept", EXCLUDED_COSTS)
def test_excluded_cost_concept_does_not_outrank_a_valid_one(kind, periods, concept):
    p = periods[1]
    recs = _recs(kind, _facts_of(Revenues={p: 100.0}, CostOfServices={p: 30.0}, **{concept: {p: 80.0}}))
    assert recs[p]["cost_of_revenue"] == 30.0
    assert recs[p]["gross_profit"] == pytest.approx(70.0)


@pytest.mark.parametrize("revenue", [0.0, -10.0])
def test_no_derivation_when_revenue_is_not_positive(kind, periods, revenue):
    p = periods[1]
    recs = _recs(kind, _facts_of(Revenues={p: revenue}, CostOfRevenue={p: 40.0}))
    assert recs[p]["cost_of_revenue"] == 40.0
    assert recs[p]["gross_profit"] is None
    assert "gross_profit_derived" not in recs[p]


def test_no_derivation_when_revenue_is_missing(kind, periods):
    p = periods[1]
    recs = _recs(kind, _facts_of(CostOfRevenue={p: 40.0}, NetIncomeLoss={p: 5.0}))
    assert recs[p]["revenue"] is None
    assert recs[p]["cost_of_revenue"] == 40.0
    assert recs[p]["gross_profit"] is None


def test_no_derivation_when_cost_is_missing(kind, periods):
    p = periods[1]
    recs = _recs(kind, _facts_of(Revenues={p: 100.0}))
    assert recs[p]["cost_of_revenue"] is None
    assert recs[p]["gross_profit"] is None
    assert "gross_profit_derived" not in recs[p]
    assert "gross_profit_suspect" not in recs[p]


def test_negative_cost_gives_none_and_a_suspect_flag(kind, periods):
    """A sign-flipped cost is unreliable: no value, flagged, never abs()."""
    p = periods[1]
    recs = _recs(kind, _facts_of(Revenues={p: 100.0}, CostOfRevenue={p: -30.0}))
    assert recs[p]["gross_profit"] is None
    assert recs[p]["gross_profit_suspect"] is True
    assert "gross_profit_derived" not in recs[p]


def test_revenue_and_cost_of_different_periods_are_never_paired(kind, periods):
    p1, p2 = periods
    recs = _recs(kind, _facts_of(Revenues={p2: 100.0}, CostOfRevenue={p1: 40.0},
                                 NetIncomeLoss={p1: 1.0, p2: 1.0}))
    assert recs[p1]["cost_of_revenue"] == 40.0 and recs[p2]["revenue"] == 100.0
    assert recs[p2]["gross_profit"] is None
    assert recs[p1]["gross_profit"] is None
    assert "gross_profit_derived" not in recs[p1] and "gross_profit_derived" not in recs[p2]


def test_a_cost_gap_in_one_period_does_not_borrow_from_its_neighbour(kind, periods):
    p1, p2 = periods
    recs = _recs(kind, _facts_of(Revenues={p1: 100.0, p2: 120.0}, CostOfRevenue={p1: 40.0}))
    assert recs[p1]["gross_profit"] == pytest.approx(60.0)
    assert recs[p2]["gross_profit"] is None


def test_cost_exceeding_revenue_is_a_real_negative_gross_profit(kind, periods):
    p = periods[1]
    recs = _recs(kind, _facts_of(Revenues={p: 100.0}, CostOfRevenue={p: 130.0}))
    assert recs[p]["gross_profit"] == pytest.approx(-30.0)
    assert recs[p]["gross_profit_derived"] is True


def test_a_cost_only_period_does_not_add_a_record(kind, periods):
    """Records exist for the same periods as before #234: the readers take ``[-1]`` as the latest."""
    p1, p2 = periods
    facts = _facts_of(Revenues={p1: 100.0}, CostOfRevenue={p1: 40.0, p2: 50.0})
    assert list(_recs(kind, facts)) == [p1]


def test_zero_cost_of_revenue_is_a_valid_cost(kind, periods):
    """Cost 0 is not a sign error: the margin is 100%, derived and not suspect."""
    p = periods[1]
    recs = _recs(kind, _facts_of(Revenues={p: 100.0}, CostOfRevenue={p: 0.0}))
    assert recs[p]["gross_profit"] == pytest.approx(100.0)
    assert recs[p]["gross_profit_derived"] is True
    assert "gross_profit_suspect" not in recs[p]


# A 52/53-week filer can have two fiscal years ending in one calendar year (2023-01-01 and
# 2023-12-31). Annual records are keyed by the calendar year of the period end, so both land in
# slot "2023"; revenue and cost are only paired when their facts end on the same date.
def _fy_fact(start: str, end: str, val: float, filed: str) -> dict:
    return {"start": start, "end": end, "val": val, "form": "10-K", "fp": "FY", "filed": filed}


def _raw_facts(**concepts) -> dict:
    return {"entityName": "52/53 Co", "facts": {"us-gaap": {
        name: {"units": {"USD": rows}} for name, rows in concepts.items()}}}


def _annual_2023(facts: dict) -> dict:
    return {r["fiscal_year"]: r for r in se.extract_annual(facts)}["2023"]


def test_revenue_and_cost_ending_on_different_dates_are_not_paired():
    rec = _annual_2023(_raw_facts(
        Revenues=[_fy_fact("2023-01-02", "2023-12-31", 100.0, "2024-03-01")],
        CostOfRevenue=[_fy_fact("2022-01-02", "2023-01-01", 150.0, "2023-03-01")]))
    assert rec["gross_profit"] is None
    assert "gross_profit_derived" not in rec and "gross_profit_suspect" not in rec


def test_a_restated_cost_of_the_prior_fiscal_year_is_not_paired_with_this_years_revenue():
    """The later-filed restatement of FY ending 2023-01-01 wins the cost slot; revenue's slot is the newer FY."""
    rec = _annual_2023(_raw_facts(
        Revenues=[_fy_fact("2022-01-02", "2023-01-01", 100.0, "2023-03-01"),
                  _fy_fact("2023-01-02", "2023-12-31", 120.0, "2024-03-01")],
        CostOfRevenue=[_fy_fact("2022-01-02", "2023-01-01", 150.0, "2024-06-01"),
                       _fy_fact("2023-01-02", "2023-12-31", 60.0, "2024-03-01")]))
    assert rec["gross_profit"] is None
    assert "gross_profit_derived" not in rec


def test_matching_period_ends_still_derive_for_a_52_53_week_filer():
    rec = _annual_2023(_raw_facts(
        Revenues=[_fy_fact("2022-01-02", "2023-01-01", 100.0, "2023-03-01"),
                  _fy_fact("2023-01-02", "2023-12-31", 120.0, "2024-03-01")],
        CostOfRevenue=[_fy_fact("2022-01-02", "2023-01-01", 40.0, "2023-03-01"),
                       _fy_fact("2023-01-02", "2023-12-31", 60.0, "2024-03-01")]))
    assert rec["gross_profit"] == pytest.approx(60.0)
    assert rec["gross_profit_derived"] is True
