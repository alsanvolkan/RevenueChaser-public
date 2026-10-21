"""13F board: the default sort column is embedded as JSON (ADR-X1 F3, #144)."""
from __future__ import annotations

from RevenueChaser.core.tests.board_js import assert_scripts_parse


def test_a_quote_in_the_period_does_not_break_the_script(tmp_path):
    from RevenueChaser.module_sec_13f.report import render_sec13f_dashboard
    cell = {"shares": 100.0, "value": 1000.0, "holders": 2}
    totals = {"AAA": {"2025-03-31": dict(cell), "2025-06-30'": dict(cell, shares=120.0)}}
    html = render_sec13f_dashboard(totals, {"AAA": {"name": "A", "sector": "Financial Services"}}, {})
    assert "var SORT_COL = \"sh_2025-06-30'\";" in html
    assert_scripts_parse(html, tmp_path)
