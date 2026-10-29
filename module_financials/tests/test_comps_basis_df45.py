"""DF45 (#45): the peer multiples are Yahoo's trailing twelve months, the target's metrics its latest fiscal year's
statements. Both stay as they are; the comps section now says which basis each side is on."""
from __future__ import annotations

from RevenueChaser.module_financials.engines.comps_engine import run_comps
from RevenueChaser.module_financials.reporting.html_builder import build_report_html


def _fetch(t):
    return {"enterpriseValue": 900.0, "ebitda": 100.0, "totalRevenue": 300.0, "trailingPE": 18.0}


def test_the_comps_result_names_each_sides_basis(financials):
    res = run_comps(financials, ["AAA", "BBB"], fetch_info=_fetch)
    latest = financials["annual"][-1]["fiscal_year"]
    assert res["basis"] == {"peers": "trailing twelve months", "target_fiscal_year": latest,
                            "target_shares": "Yahoo"}


def test_the_board_says_which_basis_each_side_is_on(financials):
    res = run_comps(financials, ["AAA", "BBB"], fetch_info=_fetch)
    html = build_report_html({"ticker": "TEST", "financials": financials, "modules": {"comps": res},
                              "warnings": res["warnings"]})
    latest = financials["annual"][-1]["fiscal_year"]
    assert "peer multiples are trailing twelve months" in html and f"fiscal year {latest}" in html


def test_a_stored_comps_result_from_before_the_label_still_renders(financials):
    res = run_comps(financials, ["AAA", "BBB"], fetch_info=_fetch)
    res.pop("basis")
    html = build_report_html({"ticker": "TEST", "financials": financials, "modules": {"comps": res},
                              "warnings": res["warnings"]})
    assert "Comps Analysis" in html and "trailing twelve months" not in html
