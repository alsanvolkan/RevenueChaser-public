"""Financials missing-data rule (#173, #255): missing is unknown, never 0; a zero the user typed is a zero."""
from __future__ import annotations

import pytest

from RevenueChaser.module_financials.engines import dcf_engine
from RevenueChaser.module_financials.engines.comps_engine import run_comps
from RevenueChaser.module_financials.engines.credit_capacity_engine import run_credit_capacity
from RevenueChaser.module_financials.engines.dcf_engine import run_dcf
from RevenueChaser.module_financials.engines.sensitivity_engine import run_sensitivity
from RevenueChaser.module_financials.engines.three_statement_engine import run_three_statement


def _with_latest(financials, **over):
    out = dict(financials)
    out["annual"] = [dict(r) for r in financials["annual"]]
    out["annual"][-1].update(over)
    return out


# ── M7 / #255: a loss-making company is not "healthy" ──────────────────────
@pytest.mark.parametrize("ebitda", [-50.0, 0.0])
def test_non_positive_ebitda_with_debt_is_not_meaningful_and_stressed(financials, ebitda):
    res = run_credit_capacity(_with_latest(financials, ebitda=ebitda))
    m, g = res["metrics"], res["grades"]
    assert m["gross_debt_to_ebitda"] is None and m["net_debt_to_ebitda"] is None
    assert m["gross_debt_to_ebitda_state"] == "n/m" and m["net_debt_to_ebitda_state"] == "n/m"
    assert g["gross_leverage"] == "stressed" and g["net_leverage"] == "stressed"


def test_non_positive_ebitda_makes_coverage_not_meaningful(financials):
    res = run_credit_capacity(_with_latest(financials, ebitda=-50.0, interest_expense=16.0))
    assert res["metrics"]["interest_coverage"] is None
    assert res["metrics"]["interest_coverage_state"] == "n/m"
    assert res["grades"]["interest_coverage"] == "stressed"


def test_missing_ebitda_stays_unknown(financials):
    # validate_annual derives EBITDA from EBIT + D&A, so all three must be gone
    res = run_credit_capacity(_with_latest(financials, ebitda=None, ebit=None, depreciation=None))
    assert res["metrics"]["gross_debt_to_ebitda_state"] == "unknown"
    assert res["grades"]["gross_leverage"] == "unknown"


def test_negative_ebitda_without_debt_is_not_stressed(financials):
    res = run_credit_capacity(_with_latest(financials, ebitda=-50.0, total_debt=0.0))
    assert res["grades"]["gross_leverage"] == "healthy"
    assert res["metrics"]["gross_debt_to_ebitda"] == 0.0


def test_debt_free_cash_rich_loss_maker_is_not_stressed_on_net_leverage(financials):
    res = run_credit_capacity(_with_latest(financials, ebitda=-50.0, total_debt=0.0, cash=600.0))
    assert res["grades"]["gross_leverage"] == "healthy"
    assert res["grades"]["net_leverage"] == "healthy"
    assert res["metrics"]["net_debt_to_ebitda"] is None
    assert res["metrics"]["net_debt_to_ebitda_state"] == "n/m"


def test_loss_maker_with_debt_and_more_cash_is_still_stressed(financials):
    res = run_credit_capacity(_with_latest(financials, ebitda=-50.0, total_debt=100.0, cash=600.0))
    assert res["grades"]["gross_leverage"] == "stressed" and res["grades"]["net_leverage"] == "stressed"


def test_positive_ebitda_is_unchanged(financials):
    res = run_credit_capacity(financials)
    assert res["metrics"]["gross_debt_to_ebitda_state"] == "ok"
    assert res["metrics"]["gross_debt_to_ebitda"] == pytest.approx(260.0 / 300.0)


def test_unknown_debt_is_unknown_not_a_grade(financials):
    res = run_credit_capacity(_with_latest(financials, total_debt=None))
    assert res["grades"]["gross_leverage"] == "unknown"


