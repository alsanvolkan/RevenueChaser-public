"""Scenario set: consolidation / up / down bands with P, R:R, Expectancy.

Mirrors the report's scenario table. All bands derive from THIS week's levels
(HVL/CR/PS/pin), not from a carry model. Probabilities are heuristic
(regime-derived, uncalibrated) and the Brier scorecard tracks their honesty.
"""
from __future__ import annotations

import math

CONSOLIDATION_P = 0.45
UP_P = 0.25
DOWN_P = 0.30


def _rr(entry: float, target: float, stop: float) -> float | None:
    """Risk/reward = |target - entry| / |entry - stop|; None when degenerate."""
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0:
        return None
    return reward / risk


def expectancy(rr: float | None, p: float) -> float | None:
    """E = (R:R x P) - (1-P)."""
    if rr is None:
        return None
    return rr * p - (1 - p)


def scenario_bands(snapshot: dict) -> dict | None:
    """The numeric edges ``build_scenarios`` prints: ``(lo, hi)`` per class.

    None when the snapshot lacks the spot or any core level (same rule as
    ``build_scenarios``), so a caller cannot score against a partial set.
    """
    levels = snapshot.get("levels", {})
    spot = snapshot.get("current_level")
    flip = levels.get("regime_threshold")            # HVL / gamma flip
    call_wall = levels.get("upper_resistance_band")  # Call Resistance / ceiling
    put_wall = levels.get("lower_support_band")      # Put Support / floor
    if spot is None or flip is None or call_wall is None or put_wall is None:
        return None
    return {
        "consolidation": (flip, call_wall + 20),
        "upside": (call_wall + 20, call_wall + 100),
        "downside": (flip - 90, flip),
    }


def realized_class(bands: dict | None, close: float | None) -> str | None:
    """Classify a session close against the consolidation band.

    Above the consolidation high is "Upside", below its low is "Downside", and a
    close inside the band or exactly on an edge is "Consolidation" (the edges
    belong to both neighbours, so the inclusive band wins). None when there are no
    bands, a band edge is not finite, or the close is not a finite number.
    """
    if not bands:
        return None
    if isinstance(close, bool) or not isinstance(close, (int, float)) or not math.isfinite(close):
        return None
    cons_lo, cons_hi = bands["consolidation"]
    if not (math.isfinite(cons_lo) and math.isfinite(cons_hi)):
        return None
    if close > cons_hi:
        return "Upside"
    if close < cons_lo:
        return "Downside"
    return "Consolidation"


def build_scenarios(snapshot: dict) -> dict:
    """Derive scenario bands from the current snapshot's levels."""
    bands = scenario_bands(snapshot)
    if bands is None:
        return {"scenarios": [], "note": "Insufficient levels to build the scenario set."}

    cons_lo, cons_hi = bands["consolidation"]
    up_lo, up_hi = bands["upside"]
    down_lo, down_hi = bands["downside"]

    scenarios = [
        {
            "name": "Consolidation",
            "range": f"{cons_lo:,.0f} – {cons_hi:,.0f}",
            "p": CONSOLIDATION_P,
            "rr": None,
            "expectancy": None,
            "trade": "Theta-weighted (directionless)",
        },
        {
            "name": "Upside",
            "range": f"{up_lo:,.0f} – {up_hi:,.0f}",
            "p": UP_P,
            "rr": _rr(up_lo, up_hi, cons_lo),
            "expectancy": None,
            "trade": "Directional long",
        },
        {
            "name": "Downside",
            "range": f"{down_lo:,.0f} – {down_hi:,.0f}",
            "p": DOWN_P,
            "rr": _rr(down_hi, down_lo, cons_hi),
            "expectancy": None,
            "trade": "Directional short",
        },
    ]

    for s in scenarios:
        if s["rr"] is not None:
            s["expectancy"] = expectancy(s["rr"], s["p"])

    note = (
        "Probabilities are heuristic, derived from the gamma regime and positioning; "
        "they are NOT calibrated forecasts. Bands derive from this week's levels only "
        "(gamma flip / call wall / put support), with the same intuitive offsets as the "
        "reference report. The Brier scorecard below tracks how honest these "
        "probabilities turn out to be."
    )
    return {"scenarios": scenarios, "note": note}
