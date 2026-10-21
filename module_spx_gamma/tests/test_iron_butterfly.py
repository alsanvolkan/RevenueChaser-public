"""Tests for the Iron Butterfly "Pin Harvest" panel (engines/iron_butterfly.py)."""
from __future__ import annotations

from RevenueChaser.module_spx_gamma import render
from RevenueChaser.module_spx_gamma.chains import (
    black_scholes_mid,
    normalize_option_chain,
    parse_option_symbol,
)
from RevenueChaser.module_spx_gamma.engines import iron_butterfly as ib
from .conftest import make_option_chain, make_snapshot


def _chain_dict(strikes: list[tuple[float, float]]) -> dict:
    """Minimal chain from [(strike, mid)] pairs with NBBO around the mid."""
    rows = []
    for strike, mid in strikes:
        for cp in ("C", "P"):
            sym = f"SPXW260814{cp}{int(strike * 1000):08d}"
            rows.append({
                "option_symbol": sym,
                "nbbo_bid": f"{mid - 0.5:.2f}",
                "nbbo_ask": f"{mid + 0.5:.2f}",
                "implied_volatility": "0.12",
                "theta": "-1.0",
            })
    return {"data": rows}


# ── symbol parsing & chain normalization ──

def test_parse_option_symbol():
    assert parse_option_symbol("SPXW260814C07800000") == {
        "root": "SPXW", "expiry": "2026-08-14", "type": "C", "strike": 7800.0}
    assert parse_option_symbol("SPXW260814P07765000")["strike"] == 7765.0
    assert parse_option_symbol("SPX260814C07800000")["root"] == "SPX"
    assert parse_option_symbol("garbage") is None


def test_normalize_option_chain_quotes():
    rows = normalize_option_chain(make_option_chain())
    by = {(r["strike"], r["type"]): r for r in rows}
    assert by[(7800.0, "C")]["mid"] == 20.0
    assert by[(7800.0, "C")]["price_source"] == "quote"
    assert by[(7800.0, "P")]["mid"] == 15.0
    assert by[(7835.0, "C")]["mid"] == 5.0
    assert by[(7765.0, "P")]["mid"] == 6.0


def test_normalize_option_chain_models_mid_without_quotes():
    rows = normalize_option_chain({
        "data": [{"option_symbol": "SPXW260814C07800000",
                  "implied_volatility": "0.20"}]}, spot=7784.0, asof_date="2026-08-14")
    assert rows and rows[0]["bid"] is None and rows[0]["ask"] is None
    assert rows[0]["mid"] is not None and rows[0]["mid"] > 0
    assert rows[0]["price_source"] == "modeled"


def test_black_scholes_atm_call():
    # ATM (K=S) call at r=0 with iv=0.2, T=1yr -> ~7.96 (BS closed form)
    mid = black_scholes_mid(100.0, 100.0, 1.0, 0.2, "C")
    assert mid is not None and abs(mid - 7.96) < 0.02
    assert black_scholes_mid(100.0, 100.0, 1.0, 0.0, "C") is None
    assert black_scholes_mid(100.0, 100.0, -1.0, 0.2, "C") is None


# ── wing width ──

def test_wing_width_rounds_to_strike_grid():
    assert ib.wing_width(36.87) == 35.0
    assert ib.wing_width(54.32) == 55.0
    assert ib.wing_width(10.0) == 25.0   # clamp min
    assert ib.wing_width(500.0) == 75.0  # clamp max
    assert ib.wing_width(None) is None


# ── structure pricing on the synthetic snapshot ──

def test_synthetic_iron_butterfly_pricing(snapshot):
    p = snapshot["iron_butterfly"]
    assert p is not None
    assert p["strategy"] == "Iron Butterfly"
    assert p["center_strike"] == 7800.0
    assert p["wing_width"] == 35.0
    assert p["net_credit"] == 24.0
    assert p["max_risk"] == 11.0
    assert p["max_profit"] == 24.0
    assert p["lower_breakeven"] == 7776.0
    assert p["upper_breakeven"] == 7824.0
    assert abs(p["return_on_risk"] - round(24.0 / 11.0, 3)) < 1e-9
    assert p["net_theta"] == 10.0
    assert p["width_source"]["kind"] == "expected_move"
    assert p["width_source"]["em_points"] is not None


