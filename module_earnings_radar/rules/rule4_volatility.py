"""Rule 4: Options volatility edge — direction-free, 0% composite weight.

Answers one question for ``select_action``: given a directional read, should
the vehicle be **options** (OPTIONS_BULL/BEAR) or **shares / defined risk**
(SHARES_BULL/BEAR)? A high edge means "options are a favourable instrument
here"; it never votes bull vs bear.

Honest inputs (no fake values):
- missing spot skips the GEX term entirely (no invented $100 regime);
- the GEX regime uses the **directionalized-volume flip** when the strike
  profile is present, else the ``gex-levels`` ``gamma_flip`` — an OI flip far
  under spot would mis-label long gamma on an earnings night (UW: use the
  volume flip for the post-earnings open);
- IV/RV is computed only when both are real; RICH vol (IV >> RV) is a
  *penalty* for a premium buyer, CHEAP vol (IV < RV) is the edge — rewarding
  expensive vol twice was the old bug. The rich ceiling is the skill's 1.25 and
  the RV is Yang-Zhang RV30 (the exact construction that ceiling assumes);
- the term-structure slope (nearest expiry -> 45 DTE, per day) adds a fifth
  input: backwardation = front IV rich vs the back = a drag, contango = the
  favourable read. Absent (no curve, disabled, or a curve that can't span the
  horizon) it contributes nothing and never fabricates a slope;
- coverage stays the shipped four-part set {EM, IV/RV, GEX, history}; the slope
  term is scored but does not enter the coverage denominator, and R4 coverage
  never feeds direction confidence anyway.
- a historical term uses data the pipeline already fetched: median 1-day long-
  straddle payoff across the last completed prints (fallback: median realized
  move vs the current implied move);
- coverage reflects how many of {EM, IV/RV, GEX, history} were actually present.
"""
from RevenueChaser.core.config import (
    VOLATILITY_EDGE_RANGE,
    GEX_REGIME_SHORT_GAMMA, GEX_REGIME_LONG_GAMMA,
    GEX_MAGNET_NEAR_PCT, GEX_MAGNET_BELOW_POINTS, GEX_MAGNET_ABOVE_POINTS,
    GEX_BLOCK_MAX, GEX_BLOCK_MIN,
    IV_RV_RICH, IV_RV_CHEAP, IV_RV_CHEAP_POINTS, IV_RV_RICH_POINTS,
    TS_SLOPE_BACKWARDATION, TS_SLOPE_HALF,
    TS_SLOPE_BACK_POINTS, TS_SLOPE_MILD_POINTS, TS_SLOPE_CONTANGO_POINTS,
)
from RevenueChaser.module_earnings_radar.models import RuleResult


