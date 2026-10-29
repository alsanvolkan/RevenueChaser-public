"""Quality reads the trailing year from the quarters (G6, build plan PR 13: "Quality reads the quarter store").

ROIC, gross margin and net debt / EBITDA read one fiscal year (the anchor, review F1) until this step. Now, when a
10-Q follows the anchor year, they read the trailing twelve months: each flow is the year plus the newest year to
date minus the same year to date a year earlier (the Rule of 40's TTM, ``pipeline._ttm_point``), and the balance
sheet is the newest quarter's. One basis for all three: when the quarters cannot give every figure the year gives,
Quality stays on the year. The card says which ("TTM to <date>" or "FY to <date>").
Default chosen (plan, avo's call): TTM whenever the quarters give it, else the fiscal year.
"""
from __future__ import annotations

import copy

import pytest

from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_fundamentals import config, pipeline
from RevenueChaser.module_fundamentals.pipeline import build_payload
from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
from RevenueChaser.module_fundamentals.tests.test_fundamental import _m

FLOWS = ("revenue", "gross_profit", "ebit", "depreciation")


def _year(fy: int, vals: dict, balance: dict) -> dict:
    start, end = f"{fy}-01-01", f"{fy}-12-31"
    rec = {"fiscal_year": str(fy), **vals, **balance}
    rec["period_starts"] = {f: start for f in vals}
    rec["period_ends"] = {f: end for f in (*vals, *balance)}
    return rec


def _quarter(end: str, ytd: dict, balance: dict, start: str, months: int = 6) -> dict:
    return {"period": end, **balance,
            "ytd": {f: {"start": start, "months": months, "val": v} for f, v in ytd.items()}}


ANNUAL = [
    _year(2024, {"revenue": 900.0, "gross_profit": 350.0, "ebit": 180.0, "depreciation": 45.0},
          {"total_debt": 520.0, "cash": 90.0, "total_equity": 760.0}),
    _year(2025, {"revenue": 1000.0, "gross_profit": 400.0, "ebit": 200.0, "depreciation": 50.0},
          {"total_debt": 500.0, "cash": 100.0, "total_equity": 800.0}),
]
QUARTERLY = [
    _quarter("2025-06-30", {"revenue": 450.0, "gross_profit": 170.0, "ebit": 80.0, "depreciation": 24.0},
             {"total_debt": 510.0, "cash": 95.0, "total_equity": 780.0}, "2025-01-01"),
    _quarter("2026-06-30", {"revenue": 550.0, "gross_profit": 230.0, "ebit": 120.0, "depreciation": 26.0},
             {"total_debt": 300.0, "cash": 150.0, "total_equity": 900.0}, "2026-01-01"),
]


def test_quality_reads_the_trailing_year_when_a_10q_follows_the_year():
    q = pipeline._quality_metrics(ANNUAL, QUARTERLY)
    assert (q["quality_basis"], q["quality_period_end"]) == ("TTM", "2026-06-30")
    # Revenue 1000 + 550 - 450 = 1100, gross profit 460, EBIT 240, depreciation 52; the June 2026 balance sheet.
    assert q["gross_margin"] == pytest.approx(460 / 1100)
    assert q["net_debt_ebitda"] == pytest.approx((300 - 150) / (240 + 52))
    assert q["roic"] == pytest.approx(240 * (1 - config.TAX_RATE) / (300 + 900 - 150))
    assert q["total_equity"] == 900.0


def test_without_a_later_quarter_quality_reads_the_year():
    for quarterly in ([], QUARTERLY[:1]):
        q = pipeline._quality_metrics(ANNUAL, quarterly)
        assert (q["quality_basis"], q["quality_period_end"]) == ("FY", "2025-12-31")
        assert q["gross_margin"] == pytest.approx(0.4)
        assert q["net_debt_ebitda"] == pytest.approx((500 - 100) / 250)
        assert q["roic"] == pytest.approx(200 * (1 - config.TAX_RATE) / (500 + 800 - 100))


