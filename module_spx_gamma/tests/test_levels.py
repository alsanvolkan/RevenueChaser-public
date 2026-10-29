"""Tests for core level mapping + 0DTE strike-space metrics."""
from __future__ import annotations

import pytest

from RevenueChaser.module_spx_gamma.levels import (
    band_walls,
    cumulative_gex_crossing,
    cumulative_gex_envelope,
    intraday_concentration,
    intraday_put_concentration,
    map_core_levels,
    map_flip_levels,
    map_volume_levels,
)


def test_map_core_levels():
    levels = map_core_levels({
        "gamma_magnet": "7900", "call_wall": "7900",
        "put_wall": "7750", "gamma_flip": "7820.63",
    })
    assert levels == {
        "equilibrium_point": 7900.0,
        "upper_resistance_band": 7900.0,
        "lower_support_band": 7750.0,
        "regime_threshold": 7820.63,
    }


def test_map_flip_levels():
    flips = map_flip_levels({"gamma_flip": "7720.47"}, {"gamma_flip": "7691.65"})
    assert flips["gamma_flip_all"] == 7720.47
    assert flips["gamma_flip_0dte"] == 7691.65


def test_band_walls_181_365_put_wall():
    # synthetic per-(strike,expiry) rows: 181-365d band has big positive net at 7500
    rows = [
        {"expiry": "2026-09-18", "dte": 35, "strike": 7600.0, "net_gamma": -100.0},
        {"expiry": "2026-09-18", "dte": 35, "strike": 7750.0, "net_gamma": 50.0},
        {"expiry": "2026-12-31", "dte": 139, "strike": 7500.0, "net_gamma": 200.0},
        {"expiry": "2026-12-31", "dte": 139, "strike": 7700.0, "net_gamma": 30.0},
        {"expiry": "2027-06-18", "dte": 308, "strike": 7500.0, "net_gamma": 500.0},
        {"expiry": "2027-06-18", "dte": 308, "strike": 7900.0, "net_gamma": 40.0},
        {"expiry": "2027-06-18", "dte": 308, "strike": 7650.0, "net_gamma": -20.0},
    ]
    walls = band_walls(rows, spot=7700.0)
    assert len(walls) == 6
    b = next(w for w in walls if w["band"] == "181-365")
    assert b["put_wall"] == 7500.0
    assert walls[-1]["band"] == "ALL"


def test_band_walls_none_without_spot():
    rows = [{"dte": 30, "strike": 7500.0, "net_gamma": 10.0}]
    assert band_walls(rows, spot=None) == []


def test_intraday_concentration_uses_max_abs_net_gamma():
    rows = [
        {"expiry": "2026-08-14", "strike": 7700, "net_gamma": -299},
        {"expiry": "2026-08-14", "strike": 7800, "net_gamma": 250},
        {"expiry": "2026-08-21", "strike": 9999, "net_gamma": 9999},
    ]
    assert intraday_concentration(rows, "2026-08-14") == 7700


def test_intraday_concentration_all_zero_returns_none():
    rows = [
        {"expiry": "2026-08-14", "strike": 7700, "net_gamma": 0},
        {"expiry": "2026-08-14", "strike": 7900, "net_gamma": 0},
    ]
    assert intraday_concentration(rows, "2026-08-14") is None


def test_intraday_put_concentration_most_negative_put_gex():
    rows = [
        {"expiry": "2026-08-14", "strike": 7700, "put_gex": -300},
        {"expiry": "2026-08-14", "strike": 7800, "put_gex": -100},
        {"expiry": "2026-08-21", "strike": 7900, "put_gex": -9999},
    ]
    assert intraday_put_concentration(rows, "2026-08-14") == 7700


def test_intraday_put_concentration_no_negative_returns_none():
    rows = [{"expiry": "2026-08-14", "strike": 7700, "put_gex": 0}]
    assert intraday_put_concentration(rows, "2026-08-14") is None


def test_cumulative_gex_crossing():
    rows = [
        {"expiry": "2026-08-14", "strike": 7700, "net_gamma": -299},
        {"expiry": "2026-08-14", "strike": 7800, "net_gamma": 250},
        {"expiry": "2026-08-14", "strike": 7900, "net_gamma": 198},
    ]
    # cumulative: -299 -> -49 -> +149, crossing between 7800 and 7900
    crossing = cumulative_gex_crossing(rows, "2026-08-14", 7784.0)
    assert crossing == pytest.approx(7824.75)


