"""SPX-6a: numeric scenario bands, realized class and a pure Brier (no IO).

The golden ``build_scenarios`` outputs below were captured from the implementation
before the bands were extracted, so they pin the refactor to a byte-identical result.
"""
from __future__ import annotations

import copy
import math

import pytest

from RevenueChaser.module_spx_gamma import brier, scenarios
from .conftest import make_snapshot

NOTE = (
    "Probabilities are heuristic, derived from the gamma regime and positioning; "
    "they are NOT calibrated forecasts. Bands derive from this week's levels only "
    "(gamma flip / call wall / put support), with the same intuitive offsets as the "
    "reference report. The Brier scorecard below tracks how honest these "
    "probabilities turn out to be."
)


def _fractional_snapshot() -> dict:
    """A second fixture: fractional levels, so the printed bands differ."""
    snap = copy.deepcopy(make_snapshot())
    snap["levels"]["regime_threshold"] = 7811.5
    snap["levels"]["upper_resistance_band"] = 7923.25
    snap["levels"]["lower_support_band"] = 7734.0
    snap["current_level"] = 7815.0
    return snap


# Golden build_scenarios output, captured before the bands were extracted.
GOLDEN = [
    (
        make_snapshot,
        {
            "consolidation": (7820.63, 7920.0),
            "upside": (7920.0, 8000.0),
            "downside": (7730.63, 7820.63),
        },
        {
            "scenarios": [
                {"name": "Consolidation", "range": "7,821 \u2013 7,920", "p": 0.45,
                 "rr": None, "expectancy": None, "trade": "Theta-weighted (directionless)"},
                {"name": "Upside", "range": "7,920 \u2013 8,000", "p": 0.25,
                 "rr": 0.8050719533058276, "expectancy": -0.5487320116735431,
                 "trade": "Directional long"},
                {"name": "Downside", "range": "7,731 \u2013 7,821", "p": 0.3,
                 "rr": 0.9057059474690561, "expectancy": -0.42828821575928316,
                 "trade": "Directional short"},
            ],
            "note": NOTE,
        },
    ),
    (
        _fractional_snapshot,
        {
            "consolidation": (7811.5, 7943.25),
            "upside": (7943.25, 8023.25),
            "downside": (7721.5, 7811.5),
        },
        {
            "scenarios": [
                {"name": "Consolidation", "range": "7,812 \u2013 7,943", "p": 0.45,
                 "rr": None, "expectancy": None, "trade": "Theta-weighted (directionless)"},
                {"name": "Upside", "range": "7,943 \u2013 8,023", "p": 0.25,
                 "rr": 0.6072106261859582, "expectancy": -0.5981973434535104,
                 "trade": "Directional long"},
                {"name": "Downside", "range": "7,722 \u2013 7,812", "p": 0.3,
                 "rr": 0.683111954459203, "expectancy": -0.495066413662239,
                 "trade": "Directional short"},
            ],
            "note": NOTE,
        },
    ),
]


# ── scenario_bands ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("make, expected_bands, expected_output", GOLDEN)
def test_scenario_bands_are_the_numeric_edges(make, expected_bands, expected_output):
    bands = scenarios.scenario_bands(make())
    assert bands == expected_bands


@pytest.mark.parametrize("make, expected_bands, expected_output", GOLDEN)
def test_bands_equal_the_numbers_build_scenarios_prints(make, expected_bands, expected_output):
    bands = scenarios.scenario_bands(make())
    for name, key in (("Consolidation", "consolidation"), ("Upside", "upside"), ("Downside", "downside")):
        lo, hi = bands[key]
        printed = next(s["range"] for s in expected_output["scenarios"] if s["name"] == name)
        assert printed == f"{lo:,.0f} \u2013 {hi:,.0f}"


@pytest.mark.parametrize("make, expected_bands, expected_output", GOLDEN)
def test_build_scenarios_output_is_unchanged(make, expected_bands, expected_output):
    assert scenarios.build_scenarios(make()) == expected_output


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
