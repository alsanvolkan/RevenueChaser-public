"""Swing board: provider strings never reach markup raw (ADR-X1, #144).

The cards and the gate modal are built in client JS, so these run the board's
real script under node (``core/tests/board_js.py``) and read ``innerHTML`` back.
"""
from __future__ import annotations

import copy
import json
import re

from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_swing_potential.pipeline import demo_payload
from RevenueChaser.module_swing_potential.report import render_swing_dashboard

_EVIL = "\"'><img src=x onerror=alert(1)>"


def _gate(name: str) -> dict:
    return {"name": name, "passed": True, "score": "3/3",
            "rules": [{"label": "rule", "passed": True, "threshold": "> 1",
                       "unit": "ratio", "actual": 1.2}]}


def _payload() -> dict:
    p = demo_payload()
    p["is_fixture"] = False
    p["candidates"] = [{
        "ticker": "AAA", "name": "Name", "price": 10.0, "change_pct": 1.0,
        "change_1w": 0.01, "change_1m": 0.01, "market_cap": 1e9,
        "sector": "Tech", "industry": "Soft", "themes": ["Theme"],
        "index": ["S&P 500"], "earnings_date": None, "multiplier": 3,
        "direction": "LONG", "quality": 80, "rs_rating": 90,
        "ma_alignment": "stacked", "entry": 10.0, "target": 12.0, "stop": 9.0,
        "rr": 2.0, "atr": 0.5,
        "gates": {"minervini": _gate("Minervini"), "weinstein": _gate("Weinstein"),
                  "qullamaggie": _gate("Qullamaggie")},
        "flow": {"status": "not_scanned"}}]
    return p


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
    html = render_swing_dashboard(p)
    return run_board_js(html, "console.log(JSON.stringify(_els['grid'].innerHTML));", tmp_path)


def test_card_fields_are_escaped(tmp_path):
    for field in ("ticker", "name", "sector", "themes", "index", "gate"):
        grid = _grid(_hostile(field), tmp_path)
        assert "&lt;img src=x" in grid, field
        assert "<img" not in grid, field


def test_gate_modal_is_escaped(tmp_path):
    p = _hostile("gate")
    html = render_swing_dashboard(p)
    out = run_board_js(html, """
openGate({getAttribute: function(k){ return k === "data-ticker" ? "AAA" : "weinstein"; }});
console.log(JSON.stringify(_els['m-rules'].innerHTML));
""", tmp_path)
    assert "&lt;img src=x" in out
    assert "<img" not in out


def test_rr_slider_reads_the_ticker_from_data_attribute(tmp_path):
    """Trap 3: html-escaping a value inside a JS string in an attribute does not
    help, because the parser decodes ``&#x27;`` before the handler runs."""
    p = _hostile("ticker")
    t = p["candidates"][0]["ticker"]
    html = render_swing_dashboard(p)
    grid = _grid(p, tmp_path)
    assert "setRR('" not in grid
    assert 'oninput="setRR(this.dataset.ticker, this.value)"' in grid
    assert "setRR(" not in re.sub(r"<script>.*?</script>", "", html, flags=re.S)   # cards are client-built (K10)
    # setRR finds the card by the decoded attribute value.
    out = run_board_js(html, """
setRR(%s, "3");
console.log(JSON.stringify(_els["target-" + %s].textContent));
""" % (json.dumps(t), json.dumps(t)), tmp_path)
    assert out == "Target $13.00"


def test_the_server_markup_holds_no_card_and_no_raw_value():
    """K10 (#383 R3): the cards are built in client JS from the payload; the server markup carries none."""
    for field in ("ticker", "name", "sector", "themes", "index", "gate"):
        html = render_swing_dashboard(_hostile(field))
        markup = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
        assert "<img" not in markup, field
        assert 'class="card"' not in markup, field


def test_a_comment_opener_or_placeholder_in_a_name_keeps_the_script_intact(tmp_path):
    """F3: ``<!--<script>`` in a value blanked the board; a value spelling
    ``__GATES__`` was substituted again."""
    from RevenueChaser.core.tests.board_js import PROBE, assert_scripts_intact
    p = copy.deepcopy(_payload())
    p["candidates"][0]["name"] = PROBE
    p["candidates"][0]["gates"]["minervini"]["rules"][0]["label"] = PROBE
    assert_scripts_intact(render_swing_dashboard(p), tmp_path)


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
    html = render_swing_dashboard(p)
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
