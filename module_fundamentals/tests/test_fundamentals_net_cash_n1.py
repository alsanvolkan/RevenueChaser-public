"""Review N1 (28 Oct): a company with more cash than debt passes "Net Debt / EBITDA < 3×" even when its EBITDA is
zero or negative. Before, the ratio was left unknown whenever EBITDA was not positive, so a net-cash firm with a
loss year could never clear Quality 3/3; a firm that owes nothing net is not over-levered, whatever it earns.
A firm with net debt and EBITDA ≤ 0 stays unknown: its leverage really cannot be measured by this ratio."""
from __future__ import annotations

from RevenueChaser.module_fundamentals.pipeline import _quality_metrics
from RevenueChaser.module_fundamentals.screeners import evaluate_quality


def _year(ebit, debt, cash):
    return [{"revenue": 1000.0, "gross_profit": 400.0, "ebit": ebit, "depreciation": 10.0,
             "total_debt": debt, "cash": cash, "total_equity": 500.0, "net_income": -50.0}]


def _rule(q: dict) -> dict:
    gate = evaluate_quality({"net_debt_ebitda": q["net_debt_ebitda"], "net_cash": q.get("net_cash")})
    return next(r for r in gate["rules"] if r["id"] == "net_debt_ebitda")


def test_net_cash_with_a_loss_passes_the_leverage_rule():
    q = _quality_metrics(_year(ebit=-80.0, debt=100.0, cash=300.0))
    assert q["net_debt_ebitda"] is None and q["net_cash"] is True
    rule = _rule(q)
    assert rule["passed"] is True and rule["actual"] == "Net cash" and rule["unit"] == "text"


def test_net_debt_with_a_loss_stays_unknown():
    q = _quality_metrics(_year(ebit=-80.0, debt=300.0, cash=100.0))
    assert q["net_cash"] is False and _rule(q)["passed"] is None


def test_a_positive_ebitda_keeps_the_ratio():
    q = _quality_metrics(_year(ebit=190.0, debt=100.0, cash=300.0))      # EBITDA 200, net debt -200
    assert q["net_debt_ebitda"] == -1.0 and q["net_cash"] is True
    rule = _rule(q)
    assert rule["passed"] is True and rule["actual"] == -1.0 and rule["unit"] == "ratio"


def test_missing_debt_or_cash_is_not_net_cash():
    q = _quality_metrics(_year(ebit=-80.0, debt=None, cash=300.0))
    assert q["net_cash"] is None and _rule(q)["passed"] is None


def test_net_cash_with_no_ebitda_figure_stays_unknown():
    """Opus review S5: the rule passes on net cash only when EBITDA is known and ≤ 0, as the finding and the PR say.
    A firm whose EBIT is not reported has no EBITDA at all; that is a missing input, not a loss year."""
    q = _quality_metrics(_year(ebit=None, debt=100.0, cash=300.0))
    assert q["net_cash"] is None and _rule(q)["passed"] is None