def test_synthetic_leg_selection(snapshot):
    legs = {(l["action"], l["type"]): l for l in snapshot["iron_butterfly"]["legs"]}
    assert legs[("BUY", "PUT")]["strike"] == 7765.0 and legs[("BUY", "PUT")]["mid"] == 6.0
    assert legs[("SELL", "PUT")]["strike"] == 7800.0 and legs[("SELL", "PUT")]["mid"] == 15.0
    assert legs[("SELL", "CALL")]["strike"] == 7800.0 and legs[("SELL", "CALL")]["mid"] == 20.0
    assert legs[("BUY", "CALL")]["strike"] == 7835.0 and legs[("BUY", "CALL")]["mid"] == 5.0


def test_missing_wing_strike_falls_back_to_nearest():
    chain = _chain_dict([(7800.0, 20.0), (7850.0, 5.0), (7750.0, 6.0)])
    snap = make_snapshot()
    p = ib.build_iron_butterfly({
        "current_level": snap["current_level"],
        "expected_move": {"points": 36.87, "iv_used": 0.076},
        "prediction": {"active_pin": 7800.0, "downside_floor": 7650.0, "upside_trigger": 7800.0},
        "option_chain": normalize_option_chain(chain),
    })
    legs = {(l["action"], l["type"]): l for l in p["legs"]}
    assert legs[("BUY", "CALL")]["strike"] == 7850.0  # nearest to 7835
    assert legs[("BUY", "PUT")]["strike"] == 7750.0   # nearest to 7765


# ── signal layer ──

def test_signal_pin_when_spot_in_band():
    s = ib.signal_recommendation({"current_level": 7798.0, "prediction": {}}, 7800.0)
    assert s["signal"] == "PIN"
    assert "theta" in s["action"].lower()


def test_signal_pin_text_names_the_centre_strike():
    # DF23: the text used to say "7,630" whatever the centre was.
    s = ib.signal_recommendation({"current_level": 7798.0, "prediction": {}}, 7800.0)
    assert "7,800 straddle" in s["action"]
    assert "7,630" not in s["action"]
    s2 = ib.signal_recommendation({"current_level": 5001.0, "prediction": {}}, 5000.0)
    assert "5,000 straddle" in s2["action"]


def test_signal_bearish_when_spot_below_band():
    pred = {"downside_floor": 7650.0, "vanna_floor": 7650.0}
    s = ib.signal_recommendation({"current_level": 7784.0, "prediction": pred}, 7800.0)
    assert s["signal"] == "BEARISH_BREAKOUT"
    assert s["target"] == 7650.0


def test_signal_bullish_when_spot_above_band():
    pred = {"upside_trigger": 7820.0}
    s = ib.signal_recommendation({"current_level": 7835.0, "prediction": pred}, 7800.0)
    assert s["signal"] == "BULLISH_ACCEL"
    assert s["target"] == 7835.0  # upside_trigger + 15


def test_signal_none_without_spot():
    s = ib.signal_recommendation({"current_level": None, "prediction": {}}, 7800.0)
    assert s["signal"] is None


# ── degradation ──

def test_build_degrades_to_none():
    snap = make_snapshot()
    assert ib.build_iron_butterfly({**snap, "current_level": None}) is None
    assert ib.build_iron_butterfly({**snap, "option_chain": []}) is None
    assert ib.build_iron_butterfly({**snap, "prediction": {}}) is None


# ── render smoke ──

def test_html_renders_iron_butterfly_section(snapshot):
    html = render.render_html(snapshot)
    assert "Iron Butterfly" in html
    assert "Pin Harvest" in html
    assert "Net Credit" in html and "Max Risk" in html
    assert "7,800" in html  # center strike rendered
    assert "Return on Risk" in html


def test_html_omits_section_when_degraded():
    snap = make_snapshot()
    snap["iron_butterfly"] = None
    html = render.render_html(snap)
    assert "Iron Butterfly" not in html
