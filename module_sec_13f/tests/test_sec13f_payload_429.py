"""SEC 13F board: the rows leave the HTML for the payload route (#429, K10 #383 C6a).

A 14,656-ticker page inlined every row (about 27 MB). The page is now chrome and scripts; its rows are
``report.board_payload`` served gzip at ``GET /view/module_sec_13f/payload.json`` and built by the page's JS with
``createElement``/``textContent``. The board shows the same rows, order, texts, classes, titles and sort values.
Offline, temp folders.
"""
from __future__ import annotations

import gzip
import json
import re

from RevenueChaser.core.reporting_common import payload_path
from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_sec_13f import report


def _cell(shares, **kw):
    return {"shares": shares, "value": shares * 10.0, "holders": 2, "new_holders": 1, "exited_holders": 0,
            "top10_pct": 90.0, "hhi": 4000.0, "buy_flow": 1.0, "sell_flow": 0.0, **kw}


def _three():
    totals = {"AAA": {"2025-03-31": _cell(100.0), "2025-06-30": _cell(300.0)},
              "BBB": {"2025-03-31": _cell(100.0), "2025-06-30": _cell(200.0)},
              "CCC": {"2025-03-31": _cell(100.0), "2025-06-30": _cell(100.0)}}
    meta = {"AAA": {"name": "Alpha", "sector": "Technology"},
            "BBB": {"name": "Bravo", "sector": "Energy"},
            "CCC": {"name": "Charlie", "sector": "Energy"}}
    return totals, meta


def _cells(payload, ticker):
    for row in payload["rows"]:
        if row[0] == ticker:
            return dict(zip(payload["cols"], row[3]))
    raise AssertionError(f"{ticker} not in the payload")


def test_the_page_holds_no_rows_and_loads_them_from_the_payload():
    totals, meta = _three()
    html = report.render_sec13f_dashboard(totals, meta, {})
    assert "3 tickers" in html
    assert not re.search(r"<tr[^>]*data-ticker=", html)     # no inline row markup
    for t in ("AAA", "BBB", "CCC"):
        assert t not in html                                # no ticker text in the page either
    assert 'rcPayload("module_sec_13f")' in html
    assert "function rcPayload" in html                     # the shared page side (PAYLOAD_JS)


def test_the_cols_are_the_numbers_the_page_sorts_on():
    totals, meta = _three()
    payload = report.board_payload(totals, meta, {})
    assert payload["cols"] == ["sh_2025-03-31", "sh_2025-06-30", "ch_2025-06-30",
                               "value", "pct_out", "holders", "new_exit", "breadth", "top10", "hhi", "netflow",
                               "streak", "accel", "insider", "short", "price_delta"]


def test_the_rows_are_in_board_order_most_shares_first():
    totals, meta = _three()
    payload = report.board_payload(totals, meta, {})
    assert [row[0] for row in payload["rows"]] == ["AAA", "BBB", "CCC"]


def test_the_cells_carry_todays_text_val_and_class():
    """The exact cells of ``test_sec13f.TestDashboard.test_render_with_new_columns``' RKT fixture."""
    totals = {"RKT": {"2025-03-31": {"shares": 100.0, "value": 1000.0, "holders": 2,
                                     "new_holders": None, "exited_holders": None,
                                     "top10_pct": 100.0, "hhi": 5000.0,
                                     "buy_flow": None, "sell_flow": None},
                      "2025-06-30": {"shares": 150.0, "value": 1500.0, "holders": 2,
                                     "new_holders": 1, "exited_holders": 1,
                                     "top10_pct": 90.0, "hhi": 4000.0,
                                     "buy_flow": 60.0, "sell_flow": 10.0}}}
    meta = {"RKT": {"name": "Rocket Companies Inc", "sector": "Financial Services"}}
    enrich = {"RKT": {"price": {"2025-03-31": 10.0, "2025-06-30": 12.0},
                      "shares_outstanding": 2000.0, "short_pct": 5.2, "insider_pct": 1.4}}
    payload = report.board_payload(totals, meta, enrich)
    cells = _cells(payload, "RKT")
    assert cells["sh_2025-06-30"] == ["150", 150.0, "zero", "150 shares"]
    assert cells["ch_2025-06-30"] == ["+50.0%", 50.0, "pos"]
    assert cells["value"] == ["$1.5K", 1500.0, "zero"]
    assert cells["pct_out"] == ["7.5%", 7.5, "zero"]
    assert cells["new_exit"] == ["+1/−1", 0, "zero"]
    assert cells["netflow"] == ["50", 50.0, "pos"]
    assert cells["streak"] == ["1", 1, "zero"]


