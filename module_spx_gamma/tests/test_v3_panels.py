"""Tests for the new API-derived panels: max pain, IV rank, vol board, VIX, CFTC, skew."""
from __future__ import annotations

import pytest

from RevenueChaser.module_spx_gamma import normalize as nm
from RevenueChaser.module_spx_gamma.cftc import _map_row
from RevenueChaser.module_spx_gamma.vix import vix_term_mode


# ── max pain ──
def test_max_pain_rows_with_distance():
    payload = {"data": [
        {"expiry": "2026-09-18", "max_pain": "7470"},
        {"expiry": "2026-10-16", "max_pain": "7405"},
    ]}
    rows = nm.normalize_max_pain(payload, spot=7711.74)
    assert len(rows) == 2
    assert rows[0]["expiry"] == "2026-09-18"
    assert rows[0]["max_pain"] == 7470.0
    # (7470 - 7711.74)/7711.74 = -3.13%
    assert abs(rows[0]["distance_pct"] - (-3.13)) < 0.01


def test_max_pain_empty():
    assert nm.normalize_max_pain(None, spot=7711.74) == []
    assert nm.normalize_max_pain({"data": []}, spot=7711.74) == []


# ── IV rank ──
def test_iv_rank_takes_newest():
    payload = {"data": [
        {"date": "2026-08-24", "iv_rank_1y": "16.7369", "volatility": "0.13"},
        {"date": "2026-08-25", "iv_rank_1y": "15.5", "volatility": "0.128"},
    ]}
    r = nm.normalize_iv_rank(payload)
    assert r["date"] == "2026-08-25"  # last (oldest-first series)
    assert r["iv_rank_1y"] == pytest.approx(15.5)


def test_iv_rank_missing():
    assert nm.normalize_iv_rank(None) is None


# ── options volume / P/C ──
def test_options_volume_pc_ratio():
    payload = {"data": [{"date": "2026-08-28", "call_volume": "171049", "put_volume": "260424",
                         "net_call_premium": "123848260.0", "net_put_premium": "35089636.0"}]}
    r = nm.normalize_options_volume(payload)
    assert r["pc_ratio"] == pytest.approx(260424 / 171049)
    assert r["call_volume"] == 171049
    assert r["net_call_premium"] == pytest.approx(123848260.0)


# ── vol term ──
def test_vol_term_sorted_by_dte():
    payload = {"data": [
        {"expiry": "2026-10-16", "dte": "49", "volatility": "0.125", "implied_move_perc": "0.031"},
        {"expiry": "2026-09-18", "dte": "21", "volatility": "0.11", "implied_move_perc": "0.017"},
    ]}
    rows = nm.normalize_vol_term(payload)
    assert rows[0]["expiry"] == "2026-09-18"  # lower DTE first
    assert rows[1]["dte"] == 49.0


# ── VRP / realized ──
def test_vrp_newest():
    payload = {"data": [
        {"date": "2025-09-29", "risk_premium": "0.042169", "rank": "0.831403"},
        {"date": "2025-09-30", "risk_premium": "0.068086", "rank": "0.888487"},
    ]}
    r = nm.normalize_vrp(payload)
    assert r["risk_premium"] == pytest.approx(0.068086)  # last = newest (oldest-first)
    assert r["date"] == "2025-09-30"


def test_realized_vol_spread():
    payload = {"data": [{"date": "2025-09-29", "implied_volatility": "0.121000",
                         "realized_volatility": "0.075686"}]}
    r = nm.normalize_realized_vol(payload)
    assert r["iv_minus_rv"] == pytest.approx(0.045314)


# ── skew (derived, unverified) ──
def test_skew_from_twin_blocks():
    payload = {"data": [
        {"days": 1, "volatility": "0.09"}, {"days": 1, "volatility": "0.062"},
        {"days": 30, "volatility": "0.119"}, {"days": 30, "volatility": "0.115"},
        {"days": 90, "volatility": "0.14"}, {"days": 90, "volatility": "0.138"},
    ]}
    s = nm.normalize_skew(payload)
    assert s is not None
    assert s["derived_unverified"] is True
    assert s["put_bias_1m"] == pytest.approx(0.115 - 0.119)  # put block - call block
    assert s["put_bias_3m"] == pytest.approx(0.138 - 0.14)


def test_skew_returns_none_with_single_block():
    payload = {"data": [{"days": 30, "volatility": "0.119"}]}
    assert nm.normalize_skew(payload) is None


# ── VIX ──
def test_vix_normalize():
    payload = {"tickers": {"^VIX": 14.43, "^VIX3M": 17.48, "^VVIX": 86.63}, "term_mode": "contango"}
    v = nm.normalize_vix(payload)
    assert v["vix"] == 14.43
    assert v["term_mode"] == "contango"


def test_vix_term_mode():
    assert vix_term_mode({"^VIX": 14.43, "^VIX3M": 17.48}) == "contango"
    assert vix_term_mode({"^VIX": 20.0, "^VIX3M": 17.48}) == "backwardation"
    assert vix_term_mode({"^VIX": 14.43, "^VIX3M": None}) is None


# ── CFTC ──
def test_cftc_map_row():
    row = {
        "report_date_as_yyyy_mm_dd": "2026-08-25",
        "open_interest_all": "2045669",
        "asset_mgr_positions_long": "1166084", "asset_mgr_positions_short": "212856",
        "lev_money_positions_long": "150526", "lev_money_positions_short": "465730",
        "dealer_positions_long_all": "207277", "dealer_positions_short_all": "968313",
        "change_in_asset_mgr_long": "-7338", "change_in_lev_money_long": "-33802",
        "change_in_dealer_long_all": "23379",
    }
    r = _map_row(row)
    assert r["asset_manager"]["net"] == pytest.approx(1166084 - 212856)
    assert r["dealer"]["net"] == pytest.approx(207277 - 968313)
    assert r["leveraged_funds"]["change"] == pytest.approx(-33802)
    assert r["report_date"] == "2026-08-25"


def test_cftc_map_row_empty():
    assert _map_row({}) is None


def test_normalize_cftc_shape():
    payload = {"report_date": "2026-08-25", "open_interest": 2045669,
               "asset_manager": {"long": 1, "short": 2, "net": -1, "change": 0},
               "leveraged_funds": {"long": 1, "short": 2, "net": -1, "change": 0},
               "dealer": {"long": 1, "short": 2, "net": -1, "change": 0}}
    c = nm.normalize_cftc(payload)
    assert c["rows"][0]["group"] == "Asset Manager"
    assert len(c["rows"]) == 3
