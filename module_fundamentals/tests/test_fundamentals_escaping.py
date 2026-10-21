"""Fundamentals board: provider strings never reach markup raw (ADR-X1, #144).

The cards and the gate modal are built in client JS, so these run the board's
real script under node (``core/tests/board_js.py``) and read ``innerHTML`` back.
"""
from __future__ import annotations

import copy
import json

from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_fundamentals.pipeline import build_payload
from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
from RevenueChaser.module_fundamentals.tests.test_fundamental import _m

_EVIL = "\"'><img src=x onerror=alert(1)>"


def _payload() -> dict:
    row = {"ticker": "AAA", "name": "Test", "price": 50.0, "market_cap": 1e9,
           "sector": "Technology", "industry": "Software", "themes": ["Software"],
           "earnings_date": None, "period": "Q2/2026 vs Q2/2025",
           "metrics": _m(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=50.0, gross_margin=0.35,
                         roic=0.20, net_debt_ebitda=2.0, dist_52w_pct=0.90, rs_6m=0.05,
                         above_200d=True, pe=15.0, pb=2.0)}
    return build_payload([row], top_n=10)


def _hostile(field: str) -> dict:
    p = copy.deepcopy(_payload())
    c = p["candidates"][0]
    if field in ("themes", "index"):
        c[field] = [_EVIL + field]
    elif field == "gate":
        for g in c["gates"].values():
            g["score"] = _EVIL + "score"
            for r in g["rules"]:
                r.update(label=_EVIL + "label", threshold=_EVIL + "threshold",
                         unit="?", actual=_EVIL + "actual")
    else:
        c[field] = _EVIL + field
    return p


def _grid(p, tmp_path):
    html = render_fundamental_dashboard(p)
    return run_board_js(html, "console.log(JSON.stringify(_els['grid'].innerHTML));", tmp_path)


def test_card_fields_are_escaped(tmp_path):
    for field in ("ticker", "name", "sector", "themes", "index"):
        grid = _grid(_hostile(field), tmp_path)
        assert "&lt;img src=x" in grid, field
        assert "<img" not in grid, field


def test_gate_buttons_and_modal_are_escaped(tmp_path):
    p = _hostile("gate")
    t = p["candidates"][0]["ticker"]
    html = render_fundamental_dashboard(p)
    out = run_board_js(html, """
openGate({getAttribute: function(k){ return k === "data-ticker" ? %s : "quality"; }});
console.log(JSON.stringify({grid: _els['grid'].innerHTML, rules: _els['m-rules'].innerHTML}));
""" % json.dumps(t), tmp_path)
    assert "<img" not in out["grid"]
    assert "&lt;img src=x" in out["rules"]
    assert "<img" not in out["rules"]


def test_the_server_markup_holds_no_card_and_no_raw_value():
    """K10 (#383 R3): the cards are built in client JS from the payload; the server markup carries none."""
    import re
    for field in ("ticker", "name", "sector", "themes", "index", "gate"):
        html = render_fundamental_dashboard(_hostile(field))
        markup = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
        assert "<img" not in markup, field
        assert 'class="card"' not in markup, field


def test_a_comment_opener_or_placeholder_in_a_name_keeps_the_script_intact(tmp_path):
    """F3: ``<!--<script>`` in a value blanked the board; a value spelling
    ``__GATES__`` was substituted again."""
    from RevenueChaser.core.tests.board_js import PROBE, assert_scripts_intact
    p = copy.deepcopy(_payload())
    p["candidates"][0].update(name=PROBE, period=PROBE)
    for g in p["candidates"][0]["gates"].values():
        g["rules"][0]["label"] = PROBE
    assert_scripts_intact(render_fundamental_dashboard(p), tmp_path)


def test_the_board_filters_on_sector_then_industry(tmp_path, rawstore_tmp_db):
    """#266, #383 C1c: the shared filter kit narrows the grid by sector, then by the two-level path."""
    from RevenueChaser.core import rawstore
    p = _payload()
    base = p["candidates"][0]
    p["candidates"] = [dict(copy.deepcopy(base), ticker=t, sector=s, industry=i) for t, s, i in (
        ("AAA", "Healthcare", "Biotechnology"), ("BBB", "Healthcare", "Medical Devices"),
        ("CCC", "Technology", "Software - Application"))]
    rawstore.write_rows("equity_universe", [
        {"ticker": "AAA", "sector": "Healthcare", "industry": "Biotechnology"},
        {"ticker": "BBB", "sector": "Healthcare", "industry": "Medical Devices"},
        {"ticker": "CCC", "sector": "Technology", "industry": "Software - Application"}])
    html = render_fundamental_dashboard(p)
    out = run_board_js(html, """
function shown(){ var g = _els['grid'].innerHTML; return ['AAA', 'BBB', 'CCC'].filter(function(t){ return g.indexOf(t) >= 0; }); }
var log = {};
render(); log.all = shown();
RCF.set({gics: {key: 'Healthcare', label: 'Healthcare', level: 1}}); log.sector = shown();
RCF.set({gics: {key: 'Healthcare\\u203aBiotechnology', label: 'Biotechnology', level: 2}}); log.industry = shown();
RCF.set({gics: {key: 'Technology', label: 'Technology', level: 1}}); log.other_sector = shown();
console.log(JSON.stringify(log));
""", tmp_path)
    assert out == {
        "all": ["AAA", "BBB", "CCC"],
        "sector": ["AAA", "BBB"],
        "industry": ["AAA"],
        "other_sector": ["CCC"]}
