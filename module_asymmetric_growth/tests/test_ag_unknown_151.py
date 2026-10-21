"""#151 (27-7, decision 8-A) on the AG board: a missing input makes a sub-criterion UNKNOWN, not a fail.

Unknowns are left out of the passed count and the signal says how many sub-criteria were known. The client-side
rescoring follows the same rule. Synthetic data, no network.
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.module_asymmetric_growth import engine
from RevenueChaser.module_asymmetric_growth.engine import score_ticker
from RevenueChaser.module_asymmetric_growth.report import board_payload, render_asymmetric_growth_hunter_html
from RevenueChaser.core.tests.board_js import run_board_js

_FULL = dict(revenue=130.0, gross_profit=78.0, operating_cash_flow=60.0, capex=10.0, total_debt=10.0, cash=100.0,
             total_assets=1000.0, rd_expense=10.0, diluted_shares=100.0)
_TECH = {"price": 100.0, "dist_52w_pct": 0.05, "vol_ratio": 1.6, "above_sma50": True}


def _card(annual, tech=_TECH):
    fin = {"info": {"country": "United States", "exchange": "NMS", "longName": "X"}, "annual": annual,
           "market_cap": 5e9, "source": "sec_edgar"}
    return score_ticker("UNKN", fin, tech, spine={})


def _years():
    return [dict(fiscal_year=2021, **{**_FULL, "revenue": 100.0, "gross_profit": 50.0}),
            dict(fiscal_year=2022, **{**_FULL, "revenue": 110.0, "gross_profit": 60.0}),
            dict(fiscal_year=2023, **_FULL)]


def _sig(card, key):
    return next(s for s in card["signals"] if s["key"] == key)


def _sub(card, param):
    return next(s for sig in card["signals"] for s in sig["subs"] if s["param"] == param)


def test_a_fully_known_card_is_unchanged():
    card = _card(_years())
    assert all(s["known"] == 3 and s["total"] == 3 for s in card["signals"])
    assert card["total_points"] == 8   # growth 3/3 (+18%, accelerating, gm 60%), survival 3/3, momentum 3/3 -> see below
    assert all(isinstance(sub["passed"], bool) for sig in card["signals"] for sub in sig["subs"])


def test_missing_revenue_history_leaves_growth_unknown_not_failed():
    card = _card([dict(fiscal_year=2023, **_FULL)])
    assert _sub(card, "rev_growth")["passed"] is None
    assert _sub(card, "accelerating")["passed"] is None
    assert _sub(card, "gross_margin")["passed"] is True
    growth = _sig(card, "growth")
    assert (growth["passed"], growth["known"], growth["total"]) == (1, 1, 3)


def test_missing_technicals_leave_momentum_unknown():
    card = _card(_years(), {"price": 100.0, "dist_52w_pct": None, "vol_ratio": None, "above_sma50": None})
    momentum = _sig(card, "momentum")
    assert (momentum["passed"], momentum["known"]) == (0, 0)
    assert [s["passed"] for s in momentum["subs"]] == [None, None, None]


def test_a_known_fail_is_still_a_fail():
    card = _card(_years(), {"price": 100.0, "dist_52w_pct": 0.5, "vol_ratio": 1.0, "above_sma50": False})
    assert [s["passed"] for s in _sig(card, "momentum")["subs"]] == [False, False, False]
    assert _sig(card, "momentum")["known"] == 3


def test_total_points_count_only_known_passes():
    unknown = _card([dict(fiscal_year=2023, **_FULL)])
    full = _card(_years())
    assert unknown["total_points"] == sum(s["passed"] for s in unknown["signals"])
    assert unknown["total_points"] < full["total_points"]


def test_unknown_runway_and_net_debt_when_the_balance_sheet_is_missing():
    card = _card([dict(fiscal_year=2023, revenue=130.0, operating_cash_flow=-60.0, capex=10.0)])
    assert _sub(card, "fcf")["passed"] is False            # known: FCF is -70
    assert _sub(card, "net_debt_assets")["passed"] is None
    assert _sub(card, "runway")["passed"] is None          # a burning company without cash data: unknown


# ── the board ───────────────────────────────────────────────────────────────

_THR = engine.THRESHOLDS


def _result(card):
    return {"asof": "2026-08-23T00:00:00", "universe_total": 1, "universe_refreshed_by": "Asymmetric Growth",
         "universe_generated_at": "2026-08-24T10:00:00+00:00",
         "summary": {"scanned": 1, "pts_9": 0, "pts_8_7": 0, "pts_6_5": 0, "pts_lt5": 1, "disruptive_themed": 0,
                     "tech_breakthrough": 0},
         "thresholds": _THR, "cards": [card]}


def _page(card):
    return render_asymmetric_growth_hunter_html(_result(card))


def _js(tmp_path, card, tail):
    """Run the board's script with the card loaded through the payload route (S7, K10)."""
    return run_board_js(_page(card), tail, tmp_path, payload=board_payload(_result(card)))


