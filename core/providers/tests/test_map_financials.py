"""Mapping of UW financials into R3/R7 records — None vs 0 semantics.

Pins: a missing balance/cash-flow field maps to None (never 0), an explicit 0 is
kept, records are sorted newest-first by fiscal_date and windowed to 8, and the
optional ``rpo`` key is mapped when present. No network: ``financials`` is
shadowed on the instance.
"""
import pytest

from RevenueChaser.core.providers.unusual_whales import UWClient


def _client(income=None, balance=None, cashflow=None) -> UWClient:
    c = UWClient(key="test")
    c.financials = lambda _t: {
        "income_statements": income or [],
        "balance_sheets": balance or [],
        "cash_flows": cashflow or [],
    }
    return c


def _inc(fd, rev, cost=None, ebit=None, ni=None, oi=None, sga=None, gp=None):
    row = {"fiscal_date_ending": fd, "total_revenue": str(rev)}
    if cost is not None:
        row["cost_of_revenue"] = str(cost)
    if ebit is not None:
        row["ebit"] = str(ebit)
    if oi is not None:
        row["operating_income"] = str(oi)
    if sga is not None:
        row["selling_general_and_administrative"] = str(sga)
    if gp is not None:
        row["gross_profit"] = str(gp)
    if ni is not None:
        row["net_income"] = str(ni)
    return row


def _bal(fd, **fields):
    return {"fiscal_date_ending": fd, **fields}


def test_missing_fields_map_to_none_not_zero():
    c = _client(
        income=[_inc("2026-03-31", 1200, cost=480, ebit=300)],
        balance=[_bal("2026-03-31", total_assets="10000")],  # no inventory/DR/receivables
    )
    rec = c._map_financials("QXO")[0]
    assert rec["revenue"] == 1200.0
    assert rec["gross_margin"] == 0.6
    assert rec["net_income"] is None            # not 0
    assert rec["cash_from_operations"] is None  # not 0
    assert rec["inventory"] is None             # not 0 (no fake 'lean' credit)
    assert rec["receivables"] is None
    assert rec["deferred_revenue"] is None      # not 0 (no fake deceleration)
    assert rec["rpo"] is None


def test_explicit_zero_is_kept():
    c = _client(
        income=[_inc("2026-03-31", 1000, cost=400)],
        balance=[_bal("2026-03-31", deferred_revenue="0", total_assets="5000")],
    )
    rec = c._map_financials("QXO")[0]
    assert rec["deferred_revenue"] == 0.0
    assert rec["total_assets"] == 5000.0


def test_gross_and_operating_margins_none_when_components_missing():
    # Revenue present but no cost_of_revenue / ebit -> margins None, never 0.
    c = _client(income=[_inc("2026-03-31", 1000)])
    rec = c._map_financials("QXO")[0]
    assert rec["revenue"] == 1000.0
    assert rec["gross_margin"] is None
    assert rec["operating_margin"] is None


def test_records_sorted_newest_first_and_windowed_to_eight():
    income = []
    balance = []
    for i in range(1, 11):  # 10 periods, supplied oldest-first
        fd = f"2024-{i:02d}-28"
        income.append(_inc(fd, 1000 + i))
        balance.append(_bal(fd, total_assets="9000"))
    c = _client(income=income, balance=balance)
    recs = c._map_financials("QXO")
    assert len(recs) == 8
    dates = [r["fiscal_date"] for r in recs]
    assert dates[0] > dates[1] > dates[7]       # newest first
    assert dates[0] == "2024-10-28"             # the newest period is first


def test_cashflow_and_balance_joined_by_fiscal_date():
    income = [_inc("2026-03-31", 1200, cost=480)]
    balance = [_bal("2026-03-31", inventory="2000")]
    cashflow = [{"fiscal_date_ending": "2026-03-31", "operating_cash_flow": "300"}]
    c = _client(income=income, balance=balance, cashflow=cashflow)
    rec = c._map_financials("QXO")[0]
    assert rec["inventory"] == 2000.0
    assert rec["cash_from_operations"] == 300.0


def test_cash_flow_net_income_cross_check_mapped():
    cashflow = [{"fiscal_date_ending": "2026-03-31", "operating_cash_flow": "300",
                 "net_income": "250"}]
    c = _client(income=[_inc("2026-03-31", 1200, ni=250)],
                cashflow=cashflow)
    rec = c._map_financials("QXO")[0]
    assert rec["net_income"] == 250.0        # income statement NI
    assert rec["cash_net_income"] == 250.0   # cash-flow NI for the R7 cross-check


def test_annual_rows_are_dropped_when_report_type_present():
    """Rows with report_type that is not quarterly never reach R3/R7."""
    income = [
        {**_inc("2025-12-31", 5000, cost=2000), "report_type": "annual"},
        {**_inc("2026-03-31", 1200, cost=480), "report_type": "quarterly"},
    ]
    c = _client(income=income)
    recs = c._map_financials("QXO")
    assert len(recs) == 1
    assert recs[0]["fiscal_date"] == "2026-03-31"


def test_rows_without_report_type_are_kept_legacy():
    """No report_type on the payload -> legacy behaviour (keep every row)."""
    income = [_inc("2025-12-31", 5000), _inc("2026-03-31", 1200)]
    c = _client(income=income)
    assert len(c._map_financials("QXO")) == 2


def test_operating_income_preferred_over_ebit():
    """R3 headline uses operating_income; ebit only when OI is null."""
    income = [_inc("2026-03-31", 1200, cost=400, oi=-200, ebit=300)]
    rec = _client(income=income)._map_financials("QXO")[0]
    assert rec["operating_income"] == -200.0
    assert rec["operating_margin"] == pytest.approx(-200 / 1200, abs=1e-3)  # -16.7%


def test_ebit_fallback_when_oi_missing():
    income = [_inc("2026-03-31", 1200, cost=400, ebit=300)]  # no operating_income
    rec = _client(income=income)._map_financials("QXO")[0]
    assert rec["operating_income"] is None
    assert rec["operating_margin"] == pytest.approx(300 / 1200, abs=1e-3)


def test_sga_mapped_when_present():
    income = [_inc("2026-03-31", 1200, sga=400)]
    rec = _client(income=income)._map_financials("QXO")[0]
    assert rec["sga"] == 400.0
    assert rec["sga"] != 0.0  # missing stays None, not 0


def test_gross_profit_direct_or_recovered():
    direct = _client(income=[_inc("2026-03-31", 1000, gp=600)])._map_financials("QXO")[0]
    assert direct["gross_margin"] == pytest.approx(0.6, abs=1e-3)
    recovered = _client(income=[_inc("2026-03-31", 1000, cost=400)])._map_financials("QXO")[0]
    assert recovered["gross_margin"] == pytest.approx(0.6, abs=1e-3)


def test_optional_rpo_is_mapped_when_present():
    c = _client(
        income=[_inc("2026-03-31", 1200)],
        balance=[_bal("2026-03-31", rpo="800")],
    )
    rec = c._map_financials("QXO")[0]
    assert rec["rpo"] == 800.0


def test_no_financials_returns_empty():
    c = _client()
    assert c._map_financials("QXO") == []
