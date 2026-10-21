"""#383 R3 slice 0 (K10 move): Swing and Fundamentals keep their rows outside the HTML.

Card 6/7 sends every candidate to the page, and C12 grows the candidate count, so both boards move to the K10
payload route first (common review §5 R3):

- ``render_*_board(payload)`` returns the page and its rows; the page holds no row and fetches
  ``/view/<mod>/payload.json`` through ``rcPayload``;
- the runner writes both files with ``reporting_common.write_board``; a run with no rows writes no payload and
  drops a stale one (the empty / no-data message stays server-rendered, DF111);
- ``render_*_dashboard(payload)`` stays the one-file page (tests, the hostile-fixture guard): the same HTML with the
  rows inlined, so it boots without a fetch.
Offline, temp folders.
"""
from __future__ import annotations

import gzip
import json

import pytest

from RevenueChaser.app import runners
from RevenueChaser.core import reporting_common as rc
from RevenueChaser.core.tests.board_js import run_board_js

_MARK = "ZqxRowName"


def _gate(name: str) -> dict:
    return {"name": name, "passed": True, "score": "3/3", "known": 1, "total": 1,
            "rules": [{"label": "rule", "passed": True, "threshold": "> 1", "unit": "ratio", "actual": 1.2}]}


def _swing_payload(n: int = 2) -> dict:
    cands = [{"ticker": f"T{i}", "name": f"{_MARK}{i}", "price": 10.0, "change_1w": 0.01, "change_1m": 0.01,
              "market_cap": 1e9, "sector": "Technology", "industry": "Software", "themes": [], "index": [],
              "earnings_date": None, "multiplier": 3, "direction": "LONG", "quality": 80, "rs_rating": 90,
              "entry": 10.0, "target": 12.0, "stop": 9.0, "rr": 2.0, "atr": 0.5,
              "gates": {k: _gate(k) for k in ("minervini", "weinstein", "qullamaggie")}} for i in range(n)]
    return {"candidates": cands, "passing": {}, "summary": {}, "universe_total": n, "is_fixture": False}


def _fund_payload(n: int = 2) -> dict:
    cands = [{"ticker": f"T{i}", "name": f"{_MARK}{i}", "price": 50.0, "market_cap": 1e9, "sector": "Technology",
              "industry": "Software", "themes": [], "earnings_date": None, "period": "Q2/2026 vs Q2/2025",
              "distorted": False, "multiplier": 3, "pe": 15.0, "pb": 2.0, "roic": 0.2,
              "gates": {k: _gate(k) for k in ("rule_of_40", "quality", "momentum")}} for i in range(n)]
    return {"candidates": cands, "tiers": {}, "passing": {}, "summary": {}, "universe_total": n,
            "is_fixture": False}


def _boards():
    from RevenueChaser.module_fundamentals import report as fr
    from RevenueChaser.module_swing_potential import report as sr
    return {
        "swing": (sr.render_swing_board, sr.render_swing_dashboard, _swing_payload, "module_swing_potential"),
        "fund": (fr.render_fundamental_board, fr.render_fundamental_dashboard, _fund_payload,
                 "module_fundamentals"),
    }


BOARDS = ("swing", "fund")


@pytest.mark.parametrize("board", BOARDS)
def test_the_board_page_holds_no_row_and_its_payload_holds_them_all(board, rawstore_tmp_db):
    render_board, _, make, mod = _boards()[board]
    html, data = render_board(make(3))
    assert _MARK not in html, "a row reached the HTML"
    assert [r["ticker"] for r in data["rows"]] == ["T0", "T1", "T2"]
    assert all("gics" in r and "theme_ids" in r for r in data["rows"])
    assert "rcPayload(" in html and json.dumps(mod) in html


@pytest.mark.parametrize("board", BOARDS)
def test_the_page_renders_the_cards_it_fetched(board, tmp_path, rawstore_tmp_db):
    render_board, _, make, mod = _boards()[board]
    html, data = render_board(make(2))
    out = run_board_js(html, """
var asked = null;
rcPayload = function(m){ asked = m; return Promise.resolve(%s); };
start();
setTimeout(function(){ console.log(JSON.stringify({mod: asked, grid: _els['grid'].innerHTML,
                                                    count: _els['count'].textContent})); }, 0);
""" % json.dumps(data), tmp_path)
    assert out["mod"] == mod
    assert f"{_MARK}0" in out["grid"] and f"{_MARK}1" in out["grid"]
    # #386 F3 renamed the Fundamentals count ("names"); Swing keeps "setups".
    assert out["count"] == ("2 setups shown" if board == "swing" else "2 names shown")