def test_cumulative_gex_crossing_no_crossing_returns_none():
    rows = [
        {"expiry": "2026-08-14", "strike": 7700, "net_gamma": -10},
        {"expiry": "2026-08-14", "strike": 7800, "net_gamma": -20},
    ]
    assert cumulative_gex_crossing(rows, "2026-08-14", 7784.0) is None


def test_envelope_detects_absorption_and_air_gap():
    # strikes: heavy negative below 7750, heavy positive above -> air gap then absorption
    rows = [
        {"strike": 7700, "net_gamma": -1000},
        {"strike": 7725, "net_gamma": -500},
        {"strike": 7750, "net_gamma": -100},
        {"strike": 7775, "net_gamma": 600},
        {"strike": 7800, "net_gamma": 1700},
    ]
    # cumulative: -1000 -> -1500 -> -1600 -> -1000 -> +700 (crosses between 7775 and 7800)
    env = cumulative_gex_envelope(rows, spot=7775.0, window_pct=0.05)
    assert env is not None
    assert env["air_gap"] is not None
    assert env["absorption"] is not None
    assert env["air_gap"]["kind"] == "air_gap"
    assert env["absorption"]["kind"] == "absorption"
    assert env["air_gap"]["lo"] < env["absorption"]["lo"]
    assert env["zero_crossing"] is not None


def test_envelope_returns_none_without_spot():
    rows = [{"strike": 7700, "net_gamma": -100}]
    assert cumulative_gex_envelope(rows, spot=None) is None


def test_envelope_returns_none_insufficient_rows():
    rows = [{"strike": 7700, "net_gamma": -100}]
    assert cumulative_gex_envelope(rows, spot=7700.0) is None


def test_envelope_windows_around_spot():
    rows = [{"strike": 1000, "net_gamma": -1000}, {"strike": 7700, "net_gamma": 500}]
    # 7700 far outside the 5% window around spot 7700 -> excluded
    env = cumulative_gex_envelope(rows, spot=7700.0, window_pct=0.02)
    assert env is None or (env["air_gap"] is None and env["absorption"] is None)


def test_map_volume_levels():
    vol = {"call_wall": 7640.0, "put_wall": 7630.0, "gamma_magnet": 7640.0, "gamma_flip": 7649.3}
    out = map_volume_levels(vol)
    assert out["call_wall_vol"] == 7640.0
    assert out["put_wall_vol"] == 7630.0
    assert out["gamma_magnet_vol"] == 7640.0
    assert out["gamma_flip_vol"] == 7649.3
    # missing data -> None, never crash
    assert map_volume_levels({})["call_wall_vol"] is None


def test_a_band_wall_is_the_strike_with_the_most_gamma_summed_over_the_band():
    """Fable review S5: band_walls took the largest single (expiry, strike) cell, so one big expiry beat a strike
    that holds more gamma across the band's expiries. The wall is now the strike sum over the band (and over all
    expiries for the ALL row), which is what Put Support says it is."""
    rows = [
        {"expiry": "2027-03-19", "dte": 217, "strike": 7400.0, "net_gamma": 100.0},   # one big cell
        {"expiry": "2027-03-19", "dte": 217, "strike": 7500.0, "net_gamma": 70.0},
        {"expiry": "2027-06-18", "dte": 308, "strike": 7500.0, "net_gamma": 60.0},    # 7500 holds 130 in the band
        {"expiry": "2027-03-19", "dte": 217, "strike": 7900.0, "net_gamma": 90.0},
        {"expiry": "2027-06-18", "dte": 308, "strike": 7800.0, "net_gamma": 50.0},
        {"expiry": "2027-12-17", "dte": 490, "strike": 7800.0, "net_gamma": 50.0},    # 365+: only in ALL
    ]
    walls = {w["band"]: w for w in band_walls(rows, spot=7700.0)}
    assert (walls["181-365"]["put_wall"], walls["181-365"]["put_wall_net"]) == (7500.0, 130.0)
    assert (walls["181-365"]["call_wall"], walls["181-365"]["call_wall_net"]) == (7900.0, 90.0)
    assert (walls["ALL"]["call_wall"], walls["ALL"]["call_wall_net"]) == (7800.0, 100.0)
