"""SPX Gamma S2 (#177): one regime (spot vs flip), normalize fixes G2/G4/G5/G6/G11/G12/G14, no MenthorQ."""
from __future__ import annotations

import copy

import pytest

from RevenueChaser.module_spx_gamma import relations, render, summary
from RevenueChaser.module_spx_gamma.engines import prediction_engine as pe
from RevenueChaser.module_spx_gamma.normalize import compute_positioning_deltas, normalize_snapshot

from .conftest import make_raw, make_snapshot

DAY = "2026-08-14"


def _raw(flip=None, flip_vol=None, **over):
    raw = make_raw()
    if flip is not None:
        raw["gex_levels"]["data"]["gamma_flip"] = str(flip)
    if flip_vol is not None:
        raw["gex_levels_vol"]["data"]["gamma_flip"] = str(flip_vol)
    raw.update(over)
    return raw


# ── G4: one regime, spot vs the OI gamma flip ──────────────────────────────
def test_regime_is_spot_against_the_flip_not_the_sign_of_greek_exposure():
    # fixture: spot 7784, OI flip 7820.63 -> spot is BELOW the flip, while the summed
    # /greek-exposure/strike gamma is positive (+374). The sign of that sum used to decide.
    s = normalize_snapshot(make_raw(), DAY)
    assert s["net_gamma_total"] == 374.0
    assert s["regime"] == "Short Gamma — Amplifying"
    assert s["source_chip"] == "NEGATIVE"
    assert s["regime_detail"]["state"] == "short"


def test_spot_above_the_flip_is_long_gamma():
    s = normalize_snapshot(_raw(flip=7700), DAY)
    assert s["regime"] == "Long Gamma — Dampening"
    assert s["source_chip"] == "POSITIVE"


def test_oi_and_volume_flips_are_shown_side_by_side():
    d = normalize_snapshot(_raw(flip=7700, flip_vol=7800), DAY)["regime_detail"]
    assert d["flips"]["oi"] == 7700.0 and d["flips"]["vol"] == 7800.0
    assert d["sides"]["oi"] == "long" and d["sides"]["vol"] == "short"
    assert d["agree"] is False


def test_local_cumulative_crossing_is_the_cross_check():
    raw = _raw()
    # OI gamma by strike: -3, +5 (cum +2) -> the cumulative changes sign between 7650 and 7850
    raw["spot_exposure"]["data"] = [
        {"strike": "7650", "call_gamma_oi": "0", "put_gamma_oi": "-3"},
        {"strike": "7850", "call_gamma_oi": "5", "put_gamma_oi": "0"},
    ]
    d = normalize_snapshot(raw, DAY)["regime_detail"]
    assert d["flips"]["cumulative"] == pytest.approx(7650 + 3 / 5 * 200)


def test_missing_flip_is_unknown_not_a_regime():
    raw = _raw()
    raw["gex_levels"]["data"].pop("gamma_flip")
    s = normalize_snapshot(raw, DAY)
    assert s["regime"] is None and s["source_chip"] is None
    assert s["regime_detail"]["state"] == "unknown"


def test_missing_spot_is_unknown():
    s = normalize_snapshot(_raw(ohlc={"source": "x", "data": []}), DAY)
    assert s["regime"] is None and s["source_chip"] is None


def test_empty_raw_day_normalizes_to_an_explicit_empty_state():
    s = normalize_snapshot({}, DAY)
    assert s["regime"] is None and s["source_chip"] is None
    assert s["net_gamma_total"] is None and s["net_dex"] is None and s["matrix_gex"] is None
    assert s["prediction"]["bias"] is None


def test_the_empty_state_renders_without_crashing():
    s = normalize_snapshot({}, DAY)
    html = render.render_html(s)
    md = render.render_markdown(s)
    assert "Short Gamma" not in html and "Short Gamma" not in md
    assert "Amplifying" not in html


# ── G2: no crash and no invented numbers when spot or flip is missing ──────
@pytest.mark.parametrize("which", ["flip", "spot"])
def test_relations_do_not_crash_without_spot_or_flip(which):
    s = make_snapshot()
    if which == "flip":
        s["levels"]["regime_threshold"] = None
        s["regime"] = None
    else:
        s["current_level"] = None
        s["regime"] = None
    gamma = relations.greek_relations(s)[0]
    assert gamma["headline"] == "Gamma — regime unknown"
    render.render_html(s)


def test_relation_text_agrees_with_spot_versus_flip():
    below = normalize_snapshot(make_raw(), DAY)             # spot 7784 < flip 7820.63
    above = normalize_snapshot(_raw(flip=7700), DAY)        # spot 7784 > flip 7700
    assert "below the gamma flip" in relations.greek_relations(below)[0]["english"]
    assert "above the gamma flip" in relations.greek_relations(above)[0]["english"]


def test_gamma_card_follows_the_regime_and_is_none_safe():
    below = normalize_snapshot(make_raw(), DAY)
    card = summary.build_summary(below)[1]
    assert card["signal"]["label"] == "Amplifying"
    assert "Short" in card["rows"][0][2]
    empty = summary.build_summary(normalize_snapshot({}, DAY))[1]
    assert empty["signal"]["label"] not in ("Amplifying", "Dampening")


# ── G4 in the engine: direction follows the regime ─────────────────────────
def test_gamma_term_is_the_regime_not_the_greek_exposure_sum():
    snap = normalize_snapshot(_raw(flip=7700), DAY)          # long gamma
    snap["net_gamma_total"] = -999.0                         # wrong-unit series must not decide
    assert pe.directional_score(snap)["components"]["gamma_term"] == 1.0
    snap2 = normalize_snapshot(make_raw(), DAY)              # short gamma
    snap2["net_gamma_total"] = 999.0
    assert pe.directional_score(snap2)["components"]["gamma_term"] == -1.0


