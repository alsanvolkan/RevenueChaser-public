"""FinancialModelEngine engine birim testleri (synthetic data, ağ yok)."""
from __future__ import annotations

from RevenueChaser.core.providers.validator import validate_annual
from RevenueChaser.module_financials.engines.comps_engine import run_comps
from RevenueChaser.module_financials.engines.credit_capacity_engine import run_credit_capacity
from RevenueChaser.module_financials.engines.dcf_engine import run_dcf
from RevenueChaser.module_financials.engines.sensitivity_engine import run_sensitivity
from RevenueChaser.module_financials.engines.three_statement_engine import run_three_statement


def test_dcf_projection_and_valuation(financials):
    res = run_dcf(financials)
    assert "error" not in res
    assert len(res["projection"]) == 5
    assert res["wacc"]["wacc"] > 0
    assert res["wacc"]["cost_of_equity"] > res["wacc"]["wacc"]
    assert res["valuation"]["per_share_gordon"] is not None
    assert res["valuation"]["per_share_exit"] is not None
    assert res["terminal_value"]["gordon"] > 0
    assert res["terminal_value"]["exit_multiple"] > 0


def test_dcf_ebitda_fallback_derivation(financials):
    broken = dict(financials)
    broken["annual"] = [{**r, "ebitda": None} for r in financials["annual"]]
    res = run_dcf(broken)
    assert "error" not in res
    assert res["base_year"]["ebitda_margin"] > 0


def test_three_statement_balances(financials):
    res = run_three_statement(financials)
    assert "error" not in res
    assert res["balance_check"]["balanced"] is True
    assert res["balance_check"]["max_residual"] < 0.01
    assert len(res["projection"]) == 5
    # net kar IS->CF bağlantısı: CFO ilk yılda hesaplandı
    assert res["projection"][0]["cfo"] is not None


def test_comps_implied_valuation(financials):
    def fake_fetch(ticker):
        data = {
            "MSFT": {"enterpriseValue": 1000, "ebitda": 100, "totalRevenue": 200, "trailingPE": 30},
            "GOOG": {"enterpriseValue": 800, "ebitda": 80, "totalRevenue": 160, "trailingPE": 25},
        }
        return data.get(ticker, {})

    res = run_comps(financials, ["MSFT", "GOOG"], fetch_info=fake_fetch)
    assert "error" not in res
    assert res["multiples"]["ev_ebitda"]["p50"] == 10.0
    assert res["implied"]["per_share_ev_ebitda"] is not None
    # target EBITDA=300, medyan EV/EBITDA=10 -> EV=3000, net debt=260-120=140 -> eq=2860 -> /1000 = 2.86
    assert abs(res["implied"]["per_share_ev_ebitda"] - 2.86) < 1e-6


def test_credit_capacity_grades(financials):
    res = run_credit_capacity(financials)
    assert "error" not in res
    assert res["metrics"]["gross_debt_to_ebitda"] == 260.0 / 300.0
    assert res["grades"]["gross_leverage"] == "healthy"
    assert res["metrics"]["interest_coverage"] is not None


def test_sensitivity_matrix(financials):
    res = run_sensitivity(financials)
    assert "error" not in res
    assert len(res["wacc_axis"]) == 5
    assert len(res["matrix"]) == 3
    assert len(res["matrix"][0]["values"]) == 5
    assert res["base_per_share"] == res["matrix"][1]["values"][2]


def test_validator_required_fields(financials):
    annual, warnings = validate_annual(financials["annual"])
    assert len(annual) == 3
    assert not any("missing" in w for w in warnings)


def test_dcf_does_not_echo_an_unused_sector_band(financials):
    """DF42: the sector band never set growth or margin, so the result no longer carries it."""
    assert "sector_defaults" not in run_dcf(financials)