def test_the_signal_line_names_the_known_count(tmp_path):
    card = _card([dict(fiscal_year=2023, **_FULL)])
    out = _js(tmp_path, card, "console.log(JSON.stringify([sigHtml(DATA[0].signals[0]), sigHtml(DATA[0].signals[1])]))")
    assert "1/3 of 1 known" in out[0]
    assert "3/3" in out[1] and "known" not in out[1]


def test_the_rescoring_leaves_unknown_unknown_and_counts_known_passes(tmp_path):
    card = _card([dict(fiscal_year=2023, **_FULL)])
    out = _js(tmp_path, card, "var r = rescore(DATA[0]); console.log(JSON.stringify("
                              "{sig: r.signals.map(function(s){return [s.passed, s.known, s.total]}), "
                              "subs: r.signals[0].subs.map(function(s){return s.passed}), pts: r.total_points}))")
    assert out["sig"][0] == [1, 1, 3]
    assert out["subs"] == [None, None, True]
    assert out["pts"] == sum(s[0] for s in out["sig"])


def test_the_rescoring_does_not_turn_a_missing_value_into_a_pass_when_a_toggle_is_off(tmp_path):
    card = _card(_years(), {"price": 100.0, "dist_52w_pct": 0.05, "vol_ratio": 1.6, "above_sma50": None})
    out = _js(tmp_path, card, "console.log(JSON.stringify(rescore(DATA[0]).signals[2].subs.map(function(s){return s.passed})))")
    assert out == [True, True, None]


def test_the_rescoring_agrees_with_the_server_when_nothing_is_moved(tmp_path):
    card = _card([dict(fiscal_year=2022, **{**_FULL, "revenue": 100.0}), dict(fiscal_year=2023, **_FULL)],
                 {"price": 100.0, "dist_52w_pct": None, "vol_ratio": 1.6, "above_sma50": True})
    out = _js(tmp_path, card, "console.log(JSON.stringify(rescore(DATA[0]).signals.map(function(s){return s.subs.map(function(x){return x.passed})})))")
    assert out == [[s["passed"] for s in sig["subs"]] for sig in card["signals"]]


def test_missing_income_and_cash_flow_figures_are_unknown_not_failed():
    card = _card([dict(fiscal_year=2022, revenue=100.0), dict(fiscal_year=2023, revenue=130.0)])
    assert _sub(card, "gross_margin")["passed"] is None
    assert _sub(card, "fcf")["passed"] is None
    assert _sub(card, "runway")["passed"] is None


def test_the_rescoring_leaves_an_unknown_free_cash_flow_unknown(tmp_path):
    card = _card([dict(fiscal_year=2023, revenue=130.0)])
    out = _js(tmp_path, card, "console.log(JSON.stringify(rescore(DATA[0]).signals[1].subs.map(function(s){return s.passed})))")
    assert out == [None, None, None]


def test_the_known_count_is_escaped_in_the_signal_line(tmp_path):
    card = _card(_years())
    card["signals"][0]["known"] = "<img src=x onerror=alert(1)>"
    out = _js(tmp_path, card, "console.log(JSON.stringify(sigHtml(DATA[0].signals[0])))")
    assert "&lt;img src=x" in out and "<img" not in out
