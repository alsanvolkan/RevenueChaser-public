"""Review SF-4 (G6, build plan PR 14): growth, margins and survival read the trailing year from the quarters.

The card read one fiscal year while momentum is today's, and the stored quarters went unread. Now, when 10-Qs
follow the anchor year, the card reads three trailing years (``core.trailing_year``: the year plus the newest year to
date minus the same a year earlier), each a year apart: growth is the newest trailing year over the one before,
acceleration that growth over the growth a year earlier, and the margins, free cash flow, net debt, runway and
dilution read the newest one (its quarter's balance sheet and share count). One basis for the whole card: without
all three trailing years the card stays on the fiscal years. The provenance line says which.
Default chosen (plan): the trailing year whenever the quarters give it, change if avo prefers the fiscal year.
"""
from __future__ import annotations

import copy

import pytest

from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_asymmetric_growth import engine, freeze, report
from RevenueChaser.module_asymmetric_growth.engine import score_ticker
from RevenueChaser.module_asymmetric_growth.tests.test_ag_payload_route_380 import _card, _result

FLOWS = ("revenue", "gross_profit", "operating_cash_flow", "capex", "rd_expense")
BALANCE = {"total_debt": 60.0, "cash": 400.0, "total_assets": 1200.0}
TECH = {"price": 100.0, "dist_52w_pct": 0.05, "vol_ratio": 1.6, "above_sma50": True}


def _year(fy, revenue, gp, ocf, capex, rd, shares, balance):
    vals = dict(zip(FLOWS, (revenue, gp, ocf, capex, rd)))
    rec = {"fiscal_year": str(fy), **vals, **balance, "diluted_shares": shares}
    rec["period_starts"] = {f: f"{fy}-01-01" for f in FLOWS}
    rec["period_ends"] = {f: f"{fy}-12-31" for f in (*FLOWS, *balance, "diluted_shares")}
    return rec


def _quarter(year, ytd, shares, balance):
    return {"period": f"{year}-06-30", **balance, "diluted_shares": shares,
            "ytd": {f: {"start": f"{year}-01-01", "months": 6, "val": v} for f, v in zip(FLOWS, ytd)}}


ANNUAL = [
    _year(2023, 800.0, 400.0, 120.0, 20.0, 100.0, 100.0, {"total_debt": 50.0, "cash": 200.0, "total_assets": 1000.0}),
    _year(2024, 850.0, 500.0, 150.0, 25.0, 130.0, 101.0, {"total_debt": 50.0, "cash": 250.0, "total_assets": 1050.0}),
    _year(2025, 1080.0, 700.0, 250.0, 40.0, 200.0, 102.0, BALANCE),
]
QUARTERLY = [
    _quarter(2023, (380.0, 180.0, 50.0, 8.0, 45.0), 99.0, {"total_debt": 50.0, "cash": 190.0, "total_assets": 990.0}),
    _quarter(2024, (400.0, 200.0, 60.0, 10.0, 60.0), 100.0, {"total_debt": 50.0, "cash": 220.0, "total_assets": 1020.0}),
    _quarter(2025, (450.0, 260.0, 100.0, 18.0, 90.0), 100.0, {"total_debt": 55.0, "cash": 300.0, "total_assets": 1100.0}),
    # A third quarter of 2025 (nine months): the trailing year a year before June 2026 is still June 2025's.
    {**_quarter(2025, (760.0, 450.0, 170.0, 28.0, 150.0), 101.0,
                {"total_debt": 58.0, "cash": 320.0, "total_assets": 1150.0}), "period": "2025-09-30",
     "ytd": {f: {"start": "2025-01-01", "months": 9, "val": v}
             for f, v in zip(FLOWS, (760.0, 450.0, 170.0, 28.0, 150.0))}},
    _quarter(2026, (700.0, 420.0, 200.0, 30.0, 140.0), 110.0, {"total_debt": 70.0, "cash": 500.0, "total_assets": 1400.0}),
]


def _fin(annual=ANNUAL, quarterly=QUARTERLY):
    return {"info": {"country": "United States", "exchange": "NMS", "longName": "Test Corp"},
            "annual": copy.deepcopy(annual), "quarterly": copy.deepcopy(quarterly), "market_cap": 5e9,
            "source": "sec_edgar"}


