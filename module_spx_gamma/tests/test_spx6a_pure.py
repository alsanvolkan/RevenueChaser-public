"""SPX-6a: numeric scenario bands, realized class and a pure Brier (no IO).

The golden rows below are derived by hand from the rule each regime's report states, not captured from the
implementation, so they fail if the rule drifts rather than following it. `make_snapshot` closes below its flip and
`_fractional_snapshot` above it, so the two regimes are both covered. The prose note is checked separately, by what
it has to disclose, so its wording can change without rewriting a golden.
"""
from __future__ import annotations

import copy
import math

import pytest

from RevenueChaser.module_spx_gamma import brier, scenarios
from .conftest import make_snapshot



def _fractional_snapshot() -> dict:
    """A second fixture: fractional levels, so the printed bands differ."""
    snap = copy.deepcopy(make_snapshot())
    snap["levels"]["regime_threshold"] = 7811.5
    snap["levels"]["upper_resistance_band"] = 7923.25
    snap["levels"]["lower_support_band"] = 7734.0
    snap["current_level"] = 7815.0
    return snap


# Hand-derived from each regime's rule (see the module docstring for both).
GOLDEN = [
    (
        # spot 7,784 < flip 7,820.63 -> negative gamma: [put support, flip] / [flip, charm pin 7,900] /
        # [put support - 100, put support], probabilities 0,35 / 0,30 / 0,35.
        make_snapshot,
        {
            "regime": scenarios.NEGATIVE_GAMMA,
            "consolidation": (7500.0, 7820.63),
            "upside": (7820.63, 7900.0),
            "downside": (7400.0, 7500.0),
        },
        [
            {"name": "Consolidation", "range": "7,500 \u2013 7,821", "p": 0.35,
             "asymmetry": None, "trade": "Theta-weighted (directionless)"},
            {"name": "Upside", "range": "7,821 \u2013 7,900", "p": 0.30,
             "asymmetry": 79.37 / 320.63, "trade": "Directional long"},
            {"name": "Downside", "range": "7,400 \u2013 7,500", "p": 0.35,
             "asymmetry": 100.0 / 320.63, "trade": "Directional short"},
        ],
    ),
    (
        # spot 7,815 > flip 7,811.5 -> positive gamma, the 9 Oct rule the pinned report does not replace:
        # [flip, call wall + 20] / [call wall + 20, call wall + 100] / [flip - 90, flip], 0,45 / 0,25 / 0,30.
        _fractional_snapshot,
        {
            "regime": scenarios.POSITIVE_GAMMA,
            "consolidation": (7811.5, 7943.25),
            "upside": (7943.25, 8023.25),
            "downside": (7721.5, 7811.5),
        },
        [
            {"name": "Consolidation", "range": "7,812 \u2013 7,943", "p": 0.45,
             "asymmetry": None, "trade": "Theta-weighted (directionless)"},
            {"name": "Upside", "range": "7,943 \u2013 8,023", "p": 0.25,
             "asymmetry": 80.0 / 131.75, "trade": "Directional long"},
            {"name": "Downside", "range": "7,722 \u2013 7,812", "p": 0.30,
             "asymmetry": 90.0 / 131.75, "trade": "Directional short"},
        ],
    ),
]


# ── scenario_bands ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("make, expected_bands, expected_rows", GOLDEN)
def test_scenario_bands_are_the_numeric_edges(make, expected_bands, expected_rows):
    bands = scenarios.scenario_bands(make())
    assert bands == expected_bands


@pytest.mark.parametrize("make, expected_bands, expected_rows", GOLDEN)
def test_bands_equal_the_numbers_build_scenarios_prints(make, expected_bands, expected_rows):
    bands = scenarios.scenario_bands(make())
    for name, key in (("Consolidation", "consolidation"), ("Upside", "upside"), ("Downside", "downside")):
        lo, hi = bands[key]
        printed = next(r["range"] for r in expected_rows if r["name"] == name)
        assert printed == f"{lo:,.0f} \u2013 {hi:,.0f}"


@pytest.mark.parametrize("make, expected_bands, expected_rows", GOLDEN)
def test_build_scenarios_rows_follow_the_rule(make, expected_bands, expected_rows):
    got = scenarios.build_scenarios(make())
    assert got["regime"] == expected_bands["regime"]
    assert got["report_as_of"] == scenarios.REPORT_AS_OF
    assert len(got["scenarios"]) == len(expected_rows)
    for row, want in zip(got["scenarios"], expected_rows):
        assert row["name"] == want["name"] and row["range"] == want["range"]
        assert row["p"] == pytest.approx(want["p"]) and row["trade"] == want["trade"]
        if want["asymmetry"] is None:
            assert row["asymmetry"] is None
        else:
            assert row["asymmetry"] == pytest.approx(want["asymmetry"])


@pytest.mark.parametrize("make, expected_bands, expected_rows", GOLDEN)
def test_the_note_discloses_what_it_must(make, expected_bands, expected_rows):
    note = scenarios.build_scenarios(make())["note"]
    assert "NOT calibrated" in note
    assert "expected value is deliberately not computed" in note
    assert scenarios.REPORT_AS_OF in note


