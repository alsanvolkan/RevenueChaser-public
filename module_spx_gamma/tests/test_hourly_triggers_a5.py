"""A5: hourly-close trigger / confirm / reset for the directional scenarios (30 Oct report, "Üç senaryo")."""
from __future__ import annotations

from RevenueChaser.module_spx_gamma.hourly import (CONFIRM_STEP, RESET_STEP, hourly_closes,
                                                   scenario_triggers, spx_ratio)

BANDS = {"regime": "below_flip", "consolidation": (7600.0, 7700.0), "upside": (7700.0, 7775.0),
         "downside": (7500.0, 7600.0)}


def _bar(end_utc: str, close: float, market: str = "r") -> dict:
    return {"end_time": end_utc, "close": str(close), "market_time": market}


def _closes(*values):
    return [{"time": f"t{i}", "close": v} for i, v in enumerate(values)]


def test_steps_are_the_reports_25_points():
    assert CONFIRM_STEP == 25.0 and RESET_STEP == 25.0


def test_downside_trigger_then_confirm_on_a_later_close():
    out = scenario_triggers(BANDS, _closes(7610, 7590, 7570))
    down = out["Downside"]
    assert (down["trigger"], down["confirm"], down["reset"]) == (7600.0, 7575.0, 7625.0)
    assert down["state"] == "confirmed"
    assert down["triggered_at"] == "t1" and down["confirmed_at"] == "t2"


def test_confirm_needs_a_later_close_not_the_trigger_bar():
    # one close below both the trigger and the confirm level: triggered only
    assert scenario_triggers(BANDS, _closes(7560))["Downside"]["state"] == "triggered"


def test_reset_drops_a_trigger_before_confirm():
    out = scenario_triggers(BANDS, _closes(7590, 7630, 7580))
    down = out["Downside"]
    assert down["reset_at"] == "t1"
    assert down["state"] == "triggered" and down["triggered_at"] == "t2"


def test_reset_after_confirm_does_not_undo_it():
    assert scenario_triggers(BANDS, _closes(7590, 7570, 7640))["Downside"]["state"] == "confirmed"


def test_upside_mirrors_the_rule_on_the_flip():
    out = scenario_triggers(BANDS, _closes(7690, 7705, 7730))
    up = out["Upside"]
    assert (up["trigger"], up["confirm"], up["reset"]) == (7700.0, 7725.0, 7675.0)
    assert up["state"] == "confirmed"
    assert out["Downside"]["state"] == "waiting"


def test_no_bands_or_no_closes():
    assert scenario_triggers(None, _closes(7600)) is None
    out = scenario_triggers(BANDS, [])
    assert out["Downside"]["state"] == "waiting" and out["Upside"]["state"] == "waiting"


def test_spx_ratio_from_the_last_shared_session():
    bars = [_bar("2026-11-03T21:00:00Z", 754.33), _bar("2026-11-04T21:00:00Z", 766.66)]
    candles = [{"date": "2026-11-03", "close": 7543.3}, {"date": "2026-11-04", "close": 7666.6}]
    assert abs(spx_ratio(bars, candles) - 10.0) < 1e-9
    assert spx_ratio(bars, []) is None


def test_hourly_closes_keep_the_reports_half_hour_closes_of_the_target_week():
    # asof Wed 4 Nov: target Friday 6 Nov, week from Mon 2 Nov. NY winter time: :30 ends are 15:30Z...20:30Z,
    # the session close is 21:00Z. 30-minute bars ending on the hour (other than the close) are not hourly closes.
    bars = [
        _bar("2026-10-30T20:30:00Z", 750.0),   # last week: dropped
        _bar("2026-11-02T14:30:00Z", 760.0, "pr"),  # premarket: dropped
        _bar("2026-11-02T14:30:00Z", 761.0),   # a 09:30 end: never an hourly close
        _bar("2026-11-02T15:00:00Z", 761.0),   # first half hour: not an hourly close
        _bar("2026-11-02T15:30:00Z", 760.5),
        _bar("2026-11-02T16:00:00Z", 760.9),   # on the hour: dropped
        _bar("2026-11-02T21:00:00Z", 752.12),  # session close
        _bar("2026-11-03T15:30:00Z", 756.0),
    ]
    candles = [{"date": "2026-11-02", "close": 7521.2}]
    out = hourly_closes({"data": list(reversed(bars))}, "2026-11-04", candles)
    assert [r["time"] for r in out["rows"]] == ["2026-11-02T15:30:00Z", "2026-11-02T21:00:00Z",
                                                "2026-11-03T15:30:00Z"]
    assert abs(out["rows"][0]["close"] - 7605.0) < 1e-6
    assert out["rows"][0]["amsterdam"] == "Mon 16:30"
    assert out["ratio"] == 10.0 and out["week_start"] == "2026-11-02"


def test_hourly_closes_read_the_mcp_shape_too():
    bars = [{"end": "2026-11-03T15:30:00Z", "c": 756.0, "market": "r"},
            {"end": "2026-11-02T21:00:00Z", "c": 752.0, "market": "r"}]
    out = hourly_closes({"data": bars}, "2026-11-04", [{"date": "2026-11-02", "close": 7520.0}])
    assert [r["close"] for r in out["rows"]] == [7520.0, 7560.0]


