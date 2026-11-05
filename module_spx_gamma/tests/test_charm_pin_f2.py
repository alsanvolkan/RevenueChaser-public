"""F2 (Fable report review, 5 Nov 2026): the charm pin follows the 30 Oct report.

The report's pin is the strike of the largest front-expiry net charm (30 Oct p.6: charm -17.39M, pin 7800), read
from UW's strike-expiry rows, which carry ``call_charm`` and ``put_charm``. The board used to take the largest
|net charm| over all expiries in directionalized-volume mode, and let that "Charm Peak" set the upside band even
when it did not qualify. Real UW rows of 4 Nov for the 5 Nov expiry: the largest net charm is at 7750
(-14,286,262 + -5,316,117).
"""
from __future__ import annotations

from RevenueChaser.module_spx_gamma import charm_pin, scenarios
from RevenueChaser.module_spx_gamma.normalize import normalize_strike_expiry_rows
from .conftest import make_snapshot

UW_ROWS_2026_11_04 = {"data": [
    {"date": "2026-11-04", "expiry": "2026-11-05", "strike": "7750", "call_charm": -14286262.2318,
     "put_charm": -5316117.3702, "call_gex": 1, "put_gex": -1, "dte": 1},
    {"date": "2026-11-04", "expiry": "2026-11-05", "strike": "7725", "call_charm": -10190031.3263,
     "put_charm": -6163497.3907, "call_gex": 1, "put_gex": -1, "dte": 1},
    {"date": "2026-11-04", "expiry": "2026-11-05", "strike": "7600", "call_charm": 4790639.9014,
     "put_charm": 2793549.3221, "call_gex": 1, "put_gex": -1, "dte": 1},
]}
LATER = {"data": [
    {"expiry": "2026-11-06", "strike": "7800", "call_charm": -90e6, "put_charm": -1e6, "dte": 2},
]}


def test_strike_expiry_rows_carry_net_charm():
    rows = normalize_strike_expiry_rows([UW_ROWS_2026_11_04], "2026-11-04")
    top = [r for r in rows if r["strike"] == 7750.0][0]
    assert top["net_charm"] == -14286262.2318 + -5316117.3702


def test_report_pin_is_the_largest_front_expiry_net_charm():
    rows = normalize_strike_expiry_rows([UW_ROWS_2026_11_04, LATER], "2026-11-04")
    pin = charm_pin.report_charm_pin(rows, "2026-11-04")
    assert pin["strike"] == 7750.0                  # the later expiry's bigger charm does not count
    assert pin["expiry"] == "2026-11-05"
    assert pin["net_charm"] == -14286262.2318 + -5316117.3702


def test_report_pin_skips_an_expired_series():
    old = {"data": [{"expiry": "2026-11-03", "strike": "7000", "call_charm": -1e9, "put_charm": 0, "dte": -1}]}
    rows = normalize_strike_expiry_rows([old, UW_ROWS_2026_11_04], "2026-11-04")
    assert charm_pin.report_charm_pin(rows, "2026-11-04")["strike"] == 7750.0


def test_no_charm_columns_means_no_pin():
    rows = normalize_strike_expiry_rows([{"data": [{"expiry": "2026-11-05", "strike": "7750", "call_gex": 1,
                                                    "put_gex": -1}]}], "2026-11-04")
    assert charm_pin.report_charm_pin(rows, "2026-11-04")["strike"] is None


def test_upside_band_uses_the_report_pin_not_an_unqualified_peak():
    snap = {"current_level": 7580.0,
            "levels": {"regime_threshold": 7750.0, "upper_resistance_band": 8100.0, "lower_support_band": 7500.0},
            "charm_qualification": {"strike": 8200.0, "label": "Charm Peak"},
            "charm_pin_report": {"strike": 7800.0}}
    assert scenarios.scenario_bands(snap)["upside"] == (7750.0, 7800.0)


def test_only_an_unqualified_peak_gives_no_set():
    snap = {"current_level": 7580.0,
            "levels": {"regime_threshold": 7750.0, "upper_resistance_band": 8100.0, "lower_support_band": 7500.0},
            "charm_qualification": {"strike": 8200.0, "label": "Charm Peak"}}
    assert scenarios.scenario_bands(snap) is None


def test_snapshot_carries_the_report_pin():
    pin = make_snapshot()["charm_pin_report"]
    assert pin["strike"] == 7900.0 and pin["expiry"] == "2026-08-14"


def test_charm_card_shows_the_report_pin_first():
    from RevenueChaser.module_spx_gamma import summary
    snap = make_snapshot()
    card = summary.build_summary(snap)[3]
    labels = [r[0] for r in card["rows"]]
    assert labels.index("Report pin") < labels.index("Where it matters")
    row = card["rows"][labels.index("Report pin")]
    assert row[1] == "$7,900" and "front expiry 2026-08-14" in row[2] and "not a confirmed attractor" in row[2]


SAME_DAY = {"data": [
    {"date": "2026-11-04", "expiry": "2026-11-04", "strike": "7650", "call_charm": -90e6, "put_charm": -9e6,
     "call_gex": 1, "put_gex": -1, "dte": 0},
]}


def test_the_asof_days_own_expiry_is_dead_after_its_close():
    """Real UW check 5 Nov: a Fetch of 4 Nov carried the 4 Nov 0DTE rows, and the pin came from that expired series
    (7650, below the flip), so the board printed no scenario set. After the close the front is 5 Nov (7750)."""
    from datetime import datetime, timezone
    rows = normalize_strike_expiry_rows([SAME_DAY, UW_ROWS_2026_11_04], "2026-11-04")
    after_close = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    pin = charm_pin.report_charm_pin(rows, "2026-11-04", fetched_at=after_close)
    assert pin["strike"] == 7750.0 and pin["expiry"] == "2026-11-05"
    in_session = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)        # 13:00 New York, the 0DTE still trades
    assert charm_pin.report_charm_pin(rows, "2026-11-04", fetched_at=in_session)["strike"] == 7650.0


def test_normalize_passes_the_fetch_time_to_the_pin():
    from RevenueChaser.module_spx_gamma.normalize import normalize_snapshot
    from .conftest import make_raw
    raw = make_raw()
    assert normalize_snapshot(raw, "2026-08-14")["charm_pin_report"]["expiry"] == "2026-08-14"
    raw["_meta"] = {"api_snapshot_time": "2026-08-15T12:00:00Z"}
    assert normalize_snapshot(raw, "2026-08-14")["charm_pin_report"]["expiry"] != "2026-08-14"
