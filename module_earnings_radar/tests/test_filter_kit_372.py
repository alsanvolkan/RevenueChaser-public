"""KE (#372, Card E): the shared filter kit on the Earnings board.

Facets Index, Market cap, Sector, Theme (no Earnings facet). The kit ANDs with the board's own search and
five chips. Every row is filtered, watchlist rows and the excluded section included. A failed ``filter_fields``
read never fails a render: ticker and name only, the bar still mounts, one notice.
"""
import json
import re

import pytest

from RevenueChaser.module_earnings_radar.models import TradeCard
from RevenueChaser.core.tests.board_js import run_board_js

import RevenueChaser.module_earnings_radar.report as er


def _card(ticker="AAPL", **overrides) -> TradeCard:
    defaults = {
        "ticker": ticker, "name": f"{ticker} Corp", "cluster": "Technology",
        "earnings_date": __import__("datetime").date(2026, 8, 11), "report_time_label": "PM",
        "direction_score": 20.0, "confidence_score": 65.0, "bias_label": "BULL",
        "expected_move_pct": 5.0, "eps_estimate": "1.25", "final_action": "OPTIONS_BULL",
        "rule_audit": {"rule2_guidance": {"reasons": [], "available": True}},
    }
    defaults.update(overrides)
    return TradeCard(**defaults)


def _fields(tickers):
    return {t: {"ticker": t, "name": f"{t} Corp", "index": ["S&P 500"] if t == "IN" else [],
                "market_cap": 5e9, "gics": ["Technology", "Software"], "theme_ids": [],
                "earnings_date": None} for t in tickers}