def score(options_data: dict = None) -> RuleResult:
    if not options_data:
        return RuleResult(score=None, available=False, coverage=0, reasons=["no options data"])

    em = options_data.get("expected_move_pct")
    iv = options_data.get("implied_volatility")
    rv = options_data.get("realized_volatility")
    price = options_data.get("underlying_price")
    gamma_flip = options_data.get("gamma_flip")
    iv_rank = options_data.get("iv_rank")
    straddle_median = options_data.get("straddle_payoff_median")
    hist_move_ratio = options_data.get("hist_move_ratio")
    gex_profile = options_data.get("gex_profile") or {}

    # Rule fires on earnings events with an implied move; no EM => no edge to gate.
    if not em:
        return RuleResult(score=None, available=False, coverage=0, reasons=["no expected move"])

    reasons: list[str] = []
    score_val = 0.0
    slope = None

    # EM buckets (unchanged)
    if em > 12:
        score_val += 25
        reasons.append(f"extreme expected move ({em:.1f}%)")
    elif em > 7:
        score_val += 15
        reasons.append(f"elevated EM ({em:.1f}%)")
    elif em > 4:
        score_val += 5
    elif em < 2:
        score_val -= 10

    # IV/RV — only when both are real. RICH vol is a drag for a premium buyer;
    # CHEAP vol is the actual edge. The rich ceiling is the skill's calibrated
    # 1.25 (not the old 1.5): the RV side is Yang-Zhang RV30, which is exactly
    # the construction that threshold was calibrated against.
    ivrv = None
    if iv is not None and rv is not None and rv > 0:
        ivrv = iv / rv
        if ivrv >= IV_RV_RICH:
            score_val += IV_RV_RICH_POINTS
            reasons.append(f"IV/RV elevated ({ivrv:.1f}) — vol rich, not cheap")
        elif ivrv < IV_RV_CHEAP:
            score_val += IV_RV_CHEAP_POINTS
            reasons.append(f"IV/RV low ({ivrv:.1f}) — vol cheap vs realized")

    # Term-structure slope — one curve, near expiry out to ~45 DTE (the
    # skill's construction). Backwardation means the front-dated vol a premium
    # buyer pays is rich vs the back, a drag; contango is the favourable read.
    ts_slope = options_data.get("ts_slope")
    if ts_slope is not None:
        try:
            slope = float(ts_slope)
        except (TypeError, ValueError):
            slope = None
        if slope is not None:
            if slope <= TS_SLOPE_BACKWARDATION:
                score_val += TS_SLOPE_BACK_POINTS
                reasons.append(
                    f"term structure backwardated ({slope:.5f}/day) — front IV rich, "
                    "drag on a premium buyer"
                )
            elif slope <= TS_SLOPE_HALF:
                score_val += TS_SLOPE_MILD_POINTS
                reasons.append(
                    f"term structure mildly backwardated ({slope:.5f}/day)"
                )
            elif slope > 0:
                score_val += TS_SLOPE_CONTANGO_POINTS
                reasons.append(
                    f"term structure contango ({slope:+.5f}/day) — front-dated "
                    "vol cheap vs the back"
                )

    # ── GEX block (clamped): regime + near-spot magnet ──
    gex_val = 0.0
    gex_present = False
    flip = None
    if price is not None:
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            price_f = 0.0
    else:
        price_f = 0.0

    # Regime: the strike profile carries the volume-book read directly
    # (heatmap semantics). Without it, fall back to spot vs the gex-levels
    # gamma flip.
    profile_regime = gex_profile.get("regime")
    if profile_regime in ("short", "long") and price_f > 0:
        gex_present = True
        if profile_regime == "short":
            gex_val += GEX_REGIME_SHORT_GAMMA
            reasons.append(
                "short gamma (volume book: EM band net gamma red)"
            )
        else:
            gex_val += GEX_REGIME_LONG_GAMMA
            reasons.append(
                "long gamma (volume book: EM band net gamma green)"
            )
    else:
        if gex_profile.get("flip") is not None:
            flip = float(gex_profile["flip"])
        elif gamma_flip is not None:
            try:
                flip = float(gamma_flip)
            except (TypeError, ValueError):
                flip = None
        if flip and price_f > 0:
            gex_present = True
            if price_f < flip:
                gex_val += GEX_REGIME_SHORT_GAMMA
                reasons.append(f"short gamma (spot ${price_f:.2f} < flip ${flip:.2f})")
            else:
                gex_val += GEX_REGIME_LONG_GAMMA
                reasons.append(f"long gamma (spot ${price_f:.2f} > flip ${flip:.2f})")

    # Magnet within GEX_MAGNET_NEAR_PCT of spot — below = open air, above = pin cap.
    magnet = gex_profile.get("magnet") or {}
    if magnet.get("strike") is not None and price_f > 0:
        m_dist = abs(magnet["strike"] - price_f) / price_f
        if m_dist <= GEX_MAGNET_NEAR_PCT:
            gex_present = True
            if magnet["side"] == "below":
                gex_val += GEX_MAGNET_BELOW_POINTS
                reasons.append(
                    f"gamma magnet ${magnet['strike']:.2f} below spot ({m_dist:.1%} away)"
                )
            else:
                gex_val += GEX_MAGNET_ABOVE_POINTS
                reasons.append(
                    f"gamma magnet ${magnet['strike']:.2f} above spot pins upside ({m_dist:.1%})"
                )
        else:
            reasons.append(
                f"gamma magnet ${magnet['strike']:.2f} {magnet['side']} but {m_dist:.1%} away"
            )

    # Charm & vanna — vehicle texture, no points. Pin vs peak tells whether time
    # decay argues for a hold (pin) or lets a gap run (no near pin).
    charm = gex_profile.get("charm") or {}
    if charm.get("strike") is not None:
        near = "· no ATM charm pin" if charm.get("label") != "Charm Pin" else " · charm pin"
        reasons.append(
            f"charm {charm['label'].lower()} ${charm['strike']:.2f} "
            f"({_signed(charm.get('signed'))}){near}"
        )
    vanna = gex_profile.get("vanna") or {}
    if vanna:
        bits = []
        if vanna.get("put_peak"):
            bits.append(f"put lobe ${vanna['put_peak']['strike']:.2f}")
        if vanna.get("call_peak"):
            bits.append(f"call lobe ${vanna['call_peak']['strike']:.2f}")
        if bits:
            reasons.append("vanna " + " / ".join(bits) + " — crush re-hedges off spot")

    if gex_present:
        score_val += max(GEX_BLOCK_MIN, min(GEX_BLOCK_MAX, gex_val))

    # History — options actually paid for a premium buyer?
    hist_term = None
    if straddle_median is not None:
        hist_term = straddle_median
        if straddle_median > 0:
            score_val += 10
            reasons.append(f"long straddles paid historically (median {straddle_median:+.1f}%)")
        elif straddle_median < 0:
            score_val -= 10
            reasons.append(f"straddle buyers lost historically (median {straddle_median:.1f}%)")
    elif hist_move_ratio is not None:
        hist_term = hist_move_ratio
        if hist_move_ratio > 1.2:
            score_val += 10
            reasons.append(f"history moves > implied ({hist_move_ratio:.2f}x)")
        elif hist_move_ratio < 0.8:
            score_val -= 5
            reasons.append(f"history under-moves implied ({hist_move_ratio:.2f}x)")

    # IV rank (screener already carries it) — informational, no extra points:
    # richness is already handled by IV/RV when RV exists.
    if iv_rank is not None:
        try:
            rank = float(iv_rank)
        except (TypeError, ValueError):
            rank = None
        if rank is not None and rank >= 80:
            reasons.append(f"iv_rank high ({rank:.0f}) — rich vs own history")

    parts = 0
    if em is not None:
        parts += 1
    if ivrv is not None:
        parts += 1
    if gex_present:
        parts += 1
    if hist_term is not None:
        parts += 1
    coverage = round(parts / 4, 2)

    score_val = max(VOLATILITY_EDGE_RANGE[0], min(VOLATILITY_EDGE_RANGE[1], score_val))

    return RuleResult(
        score=round(score_val, 1), available=True, coverage=coverage,
        reasons=reasons if reasons else ["neutral volatility edge"],
        raw_metrics={
            "expected_move_pct": em, "implied_volatility": iv,
            "realized_volatility": rv,
            "rv_source": options_data.get("rv_source"),
            "iv_rv": round(ivrv, 2) if ivrv is not None else None,
            "ts_slope": round(slope, 5) if slope is not None else None,
            "flip": round(flip, 2) if flip is not None else None,
            "magnet": magnet.get("strike") if magnet else None,
            "charm_strike": charm.get("strike") if charm else None,
            "charm_label": charm.get("label") if charm else None,
        },
    )


def _signed(v) -> str:
    if v is None:
        return "n/a"
    return f"{v:+,.0f}"