def test_unknown_regime_gives_no_direction():
    snap = normalize_snapshot(make_raw(), DAY)
    snap["regime_detail"] = {"state": "unknown"}
    out = pe.directional_score(snap)
    assert out["s_dir"] is None and out["bias"] is None


# ── G5 / G6 ────────────────────────────────────────────────────────────────
def test_zero_dte_strikes_are_not_counted_twice():
    raw = make_raw()
    raw["strike_expiry_all"][DAY] = copy.deepcopy(raw["strike_expiry"][DAY])  # same payload again
    s = normalize_snapshot(raw, DAY)
    keys = [(r["expiry"], r["strike"]) for r in s["strike_expiry_rows"]]
    assert len(keys) == len(set(keys))


def test_matrix_gex_keeps_the_zero_dte_row():
    s = normalize_snapshot(make_raw(), DAY)
    zero = 83563.6633 - 29026.4049
    seven = 50000.0 - 40000.0
    assert s["matrix_gex"] == pytest.approx(zero + seven)


# ── G11: the bearish target sits below its trigger ─────────────────────────
def test_bearish_target_above_its_trigger_is_not_shown():
    scen = {"active_pin": 7700.0, "downside_trigger": 7700.0, "vanna_floor": 7710.0,
            "gex_floor": 7705.0, "upside_trigger": None}
    row = pe.build_scenario_rows(7784.0, scen, None)[2]
    assert row["target"] == "n/a"


def test_bearish_target_below_its_trigger_is_shown():
    scen = {"active_pin": 7700.0, "downside_trigger": 7700.0, "vanna_floor": 7650.0,
            "gex_floor": 7600.0, "upside_trigger": None}
    row = pe.build_scenario_rows(7784.0, scen, None)[2]
    assert row["target"].startswith("7,650")


def test_the_gex_floor_is_used_when_the_vanna_floor_is_not_below_the_trigger():
    scen = {"active_pin": 7700.0, "downside_trigger": 7700.0, "vanna_floor": 7710.0,
            "gex_floor": 7600.0, "upside_trigger": None}
    assert pe.build_scenario_rows(7784.0, scen, None)[2]["target"].startswith("7,600")


# ── G12 ────────────────────────────────────────────────────────────────────
def test_missing_history_gives_no_delta_not_zero():
    out = compute_positioning_deltas(100.0, "net_gex_oi", {})
    assert out["delta_1d"] is None and out["delta_1w"] is None
    hist = {"2026-08-13": {"net_gex_oi": 90.0}, "2026-08-14": {"net_gex_oi": 100.0}}
    out = compute_positioning_deltas(100.0, "net_gex_oi", hist)
    assert out["delta_1d"] == 10.0 and out["delta_1w"] is None


def test_no_current_value_gives_no_delta():
    assert compute_positioning_deltas(None, "net_gex_oi", {"a": {"net_gex_oi": 1}, "b": {}})["delta_1d"] is None


# ── G14 ────────────────────────────────────────────────────────────────────
def _card(title, label, lean=None):
    c = {"title": title, "signal": {"label": label, "tone": "amber"}}
    if lean:
        c["lean"] = lean
    return c


def test_charm_votes_when_its_signals_agree():
    m = summary.signal_matrix([_card("Charm", "Agree", "bearish"), _card("Delta", "Bullish")])
    assert m["votes"] == {"bullish": 1, "bearish": 1} and m["total_directional"] == 2


def test_charm_does_not_vote_when_mixed():
    m = summary.signal_matrix([_card("Charm", "Mixed"), _card("Delta", "Bullish")])
    assert m["total_directional"] == 1


def test_the_real_charm_card_carries_its_lean():
    snap = make_snapshot()
    card = summary.charm_card(snap["spot_exposures"], snap["charm_qualification"], snap["current_level"])
    assert card["signal"]["label"] in ("Agree", "Mixed")
    assert card.get("lean") in ("bullish", "bearish", None)
    if card["signal"]["label"] == "Agree":
        assert card["lean"] is not None


def test_role_text_matches_the_short_gamma_label():
    assert render._role("Short Gamma — Amplifying") == "threshold (breakout risk)"
    assert render._role("Long Gamma — Dampening") == "support (mean-reversion)"
    assert render._role(None) == "support (mean-reversion)"


# ── MenthorQ table removed, flip methods shown instead ─────────────────────
def test_menthorq_is_gone_from_the_report():
    s = make_snapshot()
    assert "menthorq" not in render.render_html(s).lower()
    assert "menthorq" not in render.render_markdown(s).lower()
    assert not hasattr(render, "load_menthorq_reference")


def test_report_lists_the_flip_methods():
    s = normalize_snapshot(_raw(flip=7700, flip_vol=7800), DAY)
    ctx = render.build_context(s)
    names = [r["method"] for r in ctx["flip_methods"]]
    assert names[0].startswith("UW flip, open interest")
    assert any("volume" in n for n in names) and any("cumulative" in n.lower() for n in names)
    html = render.render_html(s)
    assert "Gamma flip methods" in html
    assert "UW does not document" in html


def test_zero_flow_at_a_wall_does_not_divide_by_zero():
    from RevenueChaser.module_spx_gamma.flow_signals import evaluate_flow_signal
    snap = make_snapshot()
    snap["flow_strikes"] = [{"strike": 7750.0, "call_volume": 0.0, "put_volume": 0.0}]
    snap["current_level"] = 7740.0
    assert evaluate_flow_signal(snap)["label"]
