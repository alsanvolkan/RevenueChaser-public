"""Scenario set: the bands, probabilities and asymmetry of the reference report.

**Pinned to the 30 October 2026 report** (``REPORT_AS_OF``), the Northstar / BSZ Research "SPX Market Maker Gamma —
Quant v4" weekly. When the code and a report disagree, the newest report wins (avo, 3 Nov 2026); older reports stay
reference for content and for the scorecard's history. Keep ``REPORT_AS_OF`` in step with the report this file
implements — the previous version of this module silently implemented the 9 Oct one for three weeks, and nothing in
the code said so.

All bands derive from THIS week's levels (gamma flip / put support / charm pin / call wall), never from a carry
model or an expected-move band. The probabilities are the report's own: heuristic, uncalibrated, and audited by the
Brier scorecard rather than trusted.

**The derivation is regime-dependent** (30 Oct §9). With spot BELOW the flip, negative gamma:

    Consolidation [put support, flip] · Upside [flip, charm pin] · Downside [put support - 100p, put support]

With spot ABOVE the flip the pinned report says nothing, because that regime did not occur in it. The 9 Oct rule is
kept for that case:

    Consolidation [flip, call wall + 20p] · Upside [call wall + 20p, call wall + 100p] · Downside [flip - 90p, flip]

shortcut: the above-flip rule and its probabilities are the 9 Oct report's, not the pinned one; replace them with
the pinned report's once a report closes above its flip and states them.

The third column is **asymmetry**, the report's measure since 2 Oct 2026, and expected value is deliberately not
computed: the probabilities are fixed and uncalibrated, so an expectancy built on them would read as a forecast of
profit. The earlier R:R and "Expectancy E" columns were this module's own and are gone.
"""
from __future__ import annotations

import math

#: The report this module implements. Recorded on every Brier record, so a scorecard never mixes rules.
REPORT_AS_OF = "2026-10-30"

# Spot below the flip (negative gamma): 16 Oct and 30 Oct both state these.
BELOW_FLIP_P = {"Consolidation": 0.35, "Upside": 0.30, "Downside": 0.35}
#: Downside floor below put support, below the flip. The reports call the offset intuitive, not calibrated.
BELOW_FLIP_DOWNSIDE_OFFSET = 100.0

# Spot above the flip (positive gamma): the 9 Oct rule, kept until the pinned report covers this regime.
ABOVE_FLIP_P = {"Consolidation": 0.45, "Upside": 0.25, "Downside": 0.30}
ABOVE_FLIP_CONSOLIDATION_CEILING = 20.0
ABOVE_FLIP_UPSIDE_CEILING = 100.0
ABOVE_FLIP_DOWNSIDE_OFFSET = 90.0

NEGATIVE_GAMMA = "below_flip"
POSITIVE_GAMMA = "above_flip"


def asymmetry(trigger: float | None, band_edge: float | None, invalidation: float | None) -> float | None:
    """``|band edge - trigger| / |trigger - invalidation|``; None when degenerate or a level is missing.

    The report's own definition (30 Oct §9): the trigger, the band edge and the invalidation level are all three
    band edges, and a level that is not in the table is never used. It is band geometry — not a success
    probability, not a return guarantee — and it does not apply to the directionless consolidation class.
    """
    if trigger is None or band_edge is None or invalidation is None:
        return None
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (trigger, band_edge, invalidation)):
        return None
    risk = abs(trigger - invalidation)
    if risk <= 0:
        return None
    return abs(band_edge - trigger) / risk


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def scenario_bands(snapshot: dict) -> dict | None:
    """The numeric edges ``build_scenarios`` prints: ``(lo, hi)`` per class, plus the ``regime`` they came from.

    None when the snapshot lacks the spot or any level the regime's own rule needs, so a caller cannot score
    against a partial set. Below the flip that includes the charm pin, which the upside band's ceiling is: without
    it the only honest answer is no set, because every other ceiling would be a level the report does not use.
    """
    levels = snapshot.get("levels") or {}
    spot = _finite(snapshot.get("current_level"))
    flip = _finite(levels.get("regime_threshold"))            # HVL / gamma flip
    call_wall = _finite(levels.get("upper_resistance_band"))  # Call Resistance / ceiling
    put_wall = _finite(levels.get("lower_support_band"))      # Put Support / floor
    if spot is None or flip is None or call_wall is None or put_wall is None:
        return None

    if spot < flip:
        # F2: the 30 Oct report's pin (largest front-expiry net charm), never the vol-mode "Charm Peak"
        pin = _finite((snapshot.get("charm_pin_report") or {}).get("strike"))
        if pin is None or pin <= flip:
            # No pin, or a pin at or below the flip, leaves the upside band without the ceiling the report gives
            # it. The report's upside here is the flip being taken back up to the pin; inventing another ceiling
            # would print a band from a level it does not use.
            return None
        return {
            "regime": NEGATIVE_GAMMA,
            "consolidation": (put_wall, flip),
            "upside": (flip, pin),
            "downside": (put_wall - BELOW_FLIP_DOWNSIDE_OFFSET, put_wall),
        }

    cons_hi = call_wall + ABOVE_FLIP_CONSOLIDATION_CEILING
    return {
        "regime": POSITIVE_GAMMA,
        "consolidation": (flip, cons_hi),
        "upside": (cons_hi, call_wall + ABOVE_FLIP_UPSIDE_CEILING),
        "downside": (flip - ABOVE_FLIP_DOWNSIDE_OFFSET, flip),
    }


