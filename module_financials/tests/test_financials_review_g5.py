"""The Financials Fable review's fixes (build plan PR 12, group G5). No network.

S1: a base-year input the statements do not give is a default, and the board says so instead of showing it as a
measured number (the #173 convention: missing is unknown, never a silent number).
S4: the header's "Last sync" and the comps' "as of" name the same New York session (core/calendar rule B), and a
fetch inside the session says "intraday", not "EOD".
"""
from __future__ import annotations

import copy
from datetime import date

import pytest

from RevenueChaser.module_financials import history as fin_history
from RevenueChaser.module_financials.engines.dcf_engine import run_dcf
from RevenueChaser.module_financials.engines.three_statement_engine import run_three_statement
from RevenueChaser.module_financials.reporting.html_builder import build_report_html

NWC_LINES = ("receivables", "inventory", "payables", "current_assets", "current_liabilities")


def _drop(financials, *keys, every_year=False):
    f = copy.deepcopy(financials)
    for rec in f["annual"] if every_year else f["annual"][-1:]:
        for key in keys:
            rec[key] = None
    return f


def _html(financials, modules):
    return build_report_html({"ticker": "TEST", "financials": financials, "modules": modules, "warnings": []})


# ── S1: the DCF's base year ─────────────────────────────────────────────────
def test_a_measured_base_year_carries_no_default(financials):
    res = run_dcf(financials)
    assert res["base_year"]["defaults"] == [] and res["wacc"]["weights_source"] == "market"
    assert not [w for w in res["warnings"] if "assumed" in w]


def test_a_missing_ebitda_is_a_labelled_default_margin(financials):
    res = run_dcf(_drop(financials, "ebitda", "ebit"))
    assert res["base_year"]["ebitda_margin"] == pytest.approx(0.30)          # the number is unchanged
    assert "ebitda_margin" in res["base_year"]["defaults"]
    assert "EBITDA margin: assumed 30.0% (not reported)" in res["warnings"]


def test_a_margin_the_user_typed_is_not_warned_as_assumed(financials):
    res = run_dcf(_drop(financials, "ebitda", "ebit"), {"ebitda_margin": 0.2})
    assert "ebitda_margin" in res["base_year"]["defaults"]                  # the base year itself still has none
    assert not [w for w in res["warnings"] if w.startswith("EBITDA margin")]


def test_a_missing_capex_is_a_labelled_default(financials):
    res = run_dcf(_drop(financials, "capex"))
    assert res["base_year"]["capex_pct_rev"] == pytest.approx(0.05)
    assert "capex_pct_rev" in res["base_year"]["defaults"]
    assert "Capex: assumed 5.0% of revenue (not reported)" in res["warnings"]


def test_a_capex_the_user_typed_is_no_default(financials):
    res = run_dcf(_drop(financials, "capex"), {"capex_pct_rev": 0.07})
    assert "capex_pct_rev" not in res["base_year"]["defaults"]
    assert not [w for w in res["warnings"] if w.startswith("Capex")]


def test_no_working_capital_lines_is_a_labelled_default_nwc(financials):
    res = run_dcf(_drop(financials, *NWC_LINES, every_year=True))
    assert res["base_year"]["nwc_pct_rev"] == 0.0
    assert "nwc_pct_rev" in res["base_year"]["defaults"]
    assert "NWC: assumed 0.0% of revenue (no working-capital lines reported)" in res["warnings"]


def test_one_year_with_working_capital_lines_is_enough(financials):
    f = _drop(financials, *NWC_LINES, every_year=True)
    f["annual"][0].update(receivables=80.0, inventory=60.0, payables=40.0)
    assert "nwc_pct_rev" not in run_dcf(f)["base_year"]["defaults"]


def test_a_missing_dna_marks_its_row_default_too(financials):
    assert "dep_pct_rev" in run_dcf(_drop(financials, "depreciation"))["base_year"]["defaults"]