# ── M15: missing debt or cash is not 0, and one fiscal year ────────────────
@pytest.mark.parametrize("field", ["total_debt", "cash"])
def test_dcf_without_debt_or_cash_has_no_equity_value(financials, field):
    res = run_dcf(_with_latest(financials, **{field: None}))
    v = res["valuation"]
    assert v["net_debt"] is None
    assert v["equity_gordon"] is None and v["per_share_gordon"] is None and v["per_share_exit"] is None
    assert v["ev_gordon"] is not None
    assert any("net debt" in w.lower() for w in res["warnings"])


def test_dcf_does_not_borrow_debt_from_an_older_year(financials):
    # 2023 has no cash; 2022's 110 must not be used with 2023's debt
    res = run_dcf(_with_latest(financials, cash=None))
    assert res["valuation"]["net_debt"] is None


def test_dcf_net_debt_uses_the_latest_year(financials):
    assert run_dcf(financials)["valuation"]["net_debt"] == 140.0


def test_a_reported_zero_cash_is_a_zero(financials):
    res = run_dcf(_with_latest(financials, cash=0.0))
    assert res["valuation"]["net_debt"] == 260.0


def test_comps_without_debt_or_cash_has_no_per_share(financials):
    def fetch(t):
        return {"enterpriseValue": 1000, "ebitda": 100, "totalRevenue": 200, "trailingPE": 30}

    res = run_comps(_with_latest(financials, cash=None), ["MSFT"], fetch_info=fetch)
    assert res["implied"]["net_debt"] is None
    assert res["implied"]["per_share_ev_ebitda"] is None
    assert res["implied"]["per_share_ev_revenue"] is None
    assert res["implied"]["per_share_pe"] is not None        # P/E goes to equity, no net debt needed


def test_wacc_weights_use_the_latest_year_only(financials):
    full = run_dcf(financials)["wacc"]["debt_weight"]
    no_debt_latest = run_dcf(_with_latest(financials, total_debt=None))["wacc"]
    assert full == pytest.approx(260.0 / (100_000.0 + 260.0))
    assert no_debt_latest["debt_weight"] == pytest.approx(0.15)       # the documented fallback, not 2022's 280


# ── M14: a zero the user gave is used ──────────────────────────────────────
@pytest.mark.parametrize("key,path", [
    ("tax_rate", lambda r: r["assumptions"]["tax_rate"]),
    ("terminal_growth", lambda r: r["assumptions"]["terminal_growth"]),
    ("ebitda_margin", lambda r: r["assumptions"]["ebitda_margin"]),
    ("exit_multiple", lambda r: r["assumptions"]["exit_multiple"]),
])
def test_zero_assumptions_are_not_replaced_by_defaults(financials, key, path):
    assert path(run_dcf(financials, {key: 0.0})) == 0.0


def test_zero_risk_free_and_cost_of_debt_are_used(financials):
    base = run_dcf(financials)["wacc"]
    z = run_dcf(financials, {"risk_free": 0.0, "cost_of_debt": 0.0})["wacc"]
    assert z["cost_of_equity"] == pytest.approx(base["beta"] * 0.05)
    assert z["cost_of_debt_after_tax"] == 0.0


def test_zero_growth_row_of_the_sensitivity_runs_at_zero(financials):
    res = run_sensitivity(financials, {"terminal_growth": 0.0})
    assert res["base_terminal_growth"] == 0.0
    assert res["growth_axis"][1] == 0.0


def test_projection_years_beyond_five_does_not_crash(financials):
    res = run_dcf(financials, {"projection_years": 8})
    assert "error" not in res and len(res["projection"]) == 8
    assert any("revenue growth" in w.lower() for w in res["warnings"])


def test_projection_years_of_zero_is_an_error_not_a_crash(financials):
    assert "error" in run_dcf(financials, {"projection_years": 0})


# ── I5: no silent None when g >= WACC ──────────────────────────────────────
def test_gordon_value_says_why_it_is_missing(financials):
    res = run_dcf(financials, {"terminal_growth": 0.5})
    assert res["terminal_value"]["gordon"] is None
    assert res["terminal_value"]["gordon_note"]
    assert any("terminal growth" in w.lower() for w in res["warnings"])


def test_gordon_value_has_no_note_when_it_exists(financials):
    res = run_dcf(financials)
    assert res["terminal_value"]["gordon_note"] is None


