"""13F: inline handlers read ``data-*`` and every period string is escaped (ADR-X1, #175).

The old ``onclick="setSector('{sector}')"`` chip handlers are gone with the sector chips.
Sorting still reads ``this.dataset.col``. Payload data (rows and filter fields) is JSON-escaped
by ``html_safe.script_json`` and reaches the DOM through ``textContent``/``createElement``.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from RevenueChaser.core.tests.board_js import inline_scripts, run_board_js
from RevenueChaser.module_sec_13f import report

EVIL = "Fin\"'>);__x(1);//<img src=x onerror=__x(2)>"
# sorts after "2025-03-31", so the period header keeps it
EVIL_Q = "<img src=x onerror=__x(3)>\"');__x(4);//"


def _row(shares: float) -> dict:
    return {"shares": shares, "value": shares * 10, "holders": 2, "new_holders": 1, "exited_holders": 0,
            "top10_pct": 90.0, "hhi": 4000.0, "buy_flow": 1.0, "sell_flow": 0.0}


@pytest.fixture
def page(monkeypatch) -> str:
    monkeypatch.setattr(report, "sector_or_unclassified", lambda s: s)
    totals = {"AAA": {"2025-03-31": _row(100.0), "2025-06-30": _row(150.0), EVIL_Q: _row(200.0)}}
    return report.render_sec13f_dashboard(totals, {"AAA": {"name": "A", "sector": EVIL}})


class _Handlers(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heads: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "th" and "data-col" in a:
            self.heads.append({"onclick": a.get("onclick", ""), "data": a["data-col"]})


def _handlers(page: str) -> list[dict]:
    parser = _Handlers()
    parser.feed(page)
    return [h for h in parser.heads if h["onclick"]]


def test_no_sector_chips_or_setsector_remain(page):
    assert "setSector" not in page
    assert "filter-btn" not in page
    assert "data-sector=" not in page


def test_sort_handler_reads_its_data_attribute(page):
    for h in _handlers(page):
        assert h["onclick"] == "sortTable(this.dataset.col,this)", h


def test_sort_handler_receives_its_data_attribute(page, tmp_path):
    import json
    handlers = _handlers(page)
    evil_h = next(h for h in handlers if h["data"] == "sh_" + EVIL_Q)
    tail = """
var el = {dataset: {col: %s}, classList: {add: function(){}}};
try { (function(){ eval("sortTable(this.dataset.col,this)"); }).call(el); } catch(e){ console.log(JSON.stringify({error: String(e)})); }
console.log(JSON.stringify({seen: SORT_COL}));
""" % json.dumps(evil_h["data"])
    got = run_board_js(page, tail, tmp_path)
    assert got.get("error") is None, got
    assert got["seen"] == evil_h["data"]


def test_all_sort_handlers_use_data_attributes(page):
    for h in _handlers(page):
        assert h["onclick"] == "sortTable(this.dataset.col,this)", h


def test_period_text_is_escaped_in_headers(page):
    assert "<img src=x onerror" not in page
    assert 'data-col="sh_&lt;img src=x onerror=__x(3)&gt;' in page


def test_inline_scripts_are_balanced_and_escape_safe(page):
    low = page.lower()
    assert low.count("<script") == low.count("</script"), "unbalanced <script>"
    for i, body in enumerate(inline_scripts(page)):
        assert "<!--" not in body, f"script {i} holds <!--"
        assert "<script" not in body.lower(), f"script {i} holds <script"


def test_payload_fields_are_json_escaped(monkeypatch, tmp_path):
    """Hostile ticker/name/sector in the payload do not break the JSON or the script."""
    import json
    from RevenueChaser.core import universe

    def fake_filter_fields(tickers):
        return {
            EVIL: {"ticker": EVIL, "name": EVIL, "index": [], "market_cap": None,
                   "gics": [EVIL, ""], "theme_ids": [EVIL], "earnings_date": None},
        }

    monkeypatch.setattr(universe, "filter_fields", fake_filter_fields)
    monkeypatch.setattr(universe, "theme_catalog", lambda: {
        EVIL: {"label": EVIL, "level": "coarse", "parent": ""}})
    payload = report.board_payload({EVIL: {"2025-06-30": _row(100.0)}},
                                    {EVIL: {"name": EVIL, "sector": EVIL}}, {})
    html = report.render_sec13f_dashboard({EVIL: {"2025-06-30": _row(100.0)}},
                                           {EVIL: {"name": EVIL, "sector": EVIL}}, {})
    # The hostile ticker is in the payload fields and the META themes; both go through script_json.
    assert "<img src=x onerror" not in html
    assert 'data-sector="Fin' not in html
    low = html.lower()
    assert low.count("<script") == low.count("</script"), "unbalanced <script>"
    for i, body in enumerate(inline_scripts(html)):
        assert "<!--" not in body, f"script {i} holds <!--"
        assert "<script" not in body.lower(), f"script {i} holds <script"
    # Payload round-trips through JSON.
    assert payload["fields"][EVIL]["ticker"] == EVIL
    assert payload["fields"][EVIL]["gics"][0] == EVIL
