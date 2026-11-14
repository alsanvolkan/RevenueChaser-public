"""#737: a loss-making company with net debt fails the leverage rule instead of staying unknown.

avo's decision of 14 Nov 2026 11:06 Amsterdam, option "A: Kaldı say": when EBITDA is known and at or below zero
**and** debt is above cash and short-term investments, "Net Debt / EBITDA < 3×" fails. There is no ratio to put
beside it -- that is the point -- so the row reads "Net debt" and the label says why. A loss-making company with
net cash keeps passing (review N1), and a company whose EBITDA is not reported at all stays unknown.

Measured at 85 scored names on the real store on 12 Nov 2026 (homesrv, read-only; the breakdown of the 860
unknown leverage rules, bucket "EBIT is zero or negative, 85 with net debt").

The three cases are read off ``net_cash``, so the tests below pin the pipeline invariant they rest on as well:
``_quality_of`` sets ``net_cash`` only when total debt, liquid assets and EBITDA are all known.
"""
from __future__ import annotations

from RevenueChaser.module_fundamentals.pipeline import _quality_metrics
from RevenueChaser.module_fundamentals.screeners import (LOSS_WITH_NET_DEBT_NOTE, evaluate_quality)


def _year(ebit, debt, cash, **over):
    row = {"revenue": 1000.0, "gross_profit": 400.0, "ebit": ebit, "depreciation": 10.0,
           "total_debt": debt, "cash": cash, "total_equity": 500.0, "net_income": -50.0}
    row.update(over)
    return [row]


def _gate(q: dict, **extra) -> dict:
    return evaluate_quality({"net_debt_ebitda": q["net_debt_ebitda"], "net_cash": q.get("net_cash"),
                             "debt_basis": q.get("debt_basis"), **extra})


def _leverage(q: dict, **extra) -> dict:
    return next(r for r in _gate(q, **extra)["rules"] if r["id"] == "net_debt_ebitda")


def test_a_loss_year_with_net_debt_fails_and_says_why():
    q = _quality_metrics(_year(ebit=-80.0, debt=300.0, cash=100.0))
    assert q["net_debt_ebitda"] is None and q["net_cash"] is False
    rule = _leverage(q)
    assert rule["passed"] is False
    assert rule["unit"] == "text" and rule["actual"] == "Net debt"
    assert LOSS_WITH_NET_DEBT_NOTE in rule["label"], rule["label"]
    assert rule["threshold"] == "< 3.0×"


def test_ebitda_exactly_zero_with_net_debt_fails():
    """"known and at or below zero": EBITDA is EBIT + depreciation, so -10 + 10 is the boundary."""
    q = _quality_metrics(_year(ebit=-10.0, debt=300.0, cash=100.0))
    assert q["net_debt_ebitda"] is None and q["net_cash"] is False
    assert _leverage(q)["passed"] is False


def test_a_loss_year_with_net_cash_still_passes():
    q = _quality_metrics(_year(ebit=-80.0, debt=100.0, cash=300.0))
    rule = _leverage(q)
    assert rule["passed"] is True and rule["actual"] == "Net cash"
    assert LOSS_WITH_NET_DEBT_NOTE not in rule["label"]


def test_no_ebitda_figure_at_all_stays_unknown():
    """A filer that tags no EBIT has no EBITDA: a missing input, not a loss year."""
    q = _quality_metrics(_year(ebit=None, debt=300.0, cash=100.0))
    assert q["net_cash"] is None and _leverage(q)["passed"] is None


def test_a_missing_debt_or_cash_figure_stays_unknown():
    unknown_debt = _year(ebit=-80.0, debt=None, cash=100.0, lt_debt_current=40.0)
    assert _leverage(_quality_metrics(unknown_debt))["passed"] is None
    assert _leverage(_quality_metrics(_year(ebit=-80.0, debt=300.0, cash=None)))["passed"] is None


def test_a_positive_ebitda_is_untouched():
    q = _quality_metrics(_year(ebit=190.0, debt=1000.0, cash=100.0))    # EBITDA 200, net debt 900 -> 4.5×
    rule = _leverage(q)
    assert rule["unit"] == "ratio" and rule["passed"] is False and rule["actual"] == 4.5
    assert LOSS_WITH_NET_DEBT_NOTE not in rule["label"]


def test_the_inferred_debt_note_is_unreachable_in_this_case():
    """#737's other note says the zero was inferred. An inferred zero is a zero total, so the company has no net
    debt and takes the net-cash branch; the two notes can never meet on one label."""
    q = _quality_metrics(_year(ebit=-80.0, debt=None, cash=100.0))
    assert q["debt_basis"] == "inferred_none" and q["net_cash"] is True
    rule = _leverage(q)
    assert rule["passed"] is True and LOSS_WITH_NET_DEBT_NOTE not in rule["label"]


def test_quality_passes_exactly_the_same_names():
    """The gate needs every rule known and passed (``screeners._counted``), so a rule that moves from unknown to
    False cannot take a passing name down: it was not passing before. Nothing else in the gate moves."""
    metrics = {"rev_growth_ttm": 0.30, "roic": 0.20, "gross_margin": 0.35,
               "net_debt_ebitda": None, "net_cash": False, "debt_basis": "filed"}
    gate = evaluate_quality(metrics)
    assert gate["passed"] is False                      # as before: it could not pass on an unknown either
    assert gate["known"] == 3 and gate["score"] == "2/3"  # what changes is only that the rule is now counted