def test_the_card_reads_three_trailing_years():
    card = score_ticker("TEST", _fin(), TECH, spine={})
    assert card["basis"] == "TTM" and card["period_ends"]["basis"] == "TTM"
    # Trailing revenue: 1080 + 700 - 450 = 1330, 850 + 450 - 400 = 900, 800 + 400 - 380 = 820.
    assert card["rev_growth"] == pytest.approx(1330 / 900 - 1)
    accel = next(s for s in card["signals"][0]["subs"] if s["param"] == "accelerating")
    assert accel["value"][0] == pytest.approx(1330 / 900 - 1) and accel["value"][1] == pytest.approx(900 / 820 - 1)
    assert card["gross_margin"] == pytest.approx((700 + 420 - 260) / 1330)
    assert card["fcf"] == pytest.approx((250 + 200 - 100) - (40 + 30 - 18))
    assert card["rd_intensity"] == pytest.approx((200 + 140 - 90) / 1330)
    assert card["dilution_yoy"] == pytest.approx(110 / 100 - 1)              # the June quarters' diluted shares
    nda = next(s for s in card["signals"][1]["subs"] if s["param"] == "net_debt_assets")
    assert nda["value"] == pytest.approx((70 - 500) / 1400)                   # June 2026's balance sheet
    assert card["period_ends"]["revenue"] == ["2026-06-30", "2025-06-30", "2024-06-30"]
    assert card["period_ends"]["balance_sheet"] == "2026-06-30" and card["period_ends"]["cash_flow"] == "2026-06-30"
    assert card["anchor_fy"] == 2025                                        # the year the trailing year starts from


@pytest.mark.parametrize("drop", ["2023", "2026"])
def test_without_three_trailing_years_the_card_reads_the_fiscal_years(drop):
    """No 10-Q three years back (no acceleration), or none after the year: the fiscal years, never a mix."""
    card = score_ticker("TEST", _fin(quarterly=[q for q in QUARTERLY if not q["period"].startswith(drop)]),
                        TECH, spine={})
    assert card["basis"] == "FY"
    assert card["rev_growth"] == pytest.approx(1080 / 850 - 1)
    assert card["period_ends"]["revenue"] == ["2025-12-31", "2024-12-31", "2023-12-31"]


def test_a_card_with_no_quarters_reads_the_fiscal_years():
    fin = _fin()
    del fin["quarterly"]
    assert score_ticker("TEST", fin, TECH, spine={})["basis"] == "FY"


def test_the_provenance_line_names_the_trailing_years(tmp_path, rawstore_tmp_db):
    ends = {"basis": "TTM", "revenue": ["2026-06-30", "2025-06-30", "2024-06-30"], "balance_sheet": "2026-06-30",
            "cash_flow": "2026-06-30"}
    result = _result([_card("AAA", anchor_fy=2025, prior_fy=2024, period_ends=ends,
                            model_version=freeze.model_version())])
    grid = run_board_js(report.render_asymmetric_growth_hunter_html(result),
                        "setTier('all');\nconsole.log(JSON.stringify({grid: _els['grid'].innerHTML}));", tmp_path,
                        payload=report.board_payload(result))["grid"]
    assert "TTM to 2026-06-30 vs TTM to 2025-06-30" in grid and "FY2025 vs" not in grid


def test_the_batch_read_carries_the_quarters(monkeypatch):
    monkeypatch.setattr(engine.sec_edgar, "stored_fundamentals_many",
                        lambda tickers: {"TEST": (ANNUAL, QUARTERLY, {"is_us": True})})
    monkeypatch.setattr(engine.cache_manager, "load_many", lambda *a, **k: {})
    monkeypatch.setattr(engine, "themes_for_many", lambda tickers: {})
    monkeypatch.setattr(engine, "_bars_by_ticker", lambda tickers, session: {})
    preload = engine._preload(["TEST"], no_fetch=True, session=None)
    fin = engine._cached_financials("TEST", no_fetch=True, preload=preload)
    assert fin["quarterly"] == QUARTERLY and fin["annual"] == ANNUAL


def test_the_selection_version_moves_with_the_basis():
    assert engine.SELECTION_VERSION == "8"                  # 7 was this change; 8 adds G3's prices-store momentum
