"""#187: Fundamentals net debt / EBITDA counts short-term investments next to cash; ROIC is pinned.

Synthetic companyfacts, no network.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_fundamentals import config, screeners
from RevenueChaser.module_fundamentals.pipeline import _quality_metrics

_CASH = "CashAndCashEquivalentsAtCarryingValue"


def _facts(*, sti: float | None = None, cash: float = 100.0, debt: float = 300.0) -> dict:
    def fy(val):
        return {"units": {"USD": [{"start": "2023-01-01", "end": "2023-12-31", "val": val,
                                   "form": "10-K", "fp": "FY", "filed": "2024-02-01"}]}}

    def inst(val):
        return {"units": {"USD": [{"end": "2023-12-31", "val": val, "form": "10-K", "fp": "FY",
                                   "filed": "2024-02-01"}]}}

    gaap = {"Revenues": fy(1000.0), "OperatingIncomeLoss": fy(200.0),
            "LongTermDebtNoncurrent": inst(debt), "StockholdersEquity": inst(600.0),
            _CASH: inst(cash)}
    if sti is not None:
        gaap["ShortTermInvestments"] = inst(sti)
    return {"entityName": "Liquidity Co", "facts": {"us-gaap": gaap}}


def _metrics(facts: dict) -> dict:
    return _quality_metrics(sec_edgar.extract_annual(facts))


def test_net_debt_ebitda_counts_the_short_term_investments():
    m = _metrics(_facts(sti=400.0))  # (300 - (100 + 400)) / 200
    assert m["net_debt_ebitda"] == pytest.approx(-1.0)


def test_cash_alone_gives_the_old_figure():
    m = _metrics(_facts())  # (300 - 100) / 200
    assert m["net_debt_ebitda"] == pytest.approx(1.0)


def test_the_quality_rule_reads_the_liquidity_figure():
    def rule(facts):
        gate = screeners.evaluate_quality({"rev_growth_ttm": 0.25, **_metrics(facts)})
        return next(r for r in gate["rules"] if r["id"] == "net_debt_ebitda")

    cash_only = rule(_facts(debt=1000.0))  # (1,000 - 100) / 200 = 4.5x: fails "< 3x"
    assert cash_only["actual"] == pytest.approx(4.5)
    assert cash_only["passed"] is False
    with_sti = rule(_facts(debt=1000.0, sti=600.0))  # (1,000 - 700) / 200 = 1.5x
    assert with_sti["actual"] == pytest.approx(1.5)
    assert with_sti["passed"] is True


def test_roic_is_unchanged_by_the_investments():
    without = _metrics(_facts())
    with_sti = _metrics(_facts(sti=400.0))
    expected = 200.0 * (1 - config.TAX_RATE) / (300.0 + 600.0 - 100.0)  # invested capital uses cash only
    assert with_sti["roic"] == pytest.approx(expected)
    assert with_sti["roic"] == pytest.approx(without["roic"])


def test_a_missing_sti_changes_nothing():
    m = _metrics(_facts(cash=150.0))
    assert m["net_debt_ebitda"] == pytest.approx((300.0 - 150.0) / 200.0)


def test_investments_without_cash_give_no_net_debt():
    facts = _facts(sti=400.0)
    del facts["facts"]["us-gaap"][_CASH]
    assert _metrics(facts)["net_debt_ebitda"] is None
