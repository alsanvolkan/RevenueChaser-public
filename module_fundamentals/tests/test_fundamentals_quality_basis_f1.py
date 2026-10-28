"""Review F1 (28 Oct): Quality mixes two time bases and the board now says so. Its growth half is the trailing
twelve months (the card's Period), while ROIC, gross margin and Net Debt / EBITDA are read from one fiscal year,
the anchor year. ``anchor_period_end`` was in the payload but never shown, so a reader could not tell which year
those three figures came from. The card's Quality cell and the Quality rules window now name it."""
from __future__ import annotations

import json

from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_fundamentals.pipeline import build_payload
from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
from RevenueChaser.module_fundamentals.tests.test_fundamental import _m

_EVIL = "<img src=x onerror=alert(1)>"


def _payload(anchor) -> dict:
    row = {"ticker": "AAA", "name": "Test", "price": 50.0, "market_cap": 1e9,
           "sector": "Technology", "industry": "Software", "themes": ["Software"],
           "earnings_date": None, "period": "Q2/2026 vs Q2/2025", "anchor_period_end": anchor,
           "metrics": _m(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=50.0, gross_margin=0.35,
                         roic=0.20, net_debt_ebitda=2.0, dist_52w_pct=0.90, rs_6m=0.05,
                         above_200d=True, pe=15.0, pb=2.0)}
    return build_payload([row], top_n=10)


def _run(p, tmp_path, gate="quality"):
    return run_board_js(render_fundamental_dashboard(p), """
openGate({getAttribute: function(k){ return k === "data-ticker" ? "AAA" : %s; }});
console.log(JSON.stringify({grid: _els['grid'].innerHTML, sub: _els['m-sub'].textContent}));
""" % json.dumps(gate), tmp_path)


def test_the_card_and_the_quality_window_name_the_fiscal_year(tmp_path):
    out = _run(_payload("2025-12-31"), tmp_path)
    assert "FY to 2025-12-31" in out["grid"]
    assert "growth: TTM Q2/2026 vs Q2/2025" in out["sub"]
    assert "ROIC, gross margin, net debt: fiscal year to 2025-12-31" in out["sub"]


def test_other_gates_keep_their_window(tmp_path):
    out = _run(_payload("2025-12-31"), tmp_path, gate="momentum")
    assert "fiscal year" not in out["sub"]


def test_no_anchor_says_nothing_and_a_hostile_one_is_escaped(tmp_path):
    out = _run(_payload(None), tmp_path)
    assert "FY to" not in out["grid"] and "fiscal year" not in out["sub"]
    out = _run(_payload(_EVIL), tmp_path)
    assert "<img" not in out["grid"] and "&lt;img" in out["grid"]
