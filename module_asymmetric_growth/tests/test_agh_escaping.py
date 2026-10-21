"""Asymmetric Growth board: provider strings never reach markup raw (ADR-X1, #144).

The cards are built in client JS, so these run the board's real script under
node (``core/tests/board_js.py``) and read ``innerHTML`` back. The cards reach the page through the payload
route (S7, K10), which the tests answer with ``board_payload``.
"""
from __future__ import annotations

import copy

from RevenueChaser.core.tests.board_js import run_board_js, with_payload
from RevenueChaser.module_asymmetric_growth.report import board_payload, render_asymmetric_growth_hunter_html

_EVIL = "\"'><img src=x onerror=alert(1)>"

_THR = {"rev_growth": 0.2, "gross_margin": 0.4, "net_debt_assets": 0.1, "runway": 2.0,
        "dist_52w": 0.1, "vol_ratio": 1.4, "rd_intensity": 0.15, "dilution": 0.2,
        "accelerating": True, "fcf": True, "above_sma50": True}


def _result() -> dict:
    card = {"ticker": "MU", "name": "Micron", "sector": "IT", "industry": "Semis",
            "index": ["S&P 500"], "themes": ["Memory"], "total_points": 8, "price": 100.0,
            "market_cap": 1e11, "rev_growth": 0.5, "gross_margin": 0.5, "ps_ratio": 3.0,
            "fcf": 1e9, "runway_years": 5, "rd_intensity": 0.2, "dilution_yoy": 0.01,
            "dilution_warning": False, "tech_breakthrough": True,
            "signals": [{"key": "growth", "name": "Growth", "passed": 1, "total": 1,
                         "subs": [{"label": "Revenue growth", "value": 0.5, "unit": "pct",
                                   "threshold": 0.2, "op": "gte", "param": "rev_growth"}]}],
            "inst_holders": 10, "inst_new": 1, "inst_exited": 0, "inst_buy_flow": 1.0,
            "inst_sell_flow": 0.0, "inst_shares_delta_pct": 0.1, "earnings_date": None}
    return {"asof": "2026-08-23T00:00:00", "universe_total": 1,
            "summary": {"scanned": 1}, "thresholds": dict(_THR), "cards": [card]}


def _hostile(field: str) -> dict:
    r = copy.deepcopy(_result())
    c = r["cards"][0]
    if field in ("themes", "index"):
        c[field] = [_EVIL + field]
    elif field == "signal":
        c["signals"][0]["name"] = _EVIL + "signal"
        c["signals"][0]["total"] = _EVIL + "total"
    else:
        c[field] = _EVIL + field
    return r


def test_card_fields_are_escaped(tmp_path):
    for field in ("ticker", "name", "sector", "industry", "themes", "index", "signal"):
        r = _hostile(field)
        html = render_asymmetric_growth_hunter_html(r)
        grid = run_board_js(html, "console.log(JSON.stringify(_els['grid'].innerHTML));",
                            tmp_path, payload=board_payload(r))
        assert "&lt;img src=x" in grid, field
        assert "<img" not in grid, field


def test_a_comment_opener_in_a_name_keeps_the_script_intact(tmp_path):
    """F3: ``<!--<script>`` in a value blanked the board."""
    from RevenueChaser.core.tests.board_js import PROBE, assert_scripts_intact
    r = copy.deepcopy(_result())
    r["cards"][0]["name"] = PROBE
    html = render_asymmetric_growth_hunter_html(r)
    assert "!--" not in html                                # the cards are not in the page (S7)
    assert_scripts_intact(with_payload(html, board_payload(r)), tmp_path)


def test_the_board_filters_on_sector_then_industry(tmp_path, rawstore_tmp_db):
    """#266, #383 C1c: the shared filter kit narrows the grid by sector, then by the two-level path."""
    from RevenueChaser.core import rawstore
    r = _result()
    base = r["cards"][0]
    r["cards"] = [dict(copy.deepcopy(base), ticker=t, sector=s, industry=i) for t, s, i in (
        ("AAA", "Healthcare", "Biotechnology"), ("BBB", "Healthcare", "Medical Devices"),
        ("CCC", "Technology", "Software - Application"))]
    rawstore.write_rows("equity_universe", [
        {"ticker": "AAA", "sector": "Healthcare", "industry": "Biotechnology"},
        {"ticker": "BBB", "sector": "Healthcare", "industry": "Medical Devices"},
        {"ticker": "CCC", "sector": "Technology", "industry": "Software - Application"}])
    html = render_asymmetric_growth_hunter_html(r)
    out = run_board_js(html, """
function shown(){ var g = _els['grid'].innerHTML; return ['AAA', 'BBB', 'CCC'].filter(function(t){ return g.indexOf(t) >= 0; }); }
var log = {};
render(); log.all = shown();
RCF.set({gics: {key: 'Healthcare', label: 'Healthcare', level: 1}}); log.sector = shown();
RCF.set({gics: {key: 'Healthcare\\u203aBiotechnology', label: 'Biotechnology', level: 2}}); log.industry = shown();
RCF.set({gics: {key: 'Technology', label: 'Technology', level: 1}}); log.other_sector = shown();
console.log(JSON.stringify(log));
""", tmp_path, payload=board_payload(r))
    assert out == {
        "all": ["AAA", "BBB", "CCC"],
        "sector": ["AAA", "BBB"],
        "industry": ["AAA"],
        "other_sector": ["CCC"]}
