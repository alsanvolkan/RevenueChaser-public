"""Tests for the scenario set (bands, R:R, Expectancy)."""
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


def test_directional_have_rr_and_expectancy(snapshot):
    s = scenarios.build_scenarios(snapshot)
    for x in s["scenarios"]:
        if x["name"] == "Consolidation":
            assert x["rr"] is None
        else:
            assert x["rr"] is not None
            assert x["expectancy"] is not None


def test_expectancy_formula():
    # E = (R:R * P) - (1 - P)
    rr, p = 2.0, 0.5
    assert abs(scenarios.expectancy(rr, p) - (2.0 * 0.5 - 0.5)) < 1e-9
    assert scenarios.expectancy(None, 0.5) is None


def test_note_discloses_uncalibrated(snapshot):
    s = scenarios.build_scenarios(snapshot)
    assert "NOT calibrated" in s["note"]


def test_missing_levels_returns_note(snapshot):
    snap = make_snapshot()
    snap["current_level"] = None
    s = scenarios.build_scenarios(snap)
    assert s["scenarios"] == []
    assert "Insufficient" in s["note"]