def test_build_scenarios_uses_scenario_bands(snapshot, monkeypatch):
    """build_scenarios derives its bands from scenario_bands, not a private copy."""
    swapped = {
        "consolidation": (10.0, 20.0),
        "upside": (20.0, 120.0),
        "downside": (-80.0, 10.0),
    }
    monkeypatch.setattr(scenarios, "scenario_bands", lambda _snapshot: swapped)
    printed = [s["range"] for s in scenarios.build_scenarios(snapshot)["scenarios"]]
    assert printed == ["10 \u2013 20", "20 \u2013 120", "-80 \u2013 10"]


@pytest.mark.parametrize("field", ["current_level"])
def test_scenario_bands_none_when_spot_missing(field):
    snap = make_snapshot()
    snap[field] = None
    assert scenarios.scenario_bands(snap) is None


@pytest.mark.parametrize(
    "level",
    ["regime_threshold", "upper_resistance_band", "lower_support_band"],
)
def test_scenario_bands_none_when_a_level_is_missing(level):
    snap = make_snapshot()
    snap["levels"][level] = None
    assert scenarios.scenario_bands(snap) is None


def test_scenario_bands_none_without_levels_at_all():
    assert scenarios.scenario_bands({"current_level": 7800.0}) is None


# ── realized_class ────────────────────────────────────────────────────────────

BANDS = {
    "consolidation": (7820.63, 7920.0),
    "upside": (7920.0, 8000.0),
    "downside": (7730.63, 7820.63),
}


@pytest.mark.parametrize(
    "close, expected",
    [
        (7920.01, "Upside"),      # just above the consolidation high
        (8000.0, "Upside"),       # top of the upside band
        (9000.0, "Upside"),       # far above
        (7820.62, "Downside"),    # just below the consolidation low
        (7730.63, "Downside"),    # bottom of the downside band
        (7000.0, "Downside"),     # far below
        (7870.0, "Consolidation"),
        (7820.63, "Consolidation"),  # exactly on the low edge
        (7920.0, "Consolidation"),   # exactly on the high edge
    ],
)
def test_realized_class(close, expected):
    assert scenarios.realized_class(BANDS, close) == expected


def test_realized_class_returns_brier_class_names():
    got = {scenarios.realized_class(BANDS, c) for c in (7820.62, 7870.0, 7920.01)}
    assert got <= set(brier.CLASSES)


def test_realized_class_none_without_bands():
    assert scenarios.realized_class(None, 7870.0) is None


@pytest.mark.parametrize("close", [None, math.nan, math.inf, -math.inf, "7870.0", True])
def test_realized_class_none_when_close_is_not_finite(close):
    assert scenarios.realized_class(BANDS, close) is None


# ── brier ─────────────────────────────────────────────────────────────────────

ONE_HOT_CONS = {"Consolidation": 1.0, "Upside": 0.0, "Downside": 0.0}


def test_brier_one_hot_on_the_right_class_is_zero():
    assert brier.brier(ONE_HOT_CONS, "Consolidation") == 0.0


@pytest.mark.parametrize("realized", ["Upside", "Downside"])
def test_brier_one_hot_on_a_wrong_class_is_two(realized):
    assert brier.brier(ONE_HOT_CONS, realized) == 2.0


def test_brier_equal_weights_equal_the_reference():
    third = 1 / 3
    probs = {c: third for c in brier.CLASSES}
    assert abs(brier.brier(probs, "Upside") - brier.EQUAL_WEIGHT_REFERENCE) < 1e-12


def test_brier_accepts_integer_probabilities():
    assert brier.brier({"Consolidation": 1, "Upside": 0, "Downside": 0}, "Consolidation") == 0.0


@pytest.mark.parametrize(
    "probs",
    [
        {"Consolidation": 0.5, "Upside": 0.5},                       # missing class
        {"Consolidation": 0.5, "Upside": 0.5, "Downside": 0.5},      # sums to 1.5
        {"Consolidation": 1.2, "Upside": 0.0, "Downside": -0.2},     # out of [0, 1]
        {"Consolidation": math.nan, "Upside": 0.5, "Downside": 0.5},
        {"Consolidation": True, "Upside": 0.0, "Downside": 0.0},     # bool is not a probability
        {"Consolidation": "1", "Upside": 0.0, "Downside": 0.0},
    ],
)
def test_brier_rejects_invalid_probs(probs):
    with pytest.raises(ValueError):
        brier.brier(probs, "Consolidation")


def test_brier_rejects_an_unknown_realized_class():
    with pytest.raises(ValueError):
        brier.brier(ONE_HOT_CONS, "Range")


# ── score_date delegates to brier ─────────────────────────────────────────────


def test_score_date_stores_the_same_brier(tmp_path):
    p = tmp_path / "brier.json"
    probs = {"Consolidation": 0.5, "Upside": 0.3, "Downside": 0.2}
    brier.record_probs("2026-08-21", probs, path=p)
    scored = brier.score_date("2026-08-21", "Upside", path=p)
    assert scored["records"][0]["brier"] == brier.brier(probs, "Upside")


@pytest.mark.parametrize("flip", [float("nan"), float("inf")])
def test_a_non_finite_band_edge_gives_no_class(flip):
    bands = scenarios.scenario_bands({"current_level": 7900.0, "levels": {
        "regime_threshold": flip, "upper_resistance_band": 7950.0, "lower_support_band": 7800.0}})
    assert scenarios.realized_class(bands, 7700.0) is None
