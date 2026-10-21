"""Tests for normalization."""
from __future__ import annotations

import pytest

from RevenueChaser.module_spx_gamma.config import net_sum, to_float
from RevenueChaser.module_spx_gamma.normalize import (
    current_level,
    normalize_candles,
    normalize_snapshot,
    normalize_strike_expiry_rows,
)


def test_to_float():
    assert to_float("7900") == 7900.0
    assert to_float(7820.63) == 7820.63
    assert to_float(None) is None
    assert to_float("") is None
    assert to_float("abc") is None
    assert to_float(float("nan")) is None


def test_net_sum():
    assert net_sum("100", "-40") == 60.0
    assert net_sum(None, None) is None
    assert net_sum(None, "5") == 5.0


def test_normalize_strike_expiry_rows_combines_payloads():
    payloads = {"a": {"data": [{"expiry": "a", "strike": "1", "call_gex": "2", "put_gex": "-1"}]},
                "b": {"data": [{"expiry": "b", "strike": "2", "call_gex": "5", "put_gex": "-3"}]}}
    rows = normalize_strike_expiry_rows(payloads)
    assert {r["expiry"] for r in rows} == {"a", "b"}
    assert rows[0]["net_gamma"] in (1.0, 2.0)


def test_normalize_candles_parses_ohlc_payload():
    payload = {"source": "yfinance", "data": [{"date": "2026-08-14", "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "100"}]}
    rows = normalize_candles(payload)
    assert rows[0]["close"] == 1.5


def test_normalize_snapshot_schema(raw):
    s = normalize_snapshot(raw, "2026-08-14")
    assert s["ticker"] == "SPX"
    assert s["asof_date"] == "2026-08-14"
    assert s["levels"]["equilibrium_point"] == 7900.0
    assert s["levels"]["regime_threshold"] == 7820.63
    assert s["levels"]["upper_resistance_band"] == 7900.0
    # put wall = 181-365 day band (7500), all-expiry reference (max+net across all)
    assert s["levels"]["lower_support_band"] == 7500.0
    assert s["levels"]["put_wall_all"] == 7500.0
    assert s["levels"]["gamma_flip_all"] == 7820.63
    assert s["levels"]["gamma_flip_0dte"] == 7691.65
    assert s["levels"]["zero_dte_concentration"] == 7700.0
    assert s["levels"]["zero_dte_put_concentration"] == 7700.0
    assert s["levels"]["zero_dte_cumulative_crossing"] == pytest.approx(7824.75)
    # charm: always Mode A (directionalized volume); peak at 8000 -> Charm Peak
    assert s["charm_mode"] == "directionalized_volume"
    assert s["charm_qualification"]["label"] == "Charm Peak"
    assert s["charm_qualification"]["strike"] == 8000.0
    assert s["charm_qualification"]["mode"] == "directionalized_volume"
    assert s["charm_qualification"]["signed_net_charm"] == -50.0
    assert s["charm_qualification"]["qualification_passed"] is False
    # vanna peaks (directionalized volume): call peak 7850, put peak 7650
    assert s["vanna_mode"] == "directionalized_volume"
    assert s["vanna_peaks"]["call_peak"]["strike"] == 7850.0
    assert s["vanna_peaks"]["call_peak"]["signed_call_vanna"] == 15_000_000.0
    assert s["vanna_peaks"]["put_peak"]["strike"] == 7650.0
    assert s["vanna_peaks"]["put_peak"]["signed_put_vanna"] == 30_000_000.0
    # strike rows + regime (spot 7784 is below the OI flip 7820.63 -> Short/Amplifying, G4)
    assert len(s["strike_exposures"]) == 6
    assert s["strike_exposures_prev"] is not None
    assert s["net_gamma_total"] == 374.0
    assert s["regime"] == "Short Gamma — Amplifying"
    assert s["expected_move"] is not None
    assert s["quality"]["candle_data_available"] is True
    # DTE band walls: 6 rows (5 bands + ALL); 181-365 band put wall = 7500
    walls = s["dte_band_walls"]
    assert len(walls) == 6
    b18365 = next(w for w in walls if w["band"] == "181-365")
    assert b18365["put_wall"] == 7500.0
    assert walls[-1]["band"] == "ALL"
    assert s["current_level"] == raw["ohlc"]["data"][-1]["close"]
