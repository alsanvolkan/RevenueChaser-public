"""Swing wave 2 (#384): SW-S1 graded 40/30/30 quality + direction, SW-S3 the C16 funnel, SW-S2 the card face.

SW-S1: the N× labels are gone; rank is #65's 40/30/30 score over the three method gates, RS ranks only (it no longer
gates Minervini), and the #65 direction condition (50-day MA ≥ 200-day MA and 3-month return ≥ 0) is required on
every card. SW-S3: the board's own funnel replaces the "data<252d · hygiene" line. SW-S2: the card face (R:R n/a, ATR %
and stop distance, the horizon copy) and the static gate text serialized once as ``GATE_SPEC``. Offline, synthetic
indicators, no network.
"""
from __future__ import annotations

import json

from RevenueChaser.core import rawstore
from RevenueChaser.module_swing_potential import config, freeze, scores
from RevenueChaser.module_swing_potential.pipeline import build_payload
from RevenueChaser.module_swing_potential.ranking import quality_403030
from RevenueChaser.module_swing_potential.report import render_swing_dashboard
from RevenueChaser.module_swing_potential.screeners import (
    evaluate_direction,
    evaluate_minervini,
)


def _ind(**over) -> dict:
    """Every indicator a candidate needs: all three method gates pass and the direction gate passes."""
    ind = {
        "price": 100.0, "sma50": 95.0, "sma150": 90.0, "sma200": 80.0,
        "sma200_prev_1m": 78.0, "sma150_prev_1m": 88.0, "ema10": 96.0,
        "ema20": 92.0, "ema10_prev": 90.0, "ema20_prev": 88.0,
        "above_52w_low_pct": 1.5, "within_52w_high_pct": 0.99, "vol_ratio": 2.0,
        "rs_vs_spy_3m": 0.05, "rs_vs_spy_12m": 0.1, "mom_12_1": 0.9,
        "consistency_6m": 0.9, "move_max_1_3m": 0.35, "adr_pct": 0.04,
        "atr14": 2.0, "change_pct": 0.01, "change_1w": 0.02, "change_1m": 0.05,
        "ret_3m": 0.10,
    }
    ind.update(over)
    return ind


def _row(ticker: str, ind: dict, **over) -> dict:
    row = {"ticker": ticker, "name": f"{ticker} Inc", "market_cap": 1e9, "sector": "Technology",
           "industry": "Software - Application", "themes": [], "earnings_date": None, "ind": ind,
           "ohlc_stale": False}
    row.update(over)
    return row


# ── a) the 40/30/30 quality ──────────────────────────────────────────────────

def test_quality_403030_known_cases():
    assert quality_403030(8, 5, 6) == 100
    assert quality_403030(4, 0, 3) == 35
    assert quality_403030(0, 0, 0) == 0
    assert quality_403030(1, 1, 1) == 16          # round(5 + 6 + 5)


# ── b) RS ranks only: Minervini passes on the 7 price rules ──────────────────

def test_minervini_passes_without_the_rs_rating_but_still_counts_it():
    g = evaluate_minervini(_ind(), rs_rating=10.0)
    assert g["passed"] is True
    rs = next(r for r in g["rules"] if r["id"] == "rs_ge_70")
    assert rs["passed"] is False
    assert g["score"] == "7/8"                    # RS is shown and counted, it does not gate
    assert quality_403030(7, 0, 0) == 35


# ── c) the direction condition is required ───────────────────────────────────

def test_direction_gate_passes_only_when_both_rules_are_known_passes():
    assert evaluate_direction(_ind())["passed"] is True
    assert evaluate_direction(_ind(sma50=70.0))["passed"] is False
    assert evaluate_direction(_ind(ret_3m=-0.01))["passed"] is False
    unknown = evaluate_direction(_ind(ret_3m=None))
    assert unknown["passed"] is False
    assert next(r for r in unknown["rules"] if r["id"] == "ret_3m_ge_0")["passed"] is None


