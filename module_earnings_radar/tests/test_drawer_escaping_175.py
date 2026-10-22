"""Earnings drawer: the chart renderers escape provider text, and a row is found without a selector (#175 review).

Two things only the real script can show, so the page runs under node (``core/tests/board_js.py``):

- ``renderPriceChart`` / ``renderEpsChart`` / ``renderRevenueChart`` put the provider's quarter
  label and date into SVG ``<title>`` and ``<text>`` through ``rcEsc``. Dropping one call used to
  pass every unit test and fail only the browser tier.
- ``selectRow`` and ``buildDrawer`` looked the row up with ``querySelector('tr[data-ticker="' +
  ticker + '"]')``, which throws on a ticker holding ``"`` or ``\\``. They compare
  ``getAttribute('data-ticker')`` instead.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from RevenueChaser.module_earnings_radar.models import TradeCard
from RevenueChaser.core.tests.board_js import run_board_js

Q_EVIL = "\"'><img src=x onerror=__x(1)>"
D_EVIL = "\"'><img src=x onerror=__x(2)>"
Q_ESC = "&lt;img src=x onerror=__x(1)&gt;"
D_ESC = "&lt;img src=x onerror=__x(2)&gt;"


@pytest.fixture
def page(tmp_path, monkeypatch) -> str:
    import RevenueChaser.module_earnings_radar.report as er
    monkeypatch.setattr(er, "OUTPUT_DIR", tmp_path)
    (tmp_path / "earnings").mkdir(parents=True, exist_ok=True)
    quarter = {"q": Q_EVIL, "date": D_EVIL, "implied": 5.0, "actual": 3.0, "eps_actual": 1.2,
               "eps_est": 1.0, "surprise_dollar": 0.2, "surprise_pct": 20.0,
               "revenue_actual": 1.0e9, "revenue_est": 9.0e8}
    card = TradeCard(ticker="DTL", name="Detail Co", cluster="Tech", earnings_date=date(2026, 8, 11),
                     report_time_label="PM", direction_score=20.0, confidence_score=65.0,
                     bias_label="BULL", expected_move_pct=5.0, eps_estimate="1.25",
                     final_action="OPTIONS_BULL", detail_json=json.dumps([quarter, dict(quarter)]))
    out = er.export_html([card], [], {}, filename="earnings/drawer.html")
    return out.read_text(encoding="utf-8") if hasattr(out, "read_text") else out


def test_every_chart_escapes_the_quarter_label_and_the_date(page, tmp_path):
    tail = """
CUR_DETAIL.ticker = 'DTL'; CUR_DETAIL.period = 8;
renderDetail();
console.log(JSON.stringify({price: _els['dr-price-chart'].innerHTML, eps: _els['dr-eps-chart'].innerHTML,
                            rev: _els['dr-rev-chart'].innerHTML}));
"""
    charts = run_board_js(page, tail, tmp_path)
    assert set(charts) == {"price", "eps", "rev"}
    for name, svg in charts.items():
        assert svg.startswith("<svg"), f"{name}: no chart was drawn"
        assert "<img" not in svg, f"{name}: a quarter label or date reached the SVG raw"
        assert Q_ESC in svg and D_ESC in svg, f"{name}: both strings should appear, escaped"


# Rejects what a browser rejects: a selector that ends inside a string.
_SELECTORS = r"""
function validSel(sel){
  var quote = null;
  for (var i = 0; i < sel.length; i++) {
    var c = sel[i];
    if (quote) { if (c === '\\') { i++; } else if (c === quote) { quote = null; } }
    else if (c === '"' || c === "'") { quote = c; }
  }
  return quote === null;
}
var TICKERS = %s;
var rows = TICKERS.map(function(t){
  var attrs = {'data-ticker': t, 'data-name': 'Name of ' + t};
  var row = {sel: false,
    getAttribute: function(k){ return k in attrs ? attrs[k] : null; },
    classList: {add: function(c){ if (c === 'sel') row.sel = true; },
                remove: function(c){ if (c === 'sel') row.sel = false; }},
    querySelector: function(){ return null; }};
  return row;
});
document.querySelectorAll = function(sel){
  if (!validSel(sel)) throw new SyntaxError('invalid selector: ' + sel);
  if (sel === 'tbody tr[data-ticker]') return rows;
  if (sel === 'tbody tr.sel') return rows.filter(function(r){ return r.sel; });
  return [];
};
document.querySelector = function(sel){
  if (!validSel(sel)) throw new SyntaxError('invalid selector: ' + sel);
  var m = /^tbody tr\[data-ticker="((?:[^"\\]|\\.)*)"\]$/.exec(sel);
  if (!m) return _el('');
  var want = m[1].replace(/\\(.)/g, '$1');
  for (var i = 0; i < rows.length; i++) if (rows[i].getAttribute('data-ticker') === want) return rows[i];
  return null;
};
var out = TICKERS.map(function(t, i){
  var res = {ticker: t};
  try { toggleDetail(t, null); } catch (e) { res.error = String(e); return res; }
  res.selected = rows.map(function(r){ return r.sel; });
  res.drawer = _els['drawerIn'].innerHTML;
  toggleDetail(t, null);   // close again
  return res;
});
console.log(JSON.stringify(out));
"""

TICKERS = ["AAPL", 'A"B', "A\\", "A\"]<img src=x onerror=__x(3)>", "A'B"]


def test_a_ticker_with_a_quote_or_backslash_still_opens_its_own_row(page, tmp_path):
    results = run_board_js(page, _SELECTORS % json.dumps(TICKERS), tmp_path)
    assert [r["ticker"] for r in results] == TICKERS
    for i, res in enumerate(results):
        assert "error" not in res, f"{res['ticker']!r}: {res.get('error')}"
        assert res["selected"] == [j == i for j in range(len(TICKERS))], f"{res['ticker']!r}: wrong row"
        assert "<img" not in res["drawer"], f"{res['ticker']!r}: the drawer header is raw"
        assert "Name of " in res["drawer"], f"{res['ticker']!r}: the name was not read from its own row"