# ── M19: three statement ───────────────────────────────────────────────────
def test_user_tax_rate_beats_history(financials):
    res = run_three_statement(_with_latest(financials, tax_rate=0.30), {"tax_rate": 0.10})
    assert res["assumptions"]["tax_rate"] == 0.10


def test_history_tax_rate_when_no_user_value(financials):
    assert run_three_statement(_with_latest(financials, tax_rate=0.30))["assumptions"]["tax_rate"] == 0.30


def test_user_zero_tax_rate_is_used(financials):
    res = run_three_statement(_with_latest(financials, tax_rate=0.30), {"tax_rate": 0.0})
    assert res["assumptions"]["tax_rate"] == 0.0


def test_a_reported_zero_cash_is_not_replaced(financials):
    res = run_three_statement(_with_latest(financials, cash=0.0))
    # opening cash 0 -> first-year cash comes from flows, not from a 2%-of-revenue default
    default_cash = run_three_statement(_with_latest(financials, cash=None))["projection"][0]["cash"]
    assert res["projection"][0]["cash"] != default_cash


@pytest.mark.parametrize("field", ["total_debt", "total_equity"])
def test_three_statement_needs_debt_and_equity(financials, field):
    res = run_three_statement(_with_latest(financials, **{field: None}))
    assert "error" in res and field in res["error"]


def test_zero_debt_is_a_value(financials):
    assert "error" not in run_three_statement(_with_latest(financials, total_debt=0.0))


def test_zero_user_assumptions_in_three_statement(financials):
    res = run_three_statement(financials, {"interest_rate": 0.0, "capex_pct_rev": 0.0})
    assert res["assumptions"]["interest_rate"] == 0.0 and res["assumptions"]["capex_pct_rev"] == 0.0


# ── the report shows the states ────────────────────────────────────────────
def _report(financials, **over):
    from RevenueChaser.module_financials.reporting.html_builder import build_report_html
    f = _with_latest(financials, **over)
    return build_report_html({"ticker": "TEST", "financials": f, "modules": {
        "credit": run_credit_capacity(f), "dcf": run_dcf(f, {"terminal_growth": 0.5} if over.get("gordon") else None)},
        "warnings": []})


def test_report_shows_n_m_for_a_loss_maker(financials):
    html = _report(financials, ebitda=-50.0)
    assert html.count("n/m (negative EBITDA)") == 3
    assert "b-stressed" in html


def test_report_shows_the_plain_ratio_otherwise(financials):
    assert "n/m (EBITDA" not in _report(financials)


# ── D5 / M18: per-ticker report files are the CLI's, and the ticker cannot be a path ──
def _prime(financials, tmp_path, monkeypatch):
    import RevenueChaser.module_financials.run as fm
    from RevenueChaser.core.providers import cache as cache_manager
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(fm.quick_remarks_engine, "run_quick_remarks", lambda *a, **k: {})
    cache_manager.save("AAPL", dict(financials))
    return fm


def test_cli_run_writes_the_per_ticker_file(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    fm = _prime(financials, tmp_path, monkeypatch)
    res = fm.run("AAPL", modules=["credit"], output="html", no_fetch=True)
    assert res["paths"]["html"] == str(tmp_path / "out" / "AAPL_report.html")


def test_the_dashboard_report_path_writes_only_that_file(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    fm = _prime(financials, tmp_path, monkeypatch)
    target = tmp_path / "latest" / "financials_latest.html"
    res = fm.run("AAPL", modules=["credit"], output="html", no_fetch=True, report_path=target)
    assert res["paths"]["html"] == str(target) and target.exists()
    assert not (tmp_path / "out" / "AAPL_report.html").exists()


@pytest.mark.parametrize("bad", ["../EVIL", "A/B", "..", "AAPL;rm", "", "A" * 20, "a b"])
def test_a_ticker_that_is_a_path_is_refused(rawstore_tmp_db, tmp_path, monkeypatch, financials, bad):
    fm = _prime(financials, tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        fm.run(bad, modules=["credit"], output="html", no_fetch=True)
    assert not (tmp_path / "out").exists()


def test_the_dashboard_runners_pass_the_single_report_path():
    import inspect
    from RevenueChaser.app import runners
    for fn in (runners.run_quick, runners.run_quick_cached):
        assert "report_path=QUICK_LATEST" in inspect.getsource(fn)