def test_a_name_that_passes_no_direction_is_not_a_candidate():
    for over in ({"ret_3m": -0.01}, {"ret_3m": None}):
        p = build_payload([_row("AAA", _ind(**over))], top_n=10)
        assert [c["ticker"] for c in p["candidates"]] == [], over
        assert p["passing"]["direction"] == 0


def test_the_three_methods_all_passing_but_direction_broken_is_not_a_candidate():
    """``sma50 < sma200`` fails the direction gate even when the method gates pass (an imposed break)."""
    ind = _ind()
    ind["sma50"] = 70.0                            # below sma200; the method gates are read from the raw rules
    p = build_payload([_row("AAA", ind)], top_n=10)
    assert [c["ticker"] for c in p["candidates"]] == []


# ── d) sort: quality, then momentum, then ticker ─────────────────────────────

def test_candidates_sort_by_quality_then_momentum_then_ticker():
    rows = [
        # AAA has the top quality but the LOWEST momentum, so a momentum-first sort would put it last.
        _row("AAA", _ind(mom_12_1=0.01, consistency_6m=0.05)),
        _row("BBB", _ind(above_52w_low_pct=1.1, mom_12_1=0.1, consistency_6m=0.2)),
        _row("CCC", _ind(above_52w_low_pct=1.1, mom_12_1=0.6, consistency_6m=0.8)),
    ]
    p = build_payload(rows, top_n=10)
    assert [c["ticker"] for c in p["candidates"]] == ["AAA", "CCC", "BBB"]
    assert [c["quality"] for c in p["candidates"]] == [95, 90, 90]         # the RS rule counts (AAA 7/8)
    assert p["candidates"][1]["momentum"] > p["candidates"][2]["momentum"]


# ── e) methods, bucket, and the freeze bucket ────────────────────────────────

def test_a_name_passing_two_methods_carries_the_bucket():
    p = build_payload([_row("AAA", _ind(within_52w_high_pct=0.90))], top_n=10)
    c = p["candidates"][0]
    assert c["methods"] == ["minervini", "qullamaggie"]
    assert c["bucket"] == "minervini+qullamaggie"
    assert c["direction_ok"] is True
    assert "multiplier" not in c and "direction" not in c
    assert freeze.picks([c])[0]["bucket"] == "minervini+qullamaggie"


# ── f) the stored score and gate rows ────────────────────────────────────────

def test_score_rows_carry_the_new_quality_and_direction_columns():
    rows = scores.score_rows([_row("AAA", _ind())])
    row = rows[0]
    assert {"direction_ok", "m_pass", "w_pass", "q_pass", "quality"} <= set(row)
    assert (row["direction_ok"], row["m_pass"], row["w_pass"], row["q_pass"]) == (1, 1, 1, 1)
    assert row["quality"] == 100
    assert row["multiplier"] == 3                  # the number of methods passed stays in the row


def test_gate_rows_are_21_per_name_including_direction():
    rows = scores.gate_rows([_row("AAA", _ind()), _row("BBB", _ind())])
    assert len(rows) == 2 * 21
    assert {r["gate"] for r in rows} == {"minervini", "weinstein", "qullamaggie", "direction"}


def test_the_four_new_columns_are_in_the_swing_scores_table(rawstore_tmp_db):
    with rawstore.connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(swing_scores)").fetchall()}
    assert {"direction_ok", "m_pass", "w_pass", "q_pass"} <= cols
    scores.persist([_row("AAA", _ind())])
    got = rawstore.query("SELECT direction_ok, m_pass, w_pass, q_pass, quality FROM swing_scores")
    assert got == [{"direction_ok": 1, "m_pass": 1, "w_pass": 1, "q_pass": 1, "quality": 100}]


# ── g) the page: three method checkboxes, Minervini checked, no N× ───────────

def test_the_page_has_the_method_toggles_and_no_multiplier_tabs():
    html = render_swing_dashboard(build_payload([_row("AAA", _ind())], top_n=10))
    assert html.count("data-method=") == 3
    assert 'data-method="minervini" checked' in html
    assert "data-pt" not in html and 'class="mult"' not in html and "setPt(" not in html


# ── h) an old payload renders without the toggles ────────────────────────────

