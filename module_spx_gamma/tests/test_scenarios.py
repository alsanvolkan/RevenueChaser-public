"""Tests for the scenario set: regime-dependent bands, the report's probabilities, asymmetry."""
from __future__ import annotations

from RevenueChaser.module_spx_gamma import scenarios
from .conftest import make_snapshot


def test_three_scenarios(snapshot):
    s = scenarios.build_scenarios(snapshot)
    assert len(s["scenarios"]) == 3
    assert [x["name"] for x in s["scenarios"]] == ["Consolidation", "Upside", "Downside"]


def test_probabilities_sum_to_one(snapshot):
    s = scenarios.build_scenarios(snapshot)
    total = sum(x["p"] for x in s["scenarios"])
    assert abs(total - 1.0) < 1e-9


def test_only_the_directional_classes_carry_asymmetry(snapshot):
    s = scenarios.build_scenarios(snapshot)
    for x in s["scenarios"]:
        if x["name"] == "Consolidation":
            assert x["asymmetry"] is None, "the report prints 'uygulanmaz' for the directionless class"
        else:
            assert x["asymmetry"] is not None


def test_no_expected_value_is_published(snapshot):
    """The reports state outright that expected value is not computed; the old R:R/Expectancy columns are gone."""
    s = scenarios.build_scenarios(snapshot)
    for x in s["scenarios"]:
        assert "expectancy" not in x and "rr" not in x
    assert not hasattr(scenarios, "expectancy")


def test_asymmetry_reproduces_the_30_oct_reports_own_worked_example():
    """The pinned report's §9: spot 7,580.09, flip 7,750, put support 7,500, charm pin 7,800.

    It publishes Consolidation 7500-7750, Upside 7750-7800, Downside 7400-7500, probabilities 0,35/0,30/0,35, and
    asymmetry 0,2 up and 0,4 down. Those are the numbers this module has to print.
    """
    snap = {"current_level": 7580.09,
            "levels": {"regime_threshold": 7750.0, "upper_resistance_band": 8100.0, "lower_support_band": 7500.0},
            "charm_qualification": {"strike": 7800.0}}
    bands = scenarios.scenario_bands(snap)
    assert bands["regime"] == scenarios.NEGATIVE_GAMMA
    assert bands["consolidation"] == (7500.0, 7750.0)
    assert bands["upside"] == (7750.0, 7800.0)
    assert bands["downside"] == (7400.0, 7500.0)
    rows = {x["name"]: x for x in scenarios.build_scenarios(snap)["scenarios"]}
    assert [rows[n]["p"] for n in ("Consolidation", "Upside", "Downside")] == [0.35, 0.30, 0.35]
    assert abs(rows["Upside"]["asymmetry"] - 0.2) < 1e-9
    assert abs(rows["Downside"]["asymmetry"] - 0.4) < 1e-9


def test_above_the_flip_keeps_the_9_oct_rule_and_its_probabilities():
    """The pinned report does not cover a spot above the flip, so that regime keeps the older rule."""
    snap = {"current_level": 7900.0,
            "levels": {"regime_threshold": 7770.0, "upper_resistance_band": 7920.0, "lower_support_band": 7500.0},
            "charm_qualification": {"strike": 8000.0}}
    bands = scenarios.scenario_bands(snap)
    assert bands["regime"] == scenarios.POSITIVE_GAMMA
    assert bands["consolidation"] == (7770.0, 7940.0)      # [flip, call wall + 20]
    assert bands["upside"] == (7940.0, 8020.0)             # [call wall + 20, call wall + 100]
    assert bands["downside"] == (7680.0, 7770.0)           # [flip - 90, flip]
    rows = {x["name"]: x for x in scenarios.build_scenarios(snap)["scenarios"]}
    assert [rows[n]["p"] for n in ("Consolidation", "Upside", "Downside")] == [0.45, 0.25, 0.30]


def test_below_the_flip_without_a_charm_pin_has_no_set():
    """The upside ceiling is the pin; without it every other ceiling would be a level the report does not use."""
    snap = {"current_level": 7580.0,
            "levels": {"regime_threshold": 7750.0, "upper_resistance_band": 8100.0, "lower_support_band": 7500.0},
            "charm_qualification": {}}
    assert scenarios.scenario_bands(snap) is None
    assert scenarios.build_scenarios(snap)["scenarios"] == []


def test_the_set_says_which_report_it_follows(snapshot):
    s = scenarios.build_scenarios(snapshot)
    assert s["report_as_of"] == scenarios.REPORT_AS_OF == "2026-10-30"
    assert scenarios.REPORT_AS_OF in s["note"]


def test_note_discloses_uncalibrated(snapshot):
    s = scenarios.build_scenarios(snapshot)
    assert "NOT calibrated" in s["note"]


def test_missing_levels_returns_note(snapshot):
    snap = make_snapshot()
    snap["current_level"] = None
    s = scenarios.build_scenarios(snap)
    assert s["scenarios"] == []
    assert "Insufficient" in s["note"]