def probabilities(regime: str) -> dict[str, float]:
    """The report's probabilities for ``regime``; they differ between the two (0,35/0,30/0,35 vs 0,45/0,25/0,30)."""
    return BELOW_FLIP_P if regime == NEGATIVE_GAMMA else ABOVE_FLIP_P


def realized_class(bands: dict | None, close: float | None) -> str | None:
    """Classify a session close against the consolidation band.

    Above the consolidation high is "Upside", below its low is "Downside", and a close inside the band or exactly
    on an edge is "Consolidation" (the edges belong to both neighbours, so the inclusive band wins). None when
    there are no bands, a band edge is not finite, or the close is not a finite number.
    """
    if not bands:
        return None
    if _finite(close) is None:
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
    """Derive the scenario table from the current snapshot's levels."""
    bands = scenario_bands(snapshot)
    if bands is None:
        return {"scenarios": [], "note": "Insufficient levels to build the scenario set.",
                "report_as_of": REPORT_AS_OF, "regime": None}

    cons_lo, cons_hi = bands["consolidation"]
    up_lo, up_hi = bands["upside"]
    down_lo, down_hi = bands["downside"]
    regime = bands.get("regime")
    p = probabilities(regime)

    scenarios = [
        {
            "name": "Consolidation",
            "range": f"{cons_lo:,.0f} – {cons_hi:,.0f}",
            "p": p["Consolidation"],
            # The report prints "uygulanmaz" here: asymmetry is band geometry around a direction, and this class
            # has none.
            "asymmetry": None,
            "trade": "Theta-weighted (directionless)",
        },
        {
            "name": "Upside",
            "range": f"{up_lo:,.0f} – {up_hi:,.0f}",
            "p": p["Upside"],
            # Trigger the band's near edge, reach its far edge, invalidated at the consolidation band's far side.
            "asymmetry": asymmetry(up_lo, up_hi, cons_lo),
            "trade": "Directional long",
        },
        {
            "name": "Downside",
            "range": f"{down_lo:,.0f} – {down_hi:,.0f}",
            "p": p["Downside"],
            "asymmetry": asymmetry(down_hi, down_lo, cons_hi),
            "trade": "Directional short",
        },
    ]

    regime_text = ("spot below the gamma flip (negative gamma): the consolidation ceiling is the flip and the "
                   "upside band is the flip taken back up to the charm pin"
                   if regime == NEGATIVE_GAMMA else
                   "spot above the gamma flip (positive gamma): the consolidation floor is the flip and the "
                   "bands sit around the call wall")
    note = (
        f"Bands derive from this week's levels only — {regime_text}. Probabilities are the reference report's "
        f"own: heuristic, derived from the gamma regime and positioning, and NOT calibrated forecasts. Asymmetry "
        f"is band geometry (distance from the trigger to the band edge over the distance to the invalidation "
        f"level), not a success probability; expected value is deliberately not computed, because the "
        f"probabilities behind it are fixed and uncalibrated. Scenario rules follow the {REPORT_AS_OF} report. "
        f"The Brier scorecard below tracks how honest these probabilities turn out to be."
    )
    return {"scenarios": scenarios, "note": note, "report_as_of": REPORT_AS_OF, "regime": regime}
