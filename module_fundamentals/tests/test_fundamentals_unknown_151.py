"""#151 (27-7, decision 8-A) on the Fundamentals board: a missing metric makes a rule UNKNOWN, not a fail.

A gate that needs every rule (Quality 3/3, Momentum 3/3) passes only when every rule is known and passed; the
score says "k/3 of n known". The gate rows store NULL for an unknown rule. Synthetic metrics, no network.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_fundamentals import scores, screeners
from RevenueChaser.module_fundamentals.tests.test_fundamental import _m


def _rule(gate, rule_id):
    return next(r for r in gate["rules"] if r["id"] == rule_id)


def test_quality_with_a_missing_margin_is_two_of_two_known_and_does_not_pass():
    g = screeners.evaluate_quality(_m(rev_growth_ttm=0.25, gross_margin=None, net_debt_ebitda=2.0))
    assert _rule(g, "gross_margin")["passed"] is None
    assert g["score"] == "2/3 of 2 known"
    assert g["passed"] is False


def test_quality_known_fails_keep_the_plain_score():
    g = screeners.evaluate_quality(_m(rev_growth_ttm=0.10, roic=0.10, gross_margin=0.20, net_debt_ebitda=4.0))
    assert g["score"] == "0/3" and g["passed"] is False
    assert [r["passed"] for r in g["rules"]] == [False, False, False]


@pytest.mark.parametrize("growth, roic, expected", [
    (0.25, None, True),      # growth alone clears it: the missing ROIC cannot change the answer
    (None, 0.20, True),
    (0.10, None, None),      # growth fails but ROIC could still pass: unknown
    (None, None, None),
    (0.10, 0.10, False),
])
def test_growth_or_roic_is_unknown_only_when_the_missing_half_could_decide(growth, roic, expected):
    g = screeners.evaluate_quality(_m(rev_growth_ttm=growth, roic=roic, gross_margin=0.4, net_debt_ebitda=1.0))
    assert _rule(g, "growth_or_roic")["passed"] is expected


def test_momentum_with_a_missing_ma_is_unknown_and_does_not_pass():
    g = screeners.evaluate_momentum(_m(dist_52w_pct=0.9, rs_6m=0.1, above_200d=None))
    assert _rule(g, "above_200d")["passed"] is None
    assert g["score"] == "2/3 of 2 known" and g["passed"] is False


def test_momentum_all_known_and_passing_still_passes():
    g = screeners.evaluate_momentum(_m(dist_52w_pct=0.9, rs_6m=0.1, above_200d=True))
    assert g["passed"] is True and g["score"] == "3/3"


def test_rule_of_40_rows_are_unknown_when_the_value_is_missing():
    g = screeners.evaluate_rule_of_40(_m(rev_growth_ttm=None, fcf_margin=0.2, ro40=None))
    assert [r["passed"] for r in g["rules"]] == [None]
    assert g["passed"] is False and g["score"] == "N/A"


_KNOWN = dict(rev_growth_ttm=0.25, roic=0.2, gross_margin=0.4, net_debt_ebitda=1.0,
              dist_52w_pct=0.9, rs_6m=0.1, above_200d=True)


@pytest.mark.parametrize("metric, evaluate, rule_id", [
    ("gross_margin", screeners.evaluate_quality, "gross_margin"),
    ("net_debt_ebitda", screeners.evaluate_quality, "net_debt_ebitda"),
    ("dist_52w_pct", screeners.evaluate_momentum, "near_52w_high"),
    ("rs_6m", screeners.evaluate_momentum, "rs_6m"),
    ("above_200d", screeners.evaluate_momentum, "above_200d"),
])
def test_each_missing_metric_makes_exactly_its_rule_unknown(metric, evaluate, rule_id):
    g = evaluate(_m(**{**_KNOWN, metric: None}))
    assert [r["id"] for r in g["rules"] if r["passed"] is None] == [rule_id]
    assert g["passed"] is False and (g["known"], g["total"]) == (2, 3)


def test_gate_carries_known_and_total():
    g = screeners.evaluate_quality(_m(rev_growth_ttm=0.25, gross_margin=None, net_debt_ebitda=2.0))
    assert (g["known"], g["total"]) == (2, 3)


def _result(**metrics):
    return {"ticker": "AAA", "period": "FY2024 vs FY2023", "metrics": _m(**metrics)}


def test_gate_rows_store_null_for_an_unknown_rule_and_a_flag_for_a_known_one():
    rows = scores.gate_rows([_result(rev_growth_ttm=0.25, gross_margin=None, net_debt_ebitda=2.0,
                                     dist_52w_pct=0.9, rs_6m=0.1, above_200d=True)])
    by = {(r["gate"], r["rule_id"]): r["passed"] for r in rows}
    assert by[("quality", "gross_margin")] is None
    assert by[("quality", "net_debt_ebitda")] == 1
    assert by[("momentum", "above_200d")] == 1
    assert by[("rule_of_40", "ro40")] is None   # no Rule of 40 value in this result


def test_the_score_row_counts_only_known_passes():
    row = scores.score_rows([_result(rev_growth_ttm=0.25, gross_margin=None, net_debt_ebitda=2.0)])[0]
    assert row["quality_pass"] == 2
    assert row["momentum_pass"] == 0
    assert row["multiplier"] == 0


def test_the_gate_rows_persist_through_the_real_table(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    scores.persist([_result(rev_growth_ttm=0.25, gross_margin=None, net_debt_ebitda=2.0)])
    got = rawstore.query("SELECT passed FROM fundamentals_gates WHERE ticker='AAA' AND rule_id='gross_margin'")
    assert got == [{"passed": None}]


# ── the board ───────────────────────────────────────────────────────────────

def test_the_gate_modal_marks_an_unknown_rule_as_unknown_not_failed(tmp_path):
    from RevenueChaser.core.tests.board_js import run_board_js
    from RevenueChaser.module_fundamentals.pipeline import build_payload
    from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
    row = {"ticker": "AAA", "name": "A", "price": 50.0, "market_cap": 1e9, "sector": "Tech", "industry": "Soft",
           "themes": ["Software"], "earnings_date": None, "period": "FY2024 vs FY2023",
           "metrics": _m(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=50.0, gross_margin=None, roic=0.20,
                         net_debt_ebitda=2.0, dist_52w_pct=0.9, rs_6m=0.05, above_200d=True)}
    p = build_payload([row], top_n=10)
    assert p["candidates"][0]["gates"]["quality"]["score"] == "2/3 of 2 known"
    out = run_board_js(render_fundamental_dashboard(p), """
openGate({getAttribute: function(k){ return k === "data-ticker" ? "AAA" : "quality"; }});
console.log(JSON.stringify({sub: _els['m-sub'].textContent, rules: _els['m-rules'].innerHTML}));
""", tmp_path)
    assert "2/3 of 2 known" in out["sub"] and "(FAIL)" in out["sub"]
    assert out["rules"].count("m-unk") == 1 and out["rules"].count("m-ok") == 2 and "m-no" not in out["rules"]
    assert out["rules"].count(">?<") == 1