def test_hourly_closes_none_without_a_ratio():
    assert hourly_closes({"data": [_bar("2026-11-03T15:30:00Z", 756.0)]}, "2026-11-04", []) is None
    assert hourly_closes(None, "2026-11-04", [{"date": "2026-11-02", "close": 1.0}]) is None


def test_build_scenarios_carries_the_trigger_line():
    from RevenueChaser.module_spx_gamma.scenarios import build_scenarios
    snap = {"current_level": 7650.0,
            "levels": {"regime_threshold": 7700.0, "upper_resistance_band": 7800.0, "lower_support_band": 7600.0},
            "charm_pin_report": {"strike": 7775.0},
            "hourly_closes": {"rows": [{"time": "a", "amsterdam": "Mon 16:30", "close": 7590.0},
                                       {"time": "b", "amsterdam": "Mon 17:30", "close": 7570.0}],
                              "ratio": 10.0, "week_start": "2026-11-02", "source": "SPY"}}
    by = {s["name"]: s for s in build_scenarios(snap)["scenarios"]}
    assert by["Downside"]["hourly"]["state"] == "confirmed"
    assert by["Downside"]["hourly_text"].startswith("CONFIRMED Mon 17:30 (triggered Mon 16:30)")
    assert "confirm < 7,575" in by["Downside"]["hourly_text"]
    assert by["Upside"]["hourly_text"].startswith("waiting")
    assert by["Consolidation"]["hourly_text"] == ""


def test_a_past_day_never_reads_a_later_sessions_close():
    bars = [_bar("2026-11-02T21:00:00Z", 752.0), _bar("2026-11-03T15:30:00Z", 756.0),
            _bar("2026-11-04T15:30:00Z", 766.0)]
    out = hourly_closes({"data": bars}, "2026-11-03", [{"date": "2026-11-02", "close": 7520.0}])
    assert [r["time"][:10] for r in out["rows"]] == ["2026-11-02", "2026-11-03"]


def test_a_bar_still_forming_is_left_out():
    from datetime import datetime, timezone
    bars = [_bar("2026-11-02T21:00:00Z", 752.0), _bar("2026-11-03T15:30:00Z", 756.0)]
    out = hourly_closes({"data": bars}, "2026-11-03", [{"date": "2026-11-02", "close": 7520.0}],
                        now=datetime(2026, 11, 3, 15, 10, tzinfo=timezone.utc))
    assert [r["time"] for r in out["rows"]] == ["2026-11-02T21:00:00Z"]


def test_the_13_00_bar_counts_only_on_a_short_day():
    """Fable #1: 12:30-13:00 is not a report time on a full day; on a 13:00 early close it is the close."""
    candles = [{"date": "2026-11-02", "close": 7520.0}]
    full_day = [_bar("2026-11-02T21:00:00Z", 752.0), _bar("2026-11-03T18:00:00Z", 756.0),
                _bar("2026-11-03T18:30:00Z", 757.0), _bar("2026-11-03T21:00:00Z", 758.0)]
    times = [r["time"] for r in hourly_closes({"data": full_day}, "2026-11-03", candles)["rows"]]
    assert "2026-11-03T18:00:00Z" not in times and "2026-11-03T18:30:00Z" in times
    short_day = [_bar("2026-11-02T21:00:00Z", 752.0), _bar("2026-11-03T17:30:00Z", 756.0),
                 _bar("2026-11-03T18:00:00Z", 757.0)]
    from datetime import datetime, timezone
    after = datetime(2026, 11, 3, 18, 45, tzinfo=timezone.utc)      # 13:45 NY, the 13:30 bar would exist by now
    times = [r["time"] for r in hourly_closes({"data": short_day}, "2026-11-03", candles, now=after)["rows"]]
    assert times[-1] == "2026-11-03T18:00:00Z"
    later_day = hourly_closes({"data": short_day}, "2026-11-04", candles)["rows"]
    assert later_day[-1]["time"] == "2026-11-03T18:00:00Z"            # a past day needs no fetch time


def test_a_13_00_bar_fetched_before_the_next_bar_is_not_a_close():
    """Fable delta #1: a full day fetched at 13:10 NY has the 13:00 bar as its last bar so far; it is no close."""
    from datetime import datetime, timezone
    candles = [{"date": "2026-11-02", "close": 7520.0}]
    so_far = [_bar("2026-11-02T21:00:00Z", 752.0), _bar("2026-11-03T17:30:00Z", 756.0),
              _bar("2026-11-03T18:00:00Z", 757.0)]
    early = datetime(2026, 11, 3, 18, 10, tzinfo=timezone.utc)      # 13:10 NY
    times = [r["time"] for r in hourly_closes({"data": so_far}, "2026-11-03", candles, now=early)["rows"]]
    assert "2026-11-03T18:00:00Z" not in times
    times = [r["time"] for r in hourly_closes({"data": so_far}, "2026-11-03", candles)["rows"]]
    assert "2026-11-03T18:00:00Z" not in times                       # same day, no fetch time: not known yet
