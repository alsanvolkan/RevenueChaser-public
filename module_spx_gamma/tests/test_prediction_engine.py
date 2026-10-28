"""Tests for the predictive scenario & directional engine (spec §4, §7)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from RevenueChaser.module_spx_gamma import render
from RevenueChaser.module_spx_gamma.config import RAW_ROOT
from RevenueChaser.module_spx_gamma.engines import prediction_engine as pe
from RevenueChaser.module_spx_gamma.normalize import normalize_snapshot
from .conftest import make_raw, make_snapshot


def _long_gamma_raw() -> dict:
    raw = make_raw()
    raw["gex_levels"]["data"]["gamma_flip"] = "7700"
    # A stored 0-1 DTE slice (PR 3): without one there is no squeeze trigger (avo, 28 Oct 2026), and a strike it has
    # no row for holds $0 (review SF1). -$300M at 7800 is past the $200M squeeze threshold.
    raw["zero_one_dte"] = {"stored": True, "dollars": {7800.0: -300.0e6, 7850.0: 1.0e9},
                           "label": "0-1 DTE fixture slice"}
    return raw


def _long_gamma_snapshot() -> dict:
    """The shared fixture sits below its flip (short gamma, G4); these tests want long gamma."""
    return normalize_snapshot(_long_gamma_raw(), "2026-08-14")


@pytest.fixture
def snapshot():
    return _long_gamma_snapshot()

# Verifiable target relationships from the synthetic fixture (conftest.make_raw):
#   - spot above the OI gamma flip (long gamma, flip moved to 7700) -> gamma term +1 -> BULLISH DRIFT band
#   - max positive 0-1DTE GEX sits at 7800 (0DTE) with 1DTE < 0 -> EXPIRING_PIN_FLIP
#   - first strike above spot with negative near-DTE GEX and |dollar gamma| > $200M = 7800
#   - max positive vanna below spot = 7650 -> downside floor
#   - spot exposure OI/vol gamma: 8000 (both < 0 -> FORTIFYING), 7900 (OI<0, Vol>0 -> DECAYING)


def test_synthetic_directional_bias(snapshot):
    p = snapshot["prediction"]
    assert p["bias"] == "BULLISH DRIFT"
    assert p["s_dir"] is not None and p["s_dir"] >= 0.35


def test_synthetic_pin_and_flip(snapshot):
    p = snapshot["prediction"]
    assert p["active_pin"] == 7800.0
    assert p["expiring_pin_flip"] is True
    assert p["pin_flag"] == "EXPIRING_PIN_FLIP"


def test_synthetic_upside_trigger(snapshot):
    p = snapshot["prediction"]
    assert p["upside_trigger"] == 7800.0
    assert p["upside_trigger_net_gex"] == -300.0e6, "sized by its 0-1 DTE dollars, not the per-unit grid"


def test_no_upside_trigger_at_a_strike_the_stored_slice_has_no_row_for():
    """Review SF1: a strike missing from a stored 0-1 DTE slice holds $0 of 0-1 DTE gamma: below $200M, no trigger.
    (The old rule fired there, sized by the per-unit grid.)"""
    raw = _long_gamma_raw()
    raw["zero_one_dte"]["dollars"] = {7850.0: 1.0e9}
    assert normalize_snapshot(raw, "2026-08-14")["prediction"]["upside_trigger"] is None


def test_synthetic_downside_vanna_floor(snapshot):
    p = snapshot["prediction"]
    assert p["downside_floor"] == 7650.0
    assert p["vanna_floor"] == 7650.0


def test_synthetic_divergence_flags(snapshot):
    div = {d["strike"]: d for d in snapshot["prediction"]["divergence"]}
    assert div[8000.0]["flag"] == "WALL_FORTIFYING"
    assert div[7900.0]["flag"] == "WALL_DECAYING"
    assert div[7900.0]["gex_oi"] < 0
    assert div[7900.0]["gex_vol"] > 0


def test_scenario_rows_complete(snapshot):
    rows = snapshot["prediction"]["scenarios"]
    assert [r["name"] for r in rows] == [
        "Bullish Acceleration", "Consolidation", "Bearish Breakdown"]
    assert all(r["trigger"] != "" for r in rows)
    assert rows[0]["tone"] == "bullish"
    assert rows[1]["tone"] == "neutral"
    assert rows[2]["tone"] == "bearish"


def test_confidence_heuristic_bounded(snapshot):
    conf = snapshot["prediction"]["confidence"]
    assert conf is None or 0.0 <= conf <= 100.0
    assert snapshot["prediction"]["note"]
    assert "NOT a calibrated forecast" in snapshot["prediction"]["note"]


def test_missing_data_degrades_to_note():
    snap = make_snapshot()
    snap["current_level"] = None
    p = pe.generate_prediction_matrix(snap)
    assert p["bias"] is None
    assert p["s_dir"] is None
    assert "Insufficient" in p["note"]


def test_directional_score_bands():
    base = _long_gamma_snapshot()
    assert pe.directional_score(base)["bias"] == "BULLISH DRIFT"
    bear = _long_gamma_snapshot()
    bear["regime_detail"] = {"state": "short"}
    bear["spot_exposures"] = [{**r, "call_charm_vol": (r.get("call_charm_vol") or 0) - 200,
                               "put_charm_vol": (r.get("put_charm_vol") or 0) - 200}
                              for r in bear["spot_exposures"]]
    assert pe.directional_score(bear)["bias"] == "BEARISH BREAKDOWN"


def test_pin_rejects_far_otm_peak(snapshot):
    """A huge positive 0DTE peak far above spot must never become the pin; the
    strongest positive magnet inside the ATM window wins instead."""
    snap = make_snapshot()
    spot = snap["current_level"]  # 7784
    rows = [r for r in snap["strike_expiry_rows"] if r.get("strike") != 7800.0]
    rows += [
        {"expiry": "2026-08-14", "dte": 0.0, "strike": 8000.0, "call_gex": 1000.0, "put_gex": -1.0, "net_gamma": 999.0},
        {"expiry": "2026-08-14", "dte": 0.0, "strike": 7790.0, "call_gex": 60.0, "put_gex": -10.0, "net_gamma": 50.0},
    ]
    snap["strike_expiry_rows"] = rows
    p = pe.generate_prediction_matrix(snap)
    assert p["active_pin"] == 7790.0
    assert p["pin_source"] == "grid_magnet"
    assert abs(p["active_pin"] - spot) <= 25.0


def test_pin_falls_back_to_equilibrium_when_atm_short_gamma(snapshot):
    """With a short-gamma ATM (no positive magnet near spot) the consolidation
    anchor falls back to the equilibrium level so the scenario bands stay tied
    to live spot instead of a far-away GEX peak."""
    snap = make_snapshot()
    spot = snap["current_level"]  # 7784
    rows = [r for r in snap["strike_expiry_rows"] if r.get("strike") != 7800.0]
    rows += [
        {"expiry": "2026-08-14", "dte": 0.0, "strike": 8000.0, "call_gex": 1000.0, "put_gex": -1.0, "net_gamma": 999.0},
        {"expiry": "2026-08-14", "dte": 0.0, "strike": 7790.0, "call_gex": -100.0, "put_gex": -10.0, "net_gamma": -110.0},
        {"expiry": "2026-08-14", "dte": 0.0, "strike": 7780.0, "call_gex": -50.0, "put_gex": -20.0, "net_gamma": -70.0},
    ]
    snap["strike_expiry_rows"] = rows
    snap["levels"] = {**snap.get("levels", {}), "equilibrium_point": round(spot)}
    p = pe.generate_prediction_matrix(snap)
    assert p["active_pin"] == round(spot)
    assert p["pin_source"] == "equilibrium_fallback"
    cons = [r for r in p["scenarios"] if r["name"] == "Consolidation"][0]
    assert "7,779" in cons["trigger"]
    assert "n/a" not in cons["trigger"]
    # The far-away peak must never leak into the bearish row's trigger.
    bear = [r for r in p["scenarios"] if r["name"] == "Bearish Breakdown"][0]
    assert "8,000" not in bear["trigger"]


# ── spec §7: pass historical UW raw JSON files through the engine ──

_FILE_KEY = {
    "gex_levels": "gex_levels",
    "gex_levels_vol": "gex_levels_vol",
    "greek_exposure_strike": "strike_exposure",
    "greek_exposure_strike_prev": "strike_exposure_prev",
    "spot_exposures_strike": "spot_exposure",
    "zero_dte_strike_expiry": "strike_expiry",
    "ohlc_8wk": "ohlc",
}


def _load_raw_dir(day: str) -> dict:
    """Assemble a raw payload dict from the cached per-date UW JSON files."""
    d = RAW_ROOT / day
    raw: dict = {}
    for f in d.glob("*.json"):
        key = _FILE_KEY.get(f.stem)
        if key:
            raw[key] = json.loads(f.read_text(encoding="utf-8"))
    if not raw.get("ohlc"):
        raise FileNotFoundError(f"no OHLC snapshot cached for {day}")
    sea: dict = {}
    for f in sorted(d.glob("strike_expiry_*.json")):
        payload = json.loads(f.read_text(encoding="utf-8"))
        rows = payload.get("data") or []
        expiry = rows[0].get("expiry") if rows else f.stem
        sea[expiry] = payload
    raw["strike_expiry_all"] = sea
    return raw


@pytest.mark.parametrize("day", [
    "2026-09-02",
    "2026-08-31",
    "2026-08-27",
], ids=lambda d: d)
def test_historical_raw_predictions_consistent(day):
    # Content, not existence: a failed fetch used to leave an empty directory
    # behind, which passed an exists() check and turned "no cached data" into a
    # hard error instead of a skip.
    if not any((RAW_ROOT / day).glob("*.json")):
        pytest.skip(f"no cached raw snapshot for {day}")
    """Run real cached raw payloads through the full pipeline; the extracted
    targets must match the actual Gamma/Vanna extremes in the same snapshot."""
    snap = normalize_snapshot(_load_raw_dir(day), day)
    p = snap["prediction"]
    assert p["s_dir"] is not None, day

    # Pin must equal the max positive 0-1 DTE net-GEX strike INSIDE the ATM
    # window around spot; when the ATM is short-gamma (no positive magnet near
    # spot) it must fall back to the equilibrium/spot anchor so the neutral band
    # stays tied to live price action.
    from RevenueChaser.module_spx_gamma.config import PIN_ATM_WINDOW_PTS, CONSOLIDATION_BAND_PTS
    spot = snap["current_level"]
    near = [r for r in snap["strike_expiry_rows"]
            if (r.get("dte") if r.get("dte") is not None else 0.0) <= 1
            and r.get("net_gamma") is not None]
    positives = [r for r in near if (r["net_gamma"] or 0.0) > 0]
    if p["active_pin"] is not None:
        atm_pos = [r for r in positives if abs(r["strike"] - spot) <= PIN_ATM_WINDOW_PTS]
        if atm_pos:
            expected = max(atm_pos, key=lambda r: r["net_gamma"])["strike"]
            assert p["active_pin"] == expected, day
        else:
            equilibrium = (snap.get("levels") or {}).get("equilibrium_point")
            if equilibrium is not None and abs(equilibrium - spot) <= PIN_ATM_WINDOW_PTS:
                assert p["active_pin"] == equilibrium, day
            else:
                assert p["active_pin"] == round(spot / CONSOLIDATION_BAND_PTS) * CONSOLIDATION_BAND_PTS, day
        # The pin must sit within the ATM window of spot (never a far-OTM peak).
        assert abs(p["active_pin"] - spot) <= PIN_ATM_WINDOW_PTS, day
    elif positives:
        # No pin may be reported only when no positive magnet sits near spot.
        assert not [r for r in positives if abs(r["strike"] - spot) <= PIN_ATM_WINDOW_PTS], day

    # Vanna floor must be the max-positive net-vanna strike below spot.
    if p["vanna_floor"] is not None:
        below = [r for r in snap["spot_exposures"]
                 if r["strike"] < snap["current_level"]
                 and ((r.get("call_vanna_vol") or 0) + (r.get("put_vanna_vol") or 0)) > 0]
        expected = max(below, key=lambda r: (r.get("call_vanna_vol") or 0) + (r.get("put_vanna_vol") or 0))["strike"]
        assert p["vanna_floor"] == expected, day

    # Divergence flags must match the OI/vol gamma signs at each strike.
    by_strike = {r["strike"]: r for r in snap["spot_exposures"]}
    for d in p["divergence"]:
        r = by_strike[d["strike"]]
        g_oi = (r.get("call_gamma_oi") or 0) + (r.get("put_gamma_oi") or 0)
        g_vol = (r.get("call_gamma_vol") or 0) + (r.get("put_gamma_vol") or 0)
        assert g_oi < 0, day
        assert (g_vol > 0 and d["flag"] == "WALL_DECAYING") or \
               (g_vol < 0 and d["flag"] == "WALL_FORTIFYING"), day


# ── spec §7: UI integration smoke (hero banner + scenario matrix + CSS vars) ──

def test_html_injects_prediction_banner(snapshot):
    html = render.render_html(snapshot)
    # Hero banner + bias badge
    assert 'class="prediction-container"' in html
    assert 'bias-badge bullish' in html
    assert "EXPECTED DIRECTION: BULLISH DRIFT" in html
    assert "Active Pin" in html and "Accel Trigger" in html and "Bounce Floor" in html
    # Scenario matrix rows
    assert 'class="scenario-table stack"' in html
    assert "row-bullish" in html and "row-neutral" in html and "row-bearish" in html
    assert "Bullish Acceleration" in html and "Consolidation" in html and "Bearish Breakdown" in html
    # Divergence status items
    assert 'class="divergence-bar"' in html
    assert "FORTIFYING" in html and "DECAYING" in html
    assert "OI:" in html and "Vol:" in html
    # CSS variables must survive so the iframe never breaks styling
    # (the palette is the shell's: reporting_common.THEME_CSS, dark by default; the board's names alias it)
    assert "--pos:#34D399" in html and "--neg:#F87171" in html and "--amber:#FBBF24" in html
    assert "--green:var(--pos)" in html and "--red:var(--neg)" in html
    assert "--bg:#05111F" in html


def test_html_prediction_absent_degrades():
    snap = make_snapshot()
    snap["prediction"] = {}
    html = render.render_html(snap)
    assert 'class="prediction-container"' not in html
    assert "EXPECTED DIRECTION" not in html

def test_prediction_cache_written_without_api_duplication(tmp_path, monkeypatch, stored_make_raw_window):
    """A cached prediction is written once alongside the snapshot; re-serving it
    (Refresh Tab) never invokes the UW client again."""
    from RevenueChaser.module_spx_gamma import run as spx_run

    api_calls = {"count": 0}

    class _CountingClient:
        def get(self, path, params=None):
            api_calls["count"] += 1
            return {"data": []}

    monkeypatch.setattr(spx_run, "_backfill_history", lambda *a, **k: None)
    monkeypatch.setattr(spx_run, "UWClient", lambda *a, **k: _CountingClient())
    monkeypatch.setattr(spx_run, "fetch_snapshot", lambda *a, **k: _long_gamma_raw())
    monkeypatch.setattr(spx_run, "render_html", lambda *a, **k: "<html>prediction</html>")
    monkeypatch.setattr(spx_run, "render_markdown", lambda *a, **k: "# md")
    monkeypatch.setattr(spx_run, "_record_history", lambda *a, **k: None)

    class _FakeFig:
        @staticmethod
        def write_html(*a, **k):
            pass

        @staticmethod
        def to_html(*a, **k):
            return ""

    def _fake_position_map(*a, **k):
        return _FakeFig()

    monkeypatch.setattr(spx_run, "_export_chart", lambda fig, d, n: (None, None, ""))
    monkeypatch.setattr(spx_run.position_map, "build", _fake_position_map)
    monkeypatch.setattr(spx_run, "build_tornado_fig", lambda *a: _FakeFig())
    monkeypatch.setattr(spx_run.position_map, "build_envelope_fig", lambda *a: _FakeFig())

    # Run the pipeline once -> prediction cached.
    spx_run.run("2026-08-14", data_root=tmp_path, token="x")
    pred_path = tmp_path / "parsed" / "2026-08-14" / "prediction.json"
    assert pred_path.exists()
    pred = json.loads(pred_path.read_text(encoding="utf-8"))
    assert pred.get("bias") == "BULLISH DRIFT"
    assert api_calls["count"] == 0, "the mocked snapshot is already cached data"

    # Refresh Tab = serve the cached report; the engine must derive the SAME
    # prediction from the cached snapshot without any new API traffic.
    cached_snap = json.loads((tmp_path / "parsed" / "2026-08-14" / "snapshot.json").read_text(encoding="utf-8"))
    regenerated = pe.generate_prediction_matrix(cached_snap)
    assert regenerated["bias"] == pred["bias"]
    assert regenerated["active_pin"] == pred["active_pin"]
    assert api_calls["count"] == 0