def _old_payload() -> dict:
    gate = {"name": "Minervini", "passed": True, "score": "3/3",
            "rules": [{"label": "rule", "passed": True, "threshold": "> 1", "unit": "ratio", "actual": 1.2}]}
    p = build_payload([], run_stats={})
    p["is_fixture"] = False
    p["selection_version"] = "3"
    p["candidates"] = [{
        "ticker": "AAA", "name": "Alpha", "price": 10.0, "change_1w": 0.01, "change_1m": 0.01,
        "market_cap": 1e9, "sector": "Tech", "industry": "Soft", "themes": [], "index": [],
        "earnings_date": None, "multiplier": 3, "direction": "LONG", "quality": 80, "rs_rating": 90,
        "entry": 10.0, "target": 12.0, "stop": 9.0, "rr": 2.0, "atr": 0.5,
        "gates": {k: gate for k in ("minervini", "weinstein", "qullamaggie")}}]
    return p


def test_an_old_payload_renders_without_the_toggles_and_says_so():
    html = render_swing_dashboard(_old_payload())
    assert "data-method=" not in html
    assert "Scored before v4" in html


# ── i) every new string is escaped ───────────────────────────────────────────

def test_a_hostile_candidate_is_escaped(tmp_path):
    from RevenueChaser.core.tests.board_js import run_board_js
    evil = "\"'><img src=x onerror=alert(1)>"
    gate = {"passed": True, "score": "1/1", "rules": [{"id": "r", "passed": True, "actual": 1.2}]}
    p = build_payload([], run_stats={})
    p["is_fixture"] = False
    p["candidates"] = [{
        "ticker": evil, "name": evil, "sector": evil, "price": 10.0, "change_1w": 0.01, "change_1m": 0.01,
        "market_cap": 1e9, "themes": [evil], "index": [], "earnings_date": None,
        "quality": 80, "rs_rating": 90, "entry": 10.0, "target": 12.0, "stop": 9.0, "rr": 2.0, "atr": 0.5,
        "methods": ["minervini"], "bucket": "minervini", "direction_ok": True,
        "gates": {k: gate for k in ("minervini", "weinstein", "qullamaggie", "direction")}}]
    grid = run_board_js(render_swing_dashboard(p),
                        "console.log(JSON.stringify(_els['grid'].innerHTML));", tmp_path)
    assert "&lt;img src=x" in grid and "<img" not in grid


# ══ SW-S3: the C16 funnel panel ══════════════════════════════════════════════

def _payload_with_funnel() -> dict:
    from RevenueChaser.core import funnel
    p = build_payload([_row("AAA", _ind())], top_n=10)
    p["funnel_steps"] = [
        funnel.step("universe", "Universe, all stored names", 3, "start"),
        funnel.step("no_bars", "No stored daily price bars (D01)", 2, "drop", by_design=False,
                    tickers=["DROPPED1", "DROPPED2"]),
        funnel.step("candidates", "Candidates (a card on the board)", 1, "result"),
    ]
    return p


def test_the_funnel_panel_replaces_the_eliminated_line():
    html = render_swing_dashboard(_payload_with_funnel())
    assert 'id="swing-funnel"' in html
    assert "DROPPED1" in html
    assert "data&lt;252d" not in html and "data<252d" not in html


def test_a_payload_without_a_funnel_renders_no_panel():
    html = render_swing_dashboard(_old_payload())
    assert 'id="swing-funnel"' not in html


# ══ SW-S2: the card face ═════════════════════════════════════════════════════

def _card_candidate(**over) -> dict:
    c = {
        "ticker": "AAA", "name": "Alpha", "sector": "Tech", "price": 10.0, "change_1w": 0.01,
        "change_1m": 0.01, "market_cap": 1e9, "themes": [], "index": [], "earnings_date": None,
        "quality": 80, "rs_rating": 90, "entry": 10.0, "target": 12.0, "stop": 9.0, "rr": 2.0, "atr": 0.5,
        "methods": ["minervini"], "bucket": "minervini", "direction_ok": True,
        "gates": {k: {"passed": True, "score": "1/1",
                      "rules": [{"id": "r", "passed": True, "actual": 1.2}]}
                  for k in ("minervini", "weinstein", "qullamaggie", "direction")},
    }
    c.update(over)
    return c