def test_an_empty_board_writes_no_payload_and_shows_the_empty_state(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "OUTPUT_DIR", tmp_path)
    path = report.export_sec13f_dashboard({}, {}, {}, filename="sec_13f_latest.html")
    html = path.read_text(encoding="utf-8")
    assert not payload_path(path).exists()
    assert "No SEC 13F data stored yet" in html
    assert "var ROW_COUNT = 0;" in html


def test_export_writes_the_payload_the_page_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "OUTPUT_DIR", tmp_path)
    totals, meta = _three()
    path = report.export_sec13f_dashboard(totals, meta, {}, filename="sec_13f_latest.html")
    stored = json.loads(gzip.decompress(payload_path(path).read_bytes()).decode("utf-8"))
    assert stored == report.board_payload(totals, meta, {})


def test_a_payload_that_does_not_load_says_so_in_the_tbody(tmp_path):
    """#429: a rejected fetch (or a build error) leaves one message row, never a blank board."""
    totals, meta = _three()
    html = report.render_sec13f_dashboard(totals, meta, {})
    log = run_board_js(html, """
var created = [];
var _ce = document.createElement;
document.createElement = function(tag){ var e = _ce(tag); created.push([tag, e]); return e; };
var urls = [];
globalThis.fetch = function(url){ urls.push(url); return Promise.reject(new Error("boom")); };
loadRows();
setTimeout(function(){
  var empties = created.filter(function(c){ return c[1].className === "empty"; });
  console.log(JSON.stringify({text: empties.length ? empties[empties.length - 1][1].textContent : "",
                              urls: urls}));
}, 0);
""", tmp_path, payload=report.board_payload(totals, meta, {}))
    assert "could not be loaded" in log["text"] and "boom" in log["text"]
    assert log["urls"] == ["/view/module_sec_13f/payload.json"]


def _strict(text: str):
    """``JSON.parse`` refuses NaN and Infinity, so the page's ``r.json()`` would fail on them."""
    def refuse(token):
        raise ValueError(f"not JSON: {token}")
    return json.loads(text, parse_constant=refuse)


def test_a_non_finite_number_reaches_the_page_as_an_empty_sort_value(tmp_path, monkeypatch):
    """A NaN or infinite value (e.g. a vendor insider % or price stored as NaN) must not make the whole payload
    unreadable: its sort value is empty, as for a missing number, and the cell keeps today's text."""
    monkeypatch.setattr(report, "OUTPUT_DIR", tmp_path)
    totals, meta = _three()
    enrich = {"AAA": {"shares_outstanding": float("nan"), "insider_pct": float("inf")}}
    path = report.export_sec13f_dashboard(totals, meta, enrich, filename="sec_13f_latest.html")
    stored = _strict(gzip.decompress(payload_path(path).read_bytes()).decode("utf-8"))
    cells = _cells(stored, "AAA")
    assert cells["insider"] == [report._f(float("inf"), 1, "%", False), None, "zero"]
    assert cells["pct_out"][1] is None


