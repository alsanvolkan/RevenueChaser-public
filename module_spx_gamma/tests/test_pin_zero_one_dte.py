"""Review S4 (PR 3): the $10B pin and the $200M squeeze tests size a strike by its 0-1 DTE dollar gamma.

The magnet/trigger geometry (which strike, the +-25 and +-50 point windows, the per-unit 0-1 DTE grid) and the two
thresholds are unchanged (decision A2); only the dollars behind them move from the all-expiry
``/spot-exposures/strike`` rows to ``spx_gamma_expiry_strike`` summed over 0 and 1 DTE. The all-expiry number stays
on the payload as context, never as the basis of a flag.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.module_spx_gamma import render, store_reads
from RevenueChaser.module_spx_gamma.config import PIN_MAGNET_MIN_DOLLARS, UPSIDE_TRIGGER_MIN_MAGNITUDE
from RevenueChaser.module_spx_gamma.engines.prediction_engine import generate_prediction_matrix, scenario_extraction
from RevenueChaser.module_spx_gamma.normalize import normalize_snapshot

from . import rcdata_fixture as fx
from .conftest import make_raw

SPOT = 6800.0
MAGNET = 6810.0
TRIGGER = 6820.0


def _snapshot(zero_one=None, all_expiry=None) -> dict:
    """A grid with a positive 0DTE magnet at 6810 and a negative strike at 6820, both near spot 6800.

    ``all_expiry`` and ``zero_one`` give each strike's dollars on the old and the new basis.
    """
    all_expiry = all_expiry or {}
    return {
        "current_level": SPOT,
        "levels": {},
        "strike_expiry_rows": [
            {"expiry": "2026-10-26", "strike": MAGNET, "dte": 0, "net_gamma": 500.0},
            {"expiry": "2026-10-26", "strike": TRIGGER, "dte": 0, "net_gamma": -300.0},
            {"expiry": "2026-10-26", "strike": 6790.0, "dte": 0, "net_gamma": 100.0},
        ],
        "spot_exposures": [{"strike": k, "call_gamma_oi": v, "put_gamma_oi": 0.0} for k, v in all_expiry.items()],
        "zero_one_dte_dollars": zero_one,
    }


def test_a_big_all_expiry_number_no_longer_raises_the_pin_when_the_0_1_dte_sum_is_small():
    scen = scenario_extraction(_snapshot(zero_one={MAGNET: 4e9}, all_expiry={MAGNET: 12e9}))
    assert scen["pin_magnet"] == MAGNET
    assert scen["pin_flag"] is None
    assert scen["pin_dollars"] == 4e9 and scen["pin_dollars_all_expiry"] == 12e9


def test_a_big_0_1_dte_sum_raises_the_pin_whatever_the_all_expiry_number():
    scen = scenario_extraction(_snapshot(zero_one={MAGNET: 11e9}, all_expiry={MAGNET: 2e9}))
    assert scen["pin_flag"] == "ACTIVE_PIN" and scen["pin_dollars"] == 11e9
    assert scen["pin_dollars"] >= PIN_MAGNET_MIN_DOLLARS


def test_the_squeeze_trigger_is_sized_by_0_1_dte_dollars():
    small_new = scenario_extraction(_snapshot(zero_one={MAGNET: 1e9, TRIGGER: -100e6},
                                              all_expiry={TRIGGER: -900e6}))
    assert small_new["upside_trigger"] is None, "the all-expiry $900M is context, not the trigger's size"
    assert small_new["upside_trigger_dollars_all_expiry"] is None
    big_new = scenario_extraction(_snapshot(zero_one={MAGNET: 1e9, TRIGGER: -300e6},
                                            all_expiry={TRIGGER: -50e6}))
    assert big_new["upside_trigger"] == TRIGGER and big_new["upside_trigger_net_gex"] == -300e6
    assert abs(big_new["upside_trigger_net_gex"]) > UPSIDE_TRIGGER_MIN_MAGNITUDE
    assert big_new["upside_trigger_dollars_all_expiry"] == -50e6


def test_a_missing_slice_raises_no_pin_even_when_the_all_expiry_number_is_huge():
    scen = scenario_extraction(_snapshot(zero_one=None, all_expiry={MAGNET: 50e9}))
    assert scen["pin_magnet"] == MAGNET and scen["pin_flag"] is None and scen["pin_dollars"] is None
    assert scen["pin_dollars_all_expiry"] == 50e9


def test_the_prediction_payload_names_both_numbers():
    pred = generate_prediction_matrix(_snapshot(zero_one={MAGNET: 11e9}, all_expiry={MAGNET: 2e9}))
    assert (pred["pin_magnet"], pred["pin_dollars"], pred["pin_dollars_all_expiry"]) == (MAGNET, 11e9, 2e9)
    assert pred["pin_flag"] == "ACTIVE_PIN"


# ── through the store: the Friday and the not-stored line ──

@pytest.fixture
def after_monday_close(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc))


def test_a_friday_sums_the_monday_expiry_as_1_dte_into_the_pin(after_monday_close):
    f, mon = fx.FRIDAY, fx.LAST
    fx.serve(spx={f: [fx.uw_row(f, f, MAGNET, 4e9, -1e9), fx.uw_row(f, mon, MAGNET, 8e9, -0.5e9)]})
    dollars = store_reads.zero_one_dte_dollars(f)["dollars"]
    assert dollars == {MAGNET: 10.5e9}
    scen = scenario_extraction(_snapshot(zero_one=dollars))
    assert scen["pin_flag"] == "ACTIVE_PIN", "the Monday expiry is 1 DTE on a Friday"


def _raw_on(day: str, zero_one: dict) -> dict:
    raw = make_raw()
    raw["zero_one_dte"] = zero_one
    raw["store_inputs"] = {"zero_one_dte": {k: v for k, v in zero_one.items() if k != "dollars"}}
    return raw


def test_a_missing_slice_shows_the_not_stored_line_and_no_flag(after_monday_close):
    missing = store_reads.zero_one_dte_dollars("2026-08-14")
    snap = normalize_snapshot(_raw_on("2026-08-14", missing), "2026-08-14")
    assert snap["prediction"]["pin_flag"] in (None, "EXPIRING_PIN_FLIP")
    assert snap["prediction"]["pin_dollars"] is None
    html = render.render_html(snap)
    assert "0-1 DTE not stored for 2026-08-14" in html


def test_a_stored_slice_names_its_snapshot_on_the_pin_line(after_monday_close):
    s = fx.LAST
    fx.serve(spx={s: [fx.uw_row(s, s, 7800, 3e9, -1e9)]})
    snap = normalize_snapshot(_raw_on("2026-10-26", store_reads.zero_one_dte_dollars(s)), "2026-10-26")
    html = render.render_html(snap)
    assert "0-1 DTE from the 2026-10-26 2026-10-26T20:15:00Z snapshot" in html
    assert "$2.00B" in html, "the magnet's 0-1 DTE size"


def test_a_snapshot_built_without_the_store_has_no_pin_line():
    """The goldens' snapshot (no store attach) renders as before."""
    html = render.render_html(normalize_snapshot(make_raw(), "2026-08-14"))
    assert "0-1 DTE" not in html.split("<body>", 1)[1].split("Scenario Matrix", 1)[0]
