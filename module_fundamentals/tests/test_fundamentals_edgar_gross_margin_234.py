"""#234: the Fundamentals Quality rule "Gross margin ≥ 30%" reads EDGAR revenue − cost of revenue.

Fixture filer: tags ``Revenues`` and ``CostOfRevenue`` but never ``GrossProfit``.
Synthetic companyfacts, no network.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_fundamentals import screeners
from RevenueChaser.module_fundamentals.pipeline import _quality_metrics


def _facts(cost_concept: str = "CostOfRevenue", cost: float = 400.0) -> dict:
    def fy(val):
        return {"units": {"USD": [{"start": "2023-01-01", "end": "2023-12-31", "val": val,
                                   "form": "10-K", "fp": "FY", "filed": "2024-02-01"}]}}

    return {"entityName": "No GrossProfit Co", "facts": {"us-gaap": {
        "Revenues": fy(1000.0), cost_concept: fy(cost),
        "OperatingIncomeLoss": fy(200.0), "LongTermDebtNoncurrent": fy(300.0),
        "CashAndCashEquivalentsAtCarryingValue": fy(100.0),
    }}}


def _gross_margin_rule(facts: dict) -> tuple[dict, dict]:
    metrics = _quality_metrics(sec_edgar.extract_annual(facts))
    gate = screeners.evaluate_quality({"rev_growth_ttm": 0.25, **metrics})
    return metrics, next(r for r in gate["rules"] if r["id"] == "gross_margin")


def test_derived_gross_margin_passes_the_30_percent_rule():
    metrics, rule = _gross_margin_rule(_facts())  # (1000 - 400) / 1000 = 60%
    assert metrics["gross_margin"] == pytest.approx(0.60)
    assert rule["passed"] is True
    assert rule["actual"] == pytest.approx(0.60)


def test_derived_gross_margin_below_30_percent_does_not_pass():
    metrics, rule = _gross_margin_rule(_facts(cost=800.0))  # 20%
    assert metrics["gross_margin"] == pytest.approx(0.20)
    assert rule["passed"] is False


def test_a_filer_tagging_only_an_excluded_cost_concept_stays_unknown():
    metrics, rule = _gross_margin_rule(_facts(cost_concept="OperatingCostsAndExpenses"))
    assert metrics["gross_margin"] is None
    assert rule["passed"] is None   # unknown, not a fail (#151)
