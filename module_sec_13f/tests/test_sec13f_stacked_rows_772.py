"""Shares and Δ% stack in one row group, and each chart pane expands on its own (#772).

Owner, 12 Nov 2026: *"Total ownership (shares) per quarter ve Δ% vs prior quarter ayni satir gurubunda alt alta
gosterilebilir, daha az yer kaplar ve okumasi kolay olur"* and *"uzerine basinca cikan iki plota click to expand
ile acilabilir"*. The quarter is written once in the header; the two numbers sit on two lines of the same
``<tbody class="grp">``."""
from __future__ import annotations

import re

import pytest

from RevenueChaser.module_sec_13f import report

TOTALS = {
    "AAA": {"2026-03-31": {"shares": 1_000_000, "value": 20_000_000, "holders": 120, "closed": True},
            "2026-06-30": {"shares": 1_100_000, "value": 23_000_000, "holders": 131, "closed": True}},
}
META = {"AAA": {"name": "Aaa Corp", "sector": "Technology"}}


@pytest.fixture
def sec13f_page() -> str:
    return report.render_sec13f_dashboard(TOTALS, META)


def _header(html: str) -> str:
    return html[html.index("<thead>"):html.index("</thead>")]


def test_the_header_writes_each_quarter_once(sec13f_page):
    head = _header(sec13f_page)
    assert "Δ% vs prior quarter" not in head, "the Δ block is gone; Δ lives under the quarter now"
    quarters = re.findall(r'<th class="qhead">', head)
    sorts = re.findall(r'class="qsort[^"]*" data-col="sh_', head)
    assert quarters and len(sorts) == len(quarters), "every quarter head sorts by shares"


def test_every_quarter_but_the_first_can_sort_by_its_change(sec13f_page):
    head = _header(sec13f_page)
    qheads = len(re.findall(r'<th class="qhead">', head))
    dsorts = len(re.findall(r'class="dsort" data-col="ch_', head))
    assert dsorts == qheads - 1, "the oldest quarter has no change to sort by"


def test_the_rows_are_groups_of_two():
    js = report.JS
    assert "document.createElement('tbody')" in js and "body.className = 'grp'" in js
    assert "tbody.grp" in js, "sorting and filtering work on the group, not on a single row"
    assert "rowLabel('shares')" in js and "rowLabel('Δ vs prior')" in js


def test_the_delta_row_lines_up_under_the_oldest_quarter():
    # The oldest quarter has no change; the row still needs a cell there or every Δ shifts one column left.
    assert "trD.childNodes.length === nq" in report.JS


def test_the_watch_box_comes_before_the_ticker():
    js = report.JS
    i_watch = js.index("insertAdjacentHTML('afterbegin', rcWatchButtons")
    i_ticker = js.index("spT.textContent = r[0]")
    assert i_watch < i_ticker


def test_the_chart_is_two_figures_each_with_its_own_expand(sec13f_page):
    figs = report.build_chart_figs("AAA", TOTALS["AAA"])
    assert len(figs) == 2
    assert [t["name"] for t in figs[0]["data"] if t.get("name")] == ["Shares"]
    assert "Value ($)" in [t.get("name") for t in figs[1]["data"]]
    assert sec13f_page.count('class="expand"') == 2
    assert sec13f_page.count('aria-expanded="false"') >= 2
    assert 'id="chart-shares"' in sec13f_page and 'id="chart-value"' in sec13f_page


def test_expanding_one_pane_folds_the_other():
    js = report.JS
    assert "card.setAttribute('data-expanded', pane)" in js
    assert '#modal-card[data-expanded="shares"] .pane[data-pane="value"]' in report.CSS
