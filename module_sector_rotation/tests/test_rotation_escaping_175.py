"""Rotation: ETF identity never reaches markup raw (ADR-X1, #175).

The quadrant points, table rows, filter buttons, summary chips and the trail page
carry a ticker, a configured name and a group name. Handlers read ``data-*`` (never an
interpolated value), and the two ticker pickers and the Money Flow cards build
``innerHTML`` in client JS, so those run under node (``core/tests/board_js.py``).
"""
from __future__ import annotations

import json
import re
from datetime import date

import pytest

from RevenueChaser.core.models.rotation import ETFRotationSnapshot
from RevenueChaser.core.tests.board_js import run_board_js

EVIL = "\"'><img src=x onerror=__x(1)>"


def _snap(ticker: str, quadrant: str = "LEADING", group: str = "sector") -> ETFRotationSnapshot:
    return ETFRotationSnapshot(
        as_of_date=date(2026, 8, 14), ticker=ticker, group_name=group, close_price=100.0,
        rs_1w=1.0, rs_1m=2.0, rs_3m=3.0, rs_ratio=2.0, rs_momentum=1.0,
        rs_ratio_z=1.0, rs_momentum_z=0.5, rrg_quadrant=quadrant)


@pytest.fixture
def evil_etf(monkeypatch):
    import RevenueChaser.module_sector_rotation.report as rd
    monkeypatch.setitem(rd.ROTATION_ETFS, EVIL + "T", {"name": EVIL + "N", "group": EVIL + "G"})
    monkeypatch.setattr(rd, "coarse_theme_catalog", lambda: {})
    return rd


def _page(rd, tmp_path, monkeypatch) -> str:
    monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
    out = rd.export_rotation_dashboard(
        [_snap("XLK"), _snap(EVIL + "T", "LAGGING", group=EVIL + "G"),
         _snap(EVIL + "T", "WEAKENING")], filename="evil.html", uw=None)
    return out.read_text(encoding="utf-8")


def _trail(rd) -> str:
    entry = lambda t, rz: {"ticker": t, "rs_ratio_z": rz, "rs_momentum_z": 0.2, "quadrant": EVIL + "Q",
                           "rs_ratio": 1.0, "rs_momentum": 0.5, "rs_1w": 1.0, "rs_1m": 2.0, "rs_3m": 3.0}
    series = {"2026-08-07": [entry(EVIL + "T", 0.5)], "2026-08-14": [entry(EVIL + "T", 1.0)]}
    return rd._trail_html(date(2026, 8, 3), date(2026, 8, 14), ["2026-08-07", "2026-08-14"], series)


def test_no_handler_interpolates_a_group_name(evil_etf, tmp_path, monkeypatch, rawstore_tmp_db):
    """``onclick="filterGroup('{group}')"`` was a JS-string injection: handlers read ``data-*``."""
    for html in (_page(evil_etf, tmp_path, monkeypatch), _trail(evil_etf)):
        assert not re.search(r"filter(Group|Trail)\('", html), "a handler still gets an interpolated group"
        assert 'onclick="filterGroup(this.dataset.group)"' in html or 'onclick="filterTrail(this.dataset.group)"' in html
        buttons = re.findall(r'<button class="filter-btn[^>]*>', html)
        assert buttons and all("<img" not in b for b in buttons)
        assert all(b.count('"') % 2 == 0 for b in buttons), "a quote broke out of an attribute"


def test_the_table_and_points_escape_the_ticker_and_name(evil_etf, tmp_path, monkeypatch, rawstore_tmp_db):
    html = _page(evil_etf, tmp_path, monkeypatch)
    assert "<img src=x onerror=__x(1)>" not in html
    assert 'id="caret-&quot;&#x27;&gt;&lt;img src=x onerror=__x(1)&gt;T"' in html
    assert 'data-ticker="&quot;&#x27;&gt;&lt;img src=x onerror=__x(1)&gt;T"' in html
    assert 'data-name="&quot;&#x27;&gt;&lt;img src=x onerror=__x(1)&gt;N"' in html


def test_the_trail_page_escapes_the_ticker_quadrant_and_group(evil_etf):
    html = _trail(evil_etf)
    assert "<img src=x onerror=__x(1)>" not in html
    assert 'data-group="&quot;&#x27;&gt;&lt;img src=x onerror=__x(1)&gt;G"' in html


_PICKER = """
var pts = [{dataset: {group: 'sector', ticker: %(t)s, name: %(n)s}}];
document.querySelector = function(){ return {querySelectorAll: function(){ return pts; }}; };
document.querySelectorAll = function(){ return pts; };
buildPicker();
console.log(JSON.stringify(_els['dd-menu'].innerHTML));
"""


@pytest.mark.parametrize("page", ["rotation", "trail"])
def test_the_ticker_picker_escapes_ticker_and_name(page, evil_etf, tmp_path, monkeypatch, rawstore_tmp_db):
    """Both ``buildPicker`` copies write ``innerHTML``; node runs the real script."""
    html = _page(evil_etf, tmp_path, monkeypatch) if page == "rotation" else _trail(evil_etf)
    tail = _PICKER % {"t": json.dumps(EVIL + "T"), "n": json.dumps(EVIL + "N")}
    menu = run_board_js(html, tail, tmp_path)
    assert "<img" not in menu and "&lt;img src=x onerror=__x(1)&gt;" in menu
    assert menu.count('data-t="') == 1 and 'data-t="&quot;&#39;&gt;' in menu


def test_money_flow_cards_escape_ticker_and_name(evil_etf, tmp_path, monkeypatch, rawstore_tmp_db):
    html = _page(evil_etf, tmp_path, monkeypatch)
    tail = """
var body = _el('mf-body');
document.getElementById('mf-inflows').querySelector = function(){ return body; };
document.getElementById('mf-outflows').querySelector = function(){ return _el('o'); };
globalThis.fetch = function(){ return Promise.resolve({json: function(){ return Promise.resolve(
  {inflows: [{ticker: %(t)s, name: %(n)s, flow_score: 1.5}], outflows: [], asof: '2026-08-14'}); }}); };
renderMoneyFlow();
setTimeout(function(){ console.log(JSON.stringify(body.innerHTML)); }, 20);
""" % {"t": json.dumps(EVIL + "T"), "n": json.dumps(EVIL + "N")}
    card = run_board_js(html, tail, tmp_path)
    assert "<img" not in card and "&lt;img src=x onerror=__x(1)&gt;" in card


def test_the_pages_define_the_escaper_they_call(evil_etf, tmp_path, monkeypatch, rawstore_tmp_db):
    """A page that calls ``rcEsc`` without ``ESC_JS`` throws a ReferenceError at runtime."""
    for html in (_page(evil_etf, tmp_path, monkeypatch), _trail(evil_etf)):
        assert "function rcEsc(" in html
        assert not re.search(r"(?<![A-Za-z])esc\(", html), "a call to an undefined esc()"