@pytest.mark.parametrize("key", ["market_cap", "total_debt"])
def test_no_market_cap_or_debt_is_a_labelled_default_weight(financials, key):
    f = copy.deepcopy(financials)
    if key == "market_cap":
        f["market_cap"] = None
    else:
        f["annual"][-1]["total_debt"] = None
    res = run_dcf(f)
    assert (res["wacc"]["equity_weight"], res["wacc"]["debt_weight"]) == (0.85, 0.15)
    assert res["wacc"]["weights_source"] == "default"
    assert "WACC weights: assumed 85% equity / 15% debt (market cap or total debt missing)" in res["warnings"]


def test_a_debt_weight_the_user_typed_is_an_assumption(financials):
    res = run_dcf(dict(financials, market_cap=None), {"debt_weight": 0.3})
    assert res["wacc"]["weights_source"] == "assumption"
    assert not [w for w in res["warnings"] if w.startswith("WACC weights")]


# ── S1: the three-statement model ──────────────────────────────────────────
def test_the_three_statement_model_labels_each_default(financials):
    res = run_three_statement(_drop(financials, "ebitda", "ebit", "capex", "receivables", "inventory", "payables"))
    assert res["defaults"] == ["ebitda_margin", "capex_pct_rev", "dso", "dio", "dpo"]
    for line in ("EBITDA margin: assumed 30.0% (not reported)", "Capex: assumed 5.0% of revenue (not reported)",
                 "Receivable days: assumed 45 (not reported)", "Inventory days: assumed 60 (not reported)",
                 "Payable days: assumed 45 (not reported)"):
        assert line in res["warnings"]


def test_a_measured_three_statement_model_has_no_default(financials):
    res = run_three_statement(financials)
    assert res["defaults"] == [] and not [w for w in res["warnings"] if "assumed" in w]


def test_days_the_user_typed_are_no_default(financials):
    res = run_three_statement(_drop(financials, "receivables"), {"dso": 30})
    assert "dso" not in res["defaults"]


# ── S1: the page ────────────────────────────────────────────────────────────
def test_the_page_labels_the_default_rows(financials):
    f = _drop(financials, "ebitda", "ebit", "capex", "depreciation")
    f["market_cap"] = None
    for rec in f["annual"]:
        for key in NWC_LINES:
            rec[key] = None
    html = _html(f, {"dcf": run_dcf(f)})
    for row in ("default 30.0%", "default 5.0%", "default 0.0%", "default 85.0%", "default 15.0%"):
        assert row in html
    assert html.count("default 0.0%") == 2                                  # D&A and NWC


def test_a_measured_page_says_default_nowhere(financials):
    assert "default" not in _html(financials, {"dcf": run_dcf(financials)})


# ── S4: one New York day in the header and in the comps' "as of" ───────────────
@pytest.mark.parametrize("stamp, header", [
    ("2026-10-06T20:45:00+00:00", "Tuesday, October 06, 2026 EOD"),        # 16:45 ET, the session is complete
    ("2026-10-07T02:00:00+00:00", "Tuesday, October 06, 2026 EOD"),        # 22:00 ET: UTC already says Wednesday
    ("2026-10-07T12:00:00+00:00", "Tuesday, October 06, 2026 EOD"),        # 08:00 ET Wednesday, before the open
    ("2026-10-10T15:00:00+00:00", "Friday, October 09, 2026 EOD"),         # a Saturday: Friday's close
    ("2026-10-06T15:00:00+00:00", "Tuesday, October 06, 2026 intraday"),   # 11:00 ET, inside the session
])
def test_the_header_and_the_comps_name_the_same_new_york_session(financials, stamp, header):
    financials["_fetched_at_iso"] = stamp
    assert f"Last sync: {header}<" in _html(financials, {})
    assert header.startswith(f"{date.fromisoformat(fin_history._as_of(stamp, None)):%A, %B %d, %Y} ")


def test_an_intraday_fetch_is_not_called_end_of_day(financials):
    financials["_fetched_at_iso"] = "2026-10-06T15:00:00+00:00"
    assert "EOD" not in _html(financials, {})