def _card_html(candidate: dict, tmp_path) -> str:
    from RevenueChaser.core.tests.board_js import run_board_js
    p = build_payload([], run_stats={})
    p["is_fixture"] = False
    p["candidates"] = [candidate]
    return run_board_js(render_swing_dashboard(p),
                        "console.log(JSON.stringify(_els['grid'].innerHTML));", tmp_path)


def test_a_null_rr_shows_na_and_no_slider(tmp_path):
    grid = _card_html(_card_candidate(rr=None), tmp_path)
    assert "R:R <b>n/a</b>" in grid
    assert 'type="range"' not in grid


def test_the_card_shows_atr_percent_and_stop_distance(tmp_path):
    grid = _card_html(_card_candidate(), tmp_path)
    assert "ATR 5.0%" in grid and "Stop 10.0%" in grid


def test_the_board_states_the_horizon_and_that_momentum_is_not_a_probability():
    html = render_swing_dashboard(build_payload([_row("AAA", _ind())], top_n=10))
    assert "1–3 month" in html
    assert "not a probability" in html


def test_the_candidate_gates_carry_no_static_text():
    p = build_payload([_row("AAA", _ind())], top_n=10)
    for gate in p["candidates"][0]["gates"].values():
        for rule in gate["rules"]:
            assert set(rule) == {"id", "passed", "actual"}
    assert "var GATE_SPEC" in render_swing_dashboard(p)


def test_gate_spec_holds_every_rule_once(tmp_path):
    from RevenueChaser.core.tests.board_js import run_board_js
    p = build_payload([_row("AAA", _ind())], top_n=10)
    out = run_board_js(render_swing_dashboard(p), """
var n = 0, ids = [];
for(var k in GATE_SPEC.rules){ n++; ids.push(k); }
console.log(JSON.stringify({n: n, dir: GATE_SPEC.gates.direction, rs: GATE_SPEC.rules.rs_ge_70.unit}));
""", tmp_path)
    assert out["n"] == 21
    assert out["dir"] == "Direction"
    assert out["rs"] == "rating"


def test_an_old_full_label_payload_still_renders_its_labels(tmp_path):
    from RevenueChaser.core.tests.board_js import run_board_js
    out = run_board_js(render_swing_dashboard(_old_payload()), """
openGate({getAttribute: function(k){ return k === "data-ticker" ? "AAA" : "minervini"; }});
console.log(JSON.stringify(_els['m-rules'].innerHTML));
""", tmp_path)
    assert "rule" in out and "&gt; 1" in out


# ── review (#384): the funnel names the direction rule ────────────────────────

def test_a_method_passer_that_fails_direction_is_its_own_funnel_step():
    """A name that passes a method but not the direction rule is not "passed none of the three screens"."""
    from types import SimpleNamespace

    from RevenueChaser.core import screen_common as sc
    stats: dict = {"regime": "RISK-ON"}       # a non-empty dict, as run_screen passes (an empty one is replaced)
    p = build_payload([_row("AAA", _ind()), _row("BBB", _ind(ret_3m=-0.05))], top_n=10, run_stats=stats)
    assert [c["ticker"] for c in p["candidates"]] == ["AAA"]
    assert stats["screen_drops"][0][0] == "direction_failed" and stats["screen_drops"][0][2] == ["BBB"]
    pf = SimpleNamespace(drops={}, kept=[], dropped=0)
    steps = sc.funnel_steps(universe=3, excluded_types=0, prefiltered=pf, dropped={}, scored=3, passed=1, shown=1,
                            cfg=config, screen_drops=stats["screen_drops"])
    by_key = {s["key"]: s for s in steps}
    assert by_key["direction_failed"]["count"] == 1 and by_key["direction_failed"]["tickers"] == ["BBB"]
    assert by_key["passed_no_screen"]["count"] == 1          # the third scored name passed no method
    assert by_key["candidates"]["count"] == 1
    assert "beyond_top_n" not in by_key
