"""#151 (27-7, decision 8-A) on the Swing board: a missing indicator makes a rule UNKNOWN, not a fail.

A gate that needs every rule (Minervini 8/8, Weinstein 5/5) passes only when every rule is known and passed.
Qullamaggie (5 of 6) passes on five KNOWN passes: an unknown rule is never counted as a pass. The gate rows store
NULL for an unknown rule. Synthetic indicators, no network.
"""
from __future__ import annotations

from RevenueChaser.module_swing_potential import scores
from RevenueChaser.module_swing_potential.screeners import (
    evaluate_minervini, evaluate_qullamaggie, evaluate_weinstein)
from RevenueChaser.module_swing_potential.tests.test_gates import _PASS


def _rule(gate, rule_id):
    return next(r for r in gate["rules"] if r["id"] == rule_id)


def _without(*keys):
    return {k: v for k, v in _PASS.items() if k not in keys}


def test_a_full_ind_is_unchanged():
    for g, n in ((evaluate_minervini(_PASS, 85.0), 8), (evaluate_weinstein(_PASS), 5),
                 (evaluate_qullamaggie(_PASS), 6)):
        assert g["passed"] is True and (g["known"], g["total"]) == (n, n)
        assert "known" not in g["score"]


def test_minervini_without_the_200_day_ma_has_three_unknown_rules_and_does_not_pass():
    g = evaluate_minervini(_without("sma200"), 85.0)
    assert [r["id"] for r in g["rules"] if r["passed"] is None] == [
        "price_above_150_200", "ma150_above_ma200", "ma200_rising"]
    assert g["score"] == "5/8 of 5 known" and g["passed"] is False
    assert (g["known"], g["total"]) == (5, 8)
    assert _rule(g, "ma200_rising")["actual"] is None          # no difference is invented from a missing MA


def test_minervini_without_an_rs_rating_passes_on_its_seven_price_rules():
    """#65 / #384 SW-S1: RS no longer decides ``passed``; the seven price rules do, and RS still counts."""
    g = evaluate_minervini(_PASS, None)
    assert _rule(g, "rs_ge_70")["passed"] is None
    assert g["score"] == "7/8 of 7 known" and g["passed"] is True


def test_a_known_fail_is_still_a_fail():
    g = evaluate_minervini(_PASS, 50.0)
    assert _rule(g, "rs_ge_70")["passed"] is False
    assert g["score"] == "7/8" and (g["known"], g["total"]) == (8, 8)


def test_weinstein_without_volume_is_four_of_four_known_and_does_not_pass():
    g = evaluate_weinstein(_without("vol_ratio"))
    assert _rule(g, "volume_confirmation")["passed"] is None
    assert g["score"] == "4/5 of 4 known" and g["passed"] is False


def test_qullamaggie_passes_on_five_known_passes_with_one_unknown():
    g = evaluate_qullamaggie(_without("adr_pct"))
    assert _rule(g, "adr_ge_3")["passed"] is None
    assert g["score"] == "5/6 of 5 known" and g["passed"] is True


def test_qullamaggie_does_not_count_an_unknown_as_a_pass():
    g = evaluate_qullamaggie(_without("ema10"))     # three rules need ema10
    assert [r["passed"] for r in g["rules"]].count(None) == 3
    assert g["score"] == "3/6 of 3 known" and g["passed"] is False


def test_qullamaggie_two_unknown_cannot_reach_five():
    g = evaluate_qullamaggie(_without("adr_pct", "move_max_1_3m"))
    assert g["score"] == "4/6 of 4 known" and g["passed"] is False


def test_a_zero_value_is_known():
    g = evaluate_weinstein({**_PASS, "rs_vs_spy_3m": 0.0})
    assert _rule(g, "rs_3m_gt_spy")["passed"] is False


def test_gate_rows_store_null_for_an_unknown_rule():
    result = {"ticker": "AAA", "ind": _without("sma200")}
    rows = scores.gate_rows([result])
    by = {(r["gate"], r["rule_id"]): r["passed"] for r in rows}
    assert by[("minervini", "ma200_rising")] is None
    assert by[("minervini", "price_above_50")] == 1
    assert by[("weinstein", "price_above_30w")] == 1


def test_the_multiplier_counts_only_passed_gates():
    row = scores.score_rows([{"ticker": "AAA", "ind": _without("sma200")}])[0]
    assert row["multiplier"] == 2          # Weinstein and Qullamaggie; Minervini is uncertain


def test_the_gate_rows_persist_through_the_real_table(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    scores.persist([{"ticker": "AAA", "ind": _without("sma200")}])
    got = rawstore.query("SELECT passed FROM swing_gates WHERE ticker='AAA' AND rule_id='ma200_rising'")
    assert got == [{"passed": None}]


# ── the board ───────────────────────────────────────────────────────────────

def _page(gate_overrides):
    from RevenueChaser.module_swing_potential.pipeline import build_payload
    from RevenueChaser.module_swing_potential.report import render_swing_dashboard
    ind = {**_without("sma150", "vol_ratio"), "ticker": "AAA"}
    p = build_payload([{"ticker": "AAA", "name": "A", "market_cap": 1e9, "sector": "Tech", "industry": "Soft",
                        "themes": [], "earnings_date": None, "ind": ind, "ohlc_stale": False}], top_n=10)
    p["is_fixture"] = False
    return render_swing_dashboard(p), p


def test_the_gate_button_and_modal_say_how_many_rules_were_known(tmp_path):
    from RevenueChaser.core.tests.board_js import run_board_js
    html, p = _page(None)
    gates = p["candidates"][0]["gates"]
    assert gates["minervini"]["score"] == "5/8 of 5 known"
    out = run_board_js(html, """
openGate({getAttribute: function(k){ return k === "data-ticker" ? "AAA" : "minervini"; }});
console.log(JSON.stringify({sub: _els['m-sub'].textContent, rules: _els['m-rules'].innerHTML}));
""", tmp_path)
    assert "5/8 of 5 known" in out["sub"] and "(FAIL)" in out["sub"]
    assert out["rules"].count("m-unk") == 3 and out["rules"].count("m-ok") == 5
    assert out["rules"].count(">?<") == 3
    assert "m-no" not in out["rules"]          # an unknown rule is not drawn as a failed one


def test_the_card_button_carries_the_score_text(tmp_path):
    """The cards are client-built from the payload (K10, #383 R3)."""
    from RevenueChaser.core.tests.board_js import run_board_js
    html, _ = _page(None)
    grid = run_board_js(html, """
METHODS.minervini = false; METHODS.qullamaggie = true; render();
console.log(JSON.stringify(_els['grid'].innerHTML));
""", tmp_path)
    assert "M 5/8 of 5 known" in grid