@pytest.fixture
def render(tmp_path, monkeypatch):
    monkeypatch.setattr(er, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr("RevenueChaser.core.universe.filter_fields", _fields)
    monkeypatch.setattr("RevenueChaser.core.universe.theme_catalog", lambda *a, **k: {"t1": {"label": "AI"}})

    def go(cards, **kw):
        path = er.export_html(cards, filename="earnings/x.html", **kw)
        return path.read_text(encoding="utf-8")
    return go


def _json_var(page, name):
    m = re.search(rf"var {name}\s*=\s*(.*?);\n", page)
    assert m, f"{name} not found"
    return json.loads(m.group(1))


def test_bar_mount_and_four_facets(render):
    page = render([_card("IN"), _card("OUT", filter_status="excluded")])
    assert 'id="rc-filters"' in page
    assert "RCFilters.mount(" in page
    meta = _json_var(page, "META")
    assert meta["board"] == "earnings"
    assert meta["facets"] == ["index", "mc", "gics", "theme"]


def test_data_has_one_k1_row_per_card_including_excluded(render):
    page = render([_card("IN"), _card("OUT", filter_status="excluded"), _card("WL")])
    data = _json_var(page, "DATA")
    assert sorted(r["ticker"] for r in data) == ["IN", "OUT", "WL"]
    assert {"ticker", "name", "index", "market_cap", "gics", "theme_ids"} <= set(data[0])


def test_search_and_five_chips_remain_and_kit_is_anded(render):
    page = render([_card("IN")])
    for f in ("score6", "accel", "bull", "liq", "wl"):
        assert f'data-f="{f}"' in page
    assert 'id="q"' in page
    fn = page.split("function applyFilters()", 1)[1].split("function setView", 1)[0]
    for attr in ("data-score", "data-accel", "data-bias", "data-liq", "data-wl"):
        assert attr in fn
    assert "RCF.pass" in fn
    assert "excluded-section" not in fn or ":not" not in fn.split("excluded-section", 1)[0][-40:]


_HARNESS = r"""
var attrs = {
  AAA: {'data-ticker':'AAA','data-name':'AAA Co','data-score':'9','data-accel':'1','data-bias':'BULL','data-liq':'1','data-wl':'0'},
  BBB: {'data-ticker':'BBB','data-name':'BBB Co','data-score':'9','data-accel':'1','data-bias':'BULL','data-liq':'1','data-wl':'0'},
  EXC: {'data-ticker':'EXC','data-name':'EXC Co','data-score':'9','data-accel':'1','data-bias':'BULL','data-liq':'1','data-wl':'0'}
};
function Row(t, section) {
  return {style:{display:''}, _ex: section.excluded,
    getAttribute: function(k){ return attrs[t][k] == null ? '' : String(attrs[t][k]); },
    closest: function(sel){
      if (sel.indexOf('excluded') >= 0) return section.excluded ? section : null;
      if (sel.indexOf('section') >= 0) return section;
      return null;
    }};
}
function Section(excluded, tickers) {
  var sec = {excluded: excluded, n: {textContent: '(' + tickers.length + ')'}, rows: []};
  sec.querySelector = function(sel){
    if (sel.indexOf('excluded-section') >= 0) return excluded ? {className:'excluded-section'} : null;
    if (sel === '.n' || sel.indexOf('.n') >= 0) return sec.n;
    return null;
  };
  sec.querySelectorAll = function(sel){ return sel.indexOf('data-ticker') >= 0 ? sec.rows : []; };
  sec.rows = tickers.map(function(t){ return Row(t, sec); });
  return sec;
}
var tier = Section(false, ['AAA','BBB']);
var excl = Section(true, ['EXC']);
var sections = [tier, excl];
var rows = tier.rows.concat(excl.rows);
document.querySelectorAll = function(sel){
  var skipEx = /:not\([^)]*excluded/.test(sel);
  if (sel.indexOf('data-ticker') >= 0) return skipEx ? rows.filter(function(r){ return !r._ex; }) : rows;
  if (sel.indexOf('section') >= 0) return skipEx ? sections.filter(function(s){ return !s.excluded; }) : sections;
  return [];
};
FIELDS = {AAA:{ticker:'AAA', index:['S&P 500']}, BBB:{ticker:'BBB', index:[]}, EXC:{ticker:'EXC', index:[]}};
"""


def test_apply_filters_ands_the_kit_with_search_and_chips_and_filters_excluded(render, tmp_path):
    page = render([_card("IN"), _card("OUT")])
    tail = _HARNESS + r"""
RCF = {active: function(){ return true; }, pass: function(f){ return (f.index||[]).indexOf('S&P 500') >= 0; }};
STATE = {q:'', filters:{}};
applyFilters();
var afterKit = {AAA: rows[0].style.display, BBB: rows[1].style.display, EXC: rows[2].style.display,
                tier: tier.n.textContent, excl: excl.n.textContent,
                line: document.getElementById('kit-count').textContent};
STATE.filters.wl = true;
applyFilters();
var afterChip = {AAA: rows[0].style.display, line: document.getElementById('kit-count').textContent};
STATE.filters = {};
STATE.q = 'zzz';
applyFilters();
var afterSearch = {AAA: rows[0].style.display};
console.log(JSON.stringify({afterKit: afterKit, afterChip: afterChip, afterSearch: afterSearch}));
"""
    got = run_board_js(page, tail, tmp_path)
    assert got["afterKit"] == {"AAA": "", "BBB": "none", "EXC": "none", "tier": "1/2", "excl": "0/1",
                               "line": "1 of 3 names"}
    assert got["afterChip"]["AAA"] == "none"
    assert got["afterSearch"]["AAA"] == "none"


def test_failed_filter_fields_still_renders_with_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(er, "OUTPUT_DIR", tmp_path)

    def boom(_t):
        raise RuntimeError("store down")
    monkeypatch.setattr("RevenueChaser.core.universe.filter_fields", boom)
    page = er.export_html([_card("IN"), _card("OUT", filter_status="excluded")],
                          filename="earnings/x.html").read_text(encoding="utf-8")
    assert "could not be read" in page
    assert 'id="rc-filters"' in page
    assert "RCFilters.mount(" in page
    data = _json_var(page, "DATA")
    assert sorted(r["ticker"] for r in data) == ["IN", "OUT"]
    assert all(set(r) == {"ticker", "name"} for r in data)


def test_hostile_ticker_and_name_are_escaped_in_data(tmp_path, monkeypatch):
    bad = "</script><img src=x onerror=alert(1)>"
    monkeypatch.setattr(er, "OUTPUT_DIR", tmp_path)

    def fields(tickers):
        return {t: {"ticker": t, "name": bad, "index": [], "market_cap": None,
                    "gics": ["Unclassified"], "theme_ids": [], "earnings_date": None} for t in tickers}
    monkeypatch.setattr("RevenueChaser.core.universe.filter_fields", fields)
    monkeypatch.setattr("RevenueChaser.core.universe.theme_catalog", lambda *a, **k: {})
    page = er.export_html([_card("IN", name=bad)], filename="earnings/x.html").read_text(encoding="utf-8")
    assert bad not in page
    assert "\\u003c/script\\u003e" in page


def test_count_line_mounts(render):
    page = render([_card("IN")])
    assert 'id="kit-count"' in page
    assert 'class="n"' in page and "data-total=" in page


def test_count_line_and_section_counts_only_while_a_filter_is_on(render, tmp_path):
    """No kit filter: no count line, headers keep "(N)". A chip alone: "{shown}/{total}" headers, still no line."""
    page = render([_card("IN")])
    tail = _HARNESS + r"""
RCF = {active: function(){ return false; }, pass: function(){ return false; }};
STATE = {q:'', filters:{}};
applyFilters();
var idle = {AAA: rows[0].style.display, tier: tier.n.textContent, excl: excl.n.textContent,
            line: document.getElementById('kit-count').textContent};
STATE.filters.wl = true;
applyFilters();
var chip = {tier: tier.n.textContent, line: document.getElementById('kit-count').textContent};
console.log(JSON.stringify({idle: idle, chip: chip}));
"""
    got = run_board_js(page, tail, tmp_path)
    assert got["idle"] == {"AAA": "", "tier": "(2)", "excl": "(1)", "line": ""}
    assert got["chip"] == {"tier": "0/2", "line": ""}