@pytest.mark.parametrize("board", BOARDS)
def test_a_failed_payload_fetch_says_so(board, tmp_path, rawstore_tmp_db):
    render_board, _, make, _ = _boards()[board]
    html, _ = render_board(make(1))
    out = run_board_js(html, """
rcPayload = function(){ return Promise.reject(new Error("payload 404")); };
start();
setTimeout(function(){ console.log(JSON.stringify(_els['count'].textContent)); }, 0);
""", tmp_path)
    assert "payload 404" in out and "Refresh" in out


@pytest.mark.parametrize("board", BOARDS)
def test_the_one_file_page_inlines_the_rows_and_needs_no_fetch(board, tmp_path, rawstore_tmp_db):
    _, render_page, make, _ = _boards()[board]
    html = render_page(make(2))
    out = run_board_js(html, "console.log(JSON.stringify(_els['grid'].innerHTML));", tmp_path)
    assert f"{_MARK}0" in out and f"{_MARK}1" in out      # the DOM stub's fetch never resolves


@pytest.mark.parametrize("board", BOARDS)
def test_an_empty_board_has_no_payload_and_keeps_its_server_message(board, tmp_path, rawstore_tmp_db):
    render_board, _, make, _ = _boards()[board]
    p = make(0)
    p.update(no_data=True, no_data_message="No stored scan yet.", no_data_reason="no_scan_stored")
    html, data = render_board(p)
    assert data is None
    assert "No stored scan yet." in html
    out = run_board_js(html, """
var asked = false;
rcPayload = function(){ asked = true; return new Promise(function(){}); };
start();
console.log(JSON.stringify({asked: asked, count: _els['count'].textContent, grid: (_els['grid'] || {innerHTML: ''}).innerHTML}));
""", tmp_path)
    expected = "0 setups shown" if board == "swing" else "0 names shown"    # #386 F3 (#F)
    assert out == {"asked": False, "count": expected, "grid": ""}   # DF111: the server message stays


@pytest.fixture
def runner_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "SWING_LATEST", tmp_path / "swing_potential_latest.html")
    monkeypatch.setattr(runners, "FUNDAMENTALS_LATEST", tmp_path / "fundamentals_latest.html")
    from RevenueChaser.module_fundamentals import pipeline as fp
    from RevenueChaser.module_swing_potential import pipeline as sp
    for mod in (sp, fp):
        monkeypatch.setattr(mod, "save_payload", lambda p: None)
    return tmp_path


def _run(board, payload, monkeypatch):
    from RevenueChaser.module_fundamentals import pipeline as fp
    from RevenueChaser.module_swing_potential import pipeline as sp
    mod, run, latest = {"swing": (sp, runners.run_swing, "SWING_LATEST"),
                        "fund": (fp, runners.run_fundamentals, "FUNDAMENTALS_LATEST")}[board]
    monkeypatch.setattr(mod, "run_scan", lambda *a, **k: payload)
    run(None, no_fetch=True)
    return getattr(runners, latest)


@pytest.mark.parametrize("board", BOARDS)
def test_the_runner_writes_the_html_and_the_payload_file(board, runner_paths, monkeypatch, rawstore_tmp_db):
    make = _boards()[board][2]
    html_path = _run(board, make(2), monkeypatch)
    assert _MARK not in html_path.read_text(encoding="utf-8")
    rows = json.loads(gzip.decompress(rc.payload_path(html_path).read_bytes()))["rows"]
    assert [r["ticker"] for r in rows] == ["T0", "T1"]


@pytest.mark.parametrize("board", BOARDS)
def test_a_run_with_no_rows_drops_the_stale_payload(board, runner_paths, monkeypatch, rawstore_tmp_db):
    make = _boards()[board][2]
    html_path = _run(board, make(2), monkeypatch)
    assert rc.payload_path(html_path).exists()
    p = make(0)
    p.update(no_data=True, no_data_message="No stored scan yet.", no_data_reason="no_scan_stored")
    _run(board, p, monkeypatch)
    assert not rc.payload_path(html_path).exists()
    assert "No stored scan yet." in html_path.read_text(encoding="utf-8")
