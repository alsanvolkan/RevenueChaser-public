from datetime import datetime
from typing import Optional

from RevenueChaser.core.config import RULE_WEIGHTS, DIRECTION_RANGE, VOLATILITY_EDGE_RANGE
from RevenueChaser.core.models.events import RuleResult


def compute_direction_score(rule_results: dict[str, RuleResult]) -> float:
    raw = 0.0
    total_weight = 0.0
    for rule_name, result in rule_results.items():
        weight = RULE_WEIGHTS.get(rule_name, 0)
        if weight > 0 and result.available and result.score is not None:
            raw += weight * result.score
            total_weight += weight
    if total_weight == 0:
        return 0.0
    raw = raw / max(total_weight, 0.01)
    return max(DIRECTION_RANGE[0], min(DIRECTION_RANGE[1], round(raw, 1)))


def compute_confidence_score(rule_results: dict[str, RuleResult]) -> float:
    weighted_available = sum(
        RULE_WEIGHTS.get(r, 0) * v.coverage
        for r, v in rule_results.items()
        if v.available and RULE_WEIGHTS.get(r, 0) > 0
    )
    total = sum(w for w in RULE_WEIGHTS.values() if w > 0)
    if total == 0:
        return 0.0

    # Disagreement penalty is R3 (financial) × R7 (accounting) ONLY. R2 vs R3
    # sign tension is a headline, not a confidence multiplier (design doc).
    r3 = rule_results.get("rule3_financial")
    r7 = rule_results.get("rule7_accounting")
    disagreement = 0.0
    if r3 is not None and r7 is not None and r3.available and r7.available \
            and r3.score is not None and r7.score is not None \
            and r3.score != 0 and r7.score != 0 \
            and (r3.score > 0) != (r7.score > 0):
        disagreement = 0.2
    raw = 100 * (weighted_available / total) * (1 - disagreement)
    return max(0.0, min(100.0, round(raw, 1)))


def compute_composite_scores(
    r1: RuleResult = None,
    r2: RuleResult = None,
    r3: RuleResult = None,
    r4: RuleResult = None,
    r5: RuleResult = None,
    r6: RuleResult = None,
    r7: RuleResult = None,
    r8: RuleResult = None,
) -> dict:
    rule_results = {
        "rule1_drivers": r1 or RuleResult(score=None, available=False, coverage=0),
        "rule2_guidance": r2 or RuleResult(score=None, available=False, coverage=0),
        "rule3_financial": r3 or RuleResult(score=None, available=False, coverage=0),
        "rule5_positioning": r5 or RuleResult(score=None, available=False, coverage=0),
        "rule7_accounting": r7 or RuleResult(score=None, available=False, coverage=0),
        "rule8_sentiment": r8 or RuleResult(score=None, available=False, coverage=0),
    }

    direction_score = compute_direction_score(rule_results)
    volatility_edge_score = max(
        VOLATILITY_EDGE_RANGE[0],
        min(VOLATILITY_EDGE_RANGE[1], round(r4.display_score() if r4 else 0, 1)),
    )
    confidence_score = compute_confidence_score(rule_results)

    return {
        "direction_score": direction_score,
        "volatility_edge_score": volatility_edge_score,
        "confidence_score": confidence_score,
        "rule_results": rule_results,
    }


def classify_bias(direction_score: float) -> str:
    if direction_score >= 12:
        return "BULLISH_LEAN"
    if direction_score <= -12:
        return "BEARISH_LEAN"
    return "NEUTRAL"


def classify_conviction(direction_score: float, confidence_score: float, rule_results: dict) -> str:
    # Direction votes come only from rules with a positive weight (R5 is a tag).
    scores = [v.score for r, v in rule_results.items()
              if RULE_WEIGHTS.get(r, 0) > 0 and v.available and v.score is not None]
    nonzero = [s for s in scores if s != 0]
    aligned = sum((s > 0) == (direction_score > 0) for s in nonzero)

    if abs(direction_score) >= 25 and confidence_score >= 65 and aligned >= 3:
        return "HIGH"
    if abs(direction_score) >= 12 and confidence_score >= 45 and aligned >= 2:
        return "MEDIUM"
    return "LOW"


def select_action(
    direction_score: float,
    volatility_edge_score: float,
    veto_flag: bool,
    confidence_score: float = 0,
) -> str:
    """Vehicle + R4 gate, signed by direction.

    - veto                         -> NO_TRADE
    - |dir| < 12                   -> WATCH
    - conf < 50                    -> WATCH_LOW_COVERAGE
    - |dir| >= 25 and vol-edge>=50 -> OPTIONS_BULL / OPTIONS_BEAR  (use options)
    - |dir| >= 18                  -> SHARES_BULL / SHARES_BEAR     (R4 said no)
    - else                         -> DIRECTIONAL_WATCH
    """
    if veto_flag:
        return "NO_TRADE"
    if abs(direction_score) < 12:
        return "WATCH"
    if confidence_score < 50:
        return "WATCH_LOW_COVERAGE"
    bullish = direction_score > 0
    if abs(direction_score) >= 25 and volatility_edge_score >= 50:
        return "OPTIONS_BULL" if bullish else "OPTIONS_BEAR"
    if abs(direction_score) >= 18:
        return "SHARES_BULL" if bullish else "SHARES_BEAR"
    return "DIRECTIONAL_WATCH"


def recommend_options_structure(direction_score: float, spot, gex: dict,
                                gex_profile: dict = None) -> str:
    """Recommend a specific options structure from GEX regime + direction.

    Short gamma (spot < gamma_flip) => dealers amplify moves => buy options.
    Long gamma (spot > gamma_flip) => dealers dampen moves => sell/defined-risk.

    When a strike-level ``gex_profile`` is supplied, the recommendation is
    priced as a debit (defined risk) with the gamma magnet / put wall as the
    stated target band — never fabricated strikes.
    """
    try:
        flip = float(gex.get("gamma_flip") or 0)
    except (ValueError, TypeError):
        flip = 0.0
    try:
        spot_f = float(spot)
    except (ValueError, TypeError):
        spot_f = 0.0

    if not spot_f:
        return ""

    profile = gex_profile or {}
    short_gamma = None
    if profile.get("regime") in ("short", "long"):
        short_gamma = profile["regime"] == "short"
    else:
        if not flip and profile.get("flip") is not None:
            flip = float(profile["flip"])
        if flip:
            short_gamma = spot_f < flip

    if short_gamma is None:
        return ""

    bullish = direction_score >= 12
    bearish = direction_score <= -12

    if short_gamma:
        if bullish:
            structure = "LONG CALL / CALL DEBIT SPREAD"
        elif bearish:
            structure = "LONG PUT / PUT DEBIT SPREAD"
        else:
            structure = "LONG STRADDLE / STRANGLE"
    elif bullish:
        structure = "BULL PUT SPREAD"
    elif bearish:
        structure = "BEAR CALL SPREAD"
    else:
        structure = "IRON CONDOR / SELL STRADDLE"

    # Target band from the live profile (magnets / walls are real levels).
    targets = []
    magnet = (profile.get("magnet") or {})
    if magnet.get("strike") is not None:
        targets.append(f"magnet ${magnet['strike']:.2f}")
    vanna = profile.get("vanna") or {}
    if bearish and vanna.get("put_peak"):
        targets.append(f"put wall/lobe ${vanna['put_peak']['strike']:.2f}")
    if not targets:
        return structure
    return f"{structure} · target {', '.join(targets)}"
