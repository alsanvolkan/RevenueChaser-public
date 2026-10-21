"""Tests for the volume/flow confluence signal engine."""
from __future__ import annotations

from RevenueChaser.module_spx_gamma.flow_signals import evaluate_flow_signal


def _snap(spot=7700.0, call_wall_oi=7800.0, call_wall_vol=7640.0,
          put_wall_oi=None, flow=None, net_gex=5000.0):
    flow = flow or []
    return {
        "current_level": spot,
        "net_gamma_total": net_gex,
        "source_chip": "POSITIVE" if net_gex > 0 else "NEGATIVE",
        "matrix_gex": net_gex * 0.8,
        "levels": {
            "upper_resistance_band": call_wall_oi,
            "call_wall_vol": call_wall_vol,
            "lower_support_band": put_wall_oi,
        },
        "flow_strikes": flow,
    }


def _flow(strike, cv, pv, cva=None, cvb=None):
    return {
        "strike": strike, "call_volume": cv, "put_volume": pv,
        "call_volume_ask_side": cva, "call_volume_bid_side": cvb,
        "call_premium": None, "put_premium": None, "call_trades": 0, "put_trades": 0,
    }


def test_no_signal_without_data():
    # DF26: missing flow is not a wall that holds
    res = evaluate_flow_signal(_snap(flow=[]))
    assert res["signal"] is None and res["label"] == "No flow data"


def test_squeeze_alert_at_call_wall():
    # spot at OI call wall with dominant call flow on Ask side
    snap = _snap(spot=7730.0, call_wall_oi=7750.0, flow=[
        _flow(7745, cv=300, pv=100, cva=200),  # ask ratio 0.67, call 3x put
    ])
    res = evaluate_flow_signal(snap)
    assert res["signal"] == "SQUEEZE_ALERT"
    assert res["tone"] == "green"


def test_fakeout_risk_above_wall_weak_flow():
    # price above OI wall but flow not confirming + vol wall below
    snap = _snap(spot=7810.0, call_wall_oi=7800.0, call_wall_vol=7780.0, flow=[
        _flow(7800, cv=50, pv=120, cvb=40),  # call <= put, bid-heavy
    ])
    res = evaluate_flow_signal(snap)
    assert res["signal"] == "FAKEOUT_RISK"
    assert res["tone"] == "red"


def test_wall_holding_when_flow_confirms_nothing():
    # at wall but call flow too weak -> holding
    snap = _snap(spot=7730.0, call_wall_oi=7750.0, flow=[
        _flow(7745, cv=100, pv=100, cva=50),
    ])
    res = evaluate_flow_signal(snap)
    assert res["signal"] == "WALL_HOLDING"
