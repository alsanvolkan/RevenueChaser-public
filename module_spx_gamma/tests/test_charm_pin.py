"""Tests for Charm Peak/Pin qualification and Vanna peaks."""
from __future__ import annotations

from RevenueChaser.module_spx_gamma.charm_pin import (
    MODE_DIRECTIONALIZED_VOLUME,
    MODE_OPEN_INTEREST,
    net_charm,
    normalize_spot_exposure_rows,
    qualify_charm,
    vanna_peaks,
)


def test_qualify_charm_pin():
    rows = [
        {"strike": 7900.0, "call_charm_oi": -40000000, "put_charm_oi": 10000000},  # -30M, near spot
        {"strike": 8000.0, "call_charm_oi": -20000000, "put_charm_oi": 5000000},   # -15M runner-up
    ]
    q = qualify_charm(rows, 7784.0)
    assert q["label"] == "Charm Pin"
    assert q["strike"] == 7900.0
    assert q["qualification_passed"] is True


def test_qualify_charm_peak_far_from_spot():
    rows = [
        {"strike": 8000.0, "call_charm_oi": -40000000, "put_charm_oi": 10000000},  # -30M, 2.8% away
        {"strike": 7900.0, "call_charm_oi": -20000000, "put_charm_oi": 5000000},
    ]
    q = qualify_charm(rows, 7784.0)
    assert q["label"] == "Charm Peak"
    assert q["qualification_passed"] is False


def test_qualify_charm_peak_not_dominant():
    rows = [
        {"strike": 7900.0, "call_charm_oi": -20000000, "put_charm_oi": 10000000},  # -10M
        {"strike": 7800.0, "call_charm_oi": -15000000, "put_charm_oi": 7000000},   # -8M
    ]
    q = qualify_charm(rows, 7784.0)
    assert q["label"] == "Charm Peak"  # dominance 1.25 < 1.5


def test_qualify_charm_no_runner_up():
    rows = [{"strike": 7900.0, "call_charm_oi": -40000000, "put_charm_oi": 10000000}]
    q = qualify_charm(rows, 7784.0)
    assert q["label"] == "Charm Peak"


def test_qualify_charm_empty():
    q = qualify_charm([], 7784.0)
    assert q["strike"] is None
    assert q["label"] == "Charm Peak"


def test_vanna_peaks_call_and_put():
    rows = [
        {"strike": 7650.0, "call_vanna_vol": 0, "put_vanna_vol": 30000000},
        {"strike": 7850.0, "call_vanna_vol": 15000000, "put_vanna_vol": 0},
        {"strike": 8000.0, "call_vanna_vol": 10000000, "put_vanna_vol": 0},
    ]
    v = vanna_peaks(rows)
    assert v["call_peak"]["strike"] == 7850.0
    assert v["call_peak"]["signed_call_vanna"] == 15000000.0
    assert v["put_peak"]["strike"] == 7650.0
    assert v["put_peak"]["signed_put_vanna"] == 30000000.0
    assert v["mode"] == MODE_DIRECTIONALIZED_VOLUME


def test_vanna_peaks_empty():
    v = vanna_peaks([])
    assert v["call_peak"] is None
    assert v["put_peak"] is None


def test_net_charm_mode_switch():
    row = {"call_charm_oi": "-40", "put_charm_oi": "10", "call_charm_vol": "-5", "put_charm_vol": "2"}
    assert net_charm(row, MODE_OPEN_INTEREST) == -30.0
    assert net_charm(row, MODE_DIRECTIONALIZED_VOLUME) == -3.0


def test_normalize_spot_exposure_rows():
    payload = {"data": [{"strike": "7900", "call_charm_oi": "1", "put_charm_oi": "2",
                         "call_charm_vol": "3", "put_charm_vol": "4",
                         "call_vanna_oi": "5", "put_vanna_oi": "6",
                         "call_vanna_vol": "7", "put_vanna_vol": "8"}]}
    rows = normalize_spot_exposure_rows(payload)
    assert rows[0]["strike"] == 7900.0
    assert rows[0]["put_vanna_vol"] == 8.0