@pytest.mark.parametrize("hole", ["ebit_prior", "depreciation_current", "debt_balance", "months"])
def test_one_basis_for_all_three(hole):
    """A quarter that cannot give every figure the year gives leaves Quality on the year, never a mix."""
    quarterly = copy.deepcopy(QUARTERLY)
    if hole == "ebit_prior":
        del quarterly[0]["ytd"]["ebit"]
    elif hole == "depreciation_current":
        del quarterly[1]["ytd"]["depreciation"]
    elif hole == "debt_balance":
        del quarterly[1]["total_debt"]
    else:
        quarterly[0]["ytd"]["revenue"]["months"] = 3
    q = pipeline._quality_metrics(ANNUAL, quarterly)
    assert q["quality_basis"] == "FY" and q["net_debt_ebitda"] == pytest.approx(1.6)


def test_a_derived_gross_profit_is_revenue_minus_cost():
    annual = copy.deepcopy(ANNUAL)
    quarterly = copy.deepcopy(QUARTERLY)
    for rec, cor in zip(annual, (550.0, 600.0)):
        rec["cost_of_revenue"] = cor
        rec["period_starts"]["cost_of_revenue"] = rec["period_starts"]["revenue"]
        rec["period_ends"]["cost_of_revenue"] = rec["period_ends"]["revenue"]
    for rec, cor in zip(quarterly, (280.0, 320.0)):
        del rec["ytd"]["gross_profit"]
        rec["ytd"]["cost_of_revenue"] = {**rec["ytd"]["revenue"], "val": cor}
    q = pipeline._quality_metrics(annual, quarterly)
    # TTM revenue 1100, cost of revenue 600 + 320 - 280 = 640.
    assert q["quality_basis"] == "TTM" and q["gross_margin"] == pytest.approx((1100 - 640) / 1100)


def _payload(basis, end):
    row = {"ticker": "AAA", "name": "Test", "price": 50.0, "market_cap": 1e9, "sector": "Technology",
           "industry": "Software", "themes": [], "earnings_date": None, "period": "Q2/2026 vs Q2/2025",
           "anchor_period_end": "2025-12-31", "quality_basis": basis, "quality_period_end": end,
           "metrics": _m(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=50.0, gross_margin=0.35, roic=0.20,
                         net_debt_ebitda=2.0, dist_52w_pct=0.90, rs_6m=0.05, above_200d=True, pe=15.0, pb=2.0)}
    return build_payload([row], top_n=10)


def _grid(payload, tmp_path):
    return run_board_js(render_fundamental_dashboard(payload), """
render();
console.log(JSON.stringify({grid: _els['grid'].innerHTML}));
""", tmp_path)["grid"]


def test_the_card_names_the_quality_basis(tmp_path):
    assert "TTM to 2026-06-30" in _grid(_payload("TTM", "2026-06-30"), tmp_path)
    grid = _grid(_payload("FY", "2025-12-31"), tmp_path)
    assert "FY to 2025-12-31" in grid and "TTM to" not in grid


def test_the_selection_version_moves_with_the_basis():
    assert config.SELECTION_VERSION == "8"


def test_the_row_carries_the_quality_basis(monkeypatch):
    from datetime import date

    from RevenueChaser.module_fundamentals.tests.test_fundamentals_w3_386 import _compute
    monkeypatch.setattr(pipeline, "_today", lambda: date(2026, 10, 27))
    row, drops = _compute(monkeypatch, ANNUAL, edgar=lambda t, no_fetch=False: (ANNUAL, QUARTERLY, {}))
    assert drops == [] and (row["quality_basis"], row["quality_period_end"]) == ("TTM", "2026-06-30")
    assert row["metrics"]["gross_margin"] == pytest.approx(460 / 1100)
    assert build_payload([row], top_n=10)["candidates"][0]["quality_basis"] == "TTM"