def test_the_page_builds_one_row_per_payload_row_on_load(tmp_path):
    """#429/KF: the page loads the payload by itself and builds every row (ticker, sector cells, every cell) from it,
    then mounts the filter kit. Rows no longer carry ``data-sector``; the kit filters via ``FIELDS``."""
    totals, meta = _three()
    payload = report.board_payload(totals, meta, {})
    html = report.render_sec13f_dashboard(totals, meta, {})
    # The shared DOM stub has no fragment or text node; these two are enough for ``build``.
    html = html.replace("var ROW_COUNT", "var created = [];\n"
                        "document.createDocumentFragment = function(){ return document.createElement('fragment'); };\n"
                        "document.createTextNode = function(t){ return {textContent: t}; };\n"
                        "var _ce = document.createElement;\n"
                        "document.createElement = function(tag){ var e = _ce(tag); created.push([tag, e]); return e; };\n"
                        "var ROW_COUNT", 1)
    log = run_board_js(html, f"""
var trs = created.filter(function(c){{ return c[0] === "tr"; }}).map(function(c){{ return c[1]; }});
var tds = created.filter(function(c){{ return c[0] === "td"; }}).map(function(c){{ return c[1]; }});
console.log(JSON.stringify({{
  rows: trs.filter(function(t){{ return t.getAttribute("data-ticker"); }})
           .map(function(t){{ return [t.getAttribute("data-ticker"), t.getAttribute("data-sector")]; }}),
  cols: tds.slice(0, {2 + len(payload["cols"])}).map(function(t){{ return t.getAttribute("data-col"); }}),
  first: tds.slice(0, 4).map(function(t){{ return [t.getAttribute("data-val"), t.className, t.getAttribute("title"), t.textContent]; }})
}}));
""", tmp_path, payload=payload)
    assert log["rows"] == [["AAA", None], ["BBB", None], ["CCC", None]]
    assert log["cols"] == ["ticker", "sector"] + payload["cols"]
    assert log["first"][2] == ["100", "zero", "100 shares", "100"]          # sh_2025-03-30 of AAA


def test_a_refresh_with_no_totals_drops_the_old_payload(tmp_path, monkeypatch):
    """The empty board is written in place of the page; the previous rows must not stay served beside it."""
    import pytest
    from RevenueChaser.app import runners
    from RevenueChaser.app.jobs import JobMessage
    from RevenueChaser.module_sec_13f import engine
    page = tmp_path / "sec_13f_latest.html"
    monkeypatch.setattr(runners, "SEC_13F_LATEST", page)
    monkeypatch.setattr(engine, "load_totals", lambda: {})
    payload_path(page).write_bytes(gzip.compress(b'{"cols": [], "rows": [["OLD", "", "", []]]}'))
    with pytest.raises(JobMessage):
        runners.run_sec_13f_latest(None)
    assert page.exists() and not payload_path(page).exists()


def test_each_built_row_carries_the_watch_list_pair_for_its_ticker(tmp_path):
    """PR 4: the ticker cell gets the shared pair (``rcWatchButtons``), reading ``data-ticker``, from the payload."""
    totals, meta = _three()
    payload = report.board_payload(totals, meta, {})
    html = report.render_sec13f_dashboard(totals, meta, {})
    html = html.replace("var ROW_COUNT", "var created = [];\n"
                        "document.createDocumentFragment = function(){ return document.createElement('fragment'); };\n"
                        "document.createTextNode = function(t){ return {textContent: t}; };\n"
                        "var _ce = document.createElement;\n"
                        "document.createElement = function(tag){ var e = _ce(tag); created.push([tag, e]); return e; };\n"
                        "var ROW_COUNT", 1)
    log = run_board_js(html, """
var tds = created.filter(function(c){ return c[0] === "td" && c[1].getAttribute("data-col") === "ticker"; });
console.log(JSON.stringify(tds.map(function(c){ return c[1].innerHTML; })));
""", tmp_path, payload=payload)
    assert len(log) == 3
    for t, cell in zip(("AAA", "BBB", "CCC"), log):
        assert f'data-rc-watch="option" data-ticker="{t}"' in cell and f'data-rc-watch="stock" data-ticker="{t}"' in cell
    assert 'rcWatchInit("sec_13f")' in html
