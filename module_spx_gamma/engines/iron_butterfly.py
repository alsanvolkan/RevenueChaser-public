"""Iron Butterfly "Pin Harvest" panel around the prediction-engine pin.

Center strike = ``prediction.active_pin`` (the 0-1 DTE magnet, or the equilibrium
fallback — e.g. 7,630 when spot printed 7,631). Wing width = 1x expected move
rounded to the strike grid and clamped, with fixed alternates priced for
comparison. The four legs are quoted off the normalized UW option chain (NBBO
mid). A live signal maps spot against the pin ± consolidation band: HOLD PIN
inside the range, else directional breakout (short-gamma breakdown toward the
vanna floor / upside acceleration toward the squeeze target).
"""
from __future__ import annotations

from ..config import (
    CONSOLIDATION_BAND_PTS,
    IB_CANDIDATE_WINGS,
    IB_MAX_W,
    IB_MIN_W,
    IB_STRIKE_STEP,
)


def wing_width(em_points: float | None) -> float | None:
    """1x expected move rounded to the strike grid, clamped to [IB_MIN_W, IB_MAX_W]."""
    if em_points is None:
        return None
    w = round(em_points / IB_STRIKE_STEP) * IB_STRIKE_STEP
    return min(max(w, IB_MIN_W), IB_MAX_W)


def _legs(chain: list[dict], strike: float, typ: str) -> list[dict]:
    return [r for r in chain
            if r.get("type") == typ and r.get("strike") == strike]


def _pick_leg(chain: list[dict], strike: float, typ: str, spot: float) -> dict | None:
    """Exact (strike, type) row; fall back to the nearest available strike."""
    exact = _legs(chain, strike, typ)
    if exact:
        return exact[0]
    candidates = [r for r in chain if r.get("type") == typ and r.get("strike") is not None]
    if not candidates:
        return None
    candidates.sort(key=lambda r: abs(r["strike"] - strike))
    return candidates[0]


def _mid(leg: dict | None) -> float | None:
    if leg is None:
        return None
    mid = leg.get("mid")
    if mid is not None:
        return mid
    bid, ask = leg.get("bid"), leg.get("ask")
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return None


def _theta(leg: dict | None) -> float | None:
    return leg.get("theta") if leg is not None else None


def _price(chain: list[dict], center: float, width: float, spot: float) -> dict:
    """Price the 4-leg structure at ``width``; None metrics when any leg lacks a price."""
    lo = center - width
    hi = center + width
    long_put = _pick_leg(chain, lo, "P", spot)
    short_put = _pick_leg(chain, center, "P", spot)
    short_call = _pick_leg(chain, center, "C", spot)
    long_call = _pick_leg(chain, hi, "C", spot)

    mids = [_mid(x) for x in (long_put, short_put, short_call, long_call)]
    priced = all(m is not None for m in mids)
    if priced:
        net_credit = (mids[1] + mids[2]) - (mids[0] + mids[3])
        max_risk = width - net_credit
        return_on_risk = (net_credit / max_risk) if max_risk > 0 else None
    else:
        net_credit = max_risk = return_on_risk = None

    thetas = [_theta(x) for x in (long_put, short_put, short_call, long_call)]
    net_theta = None
    if all(t is not None for t in thetas):
        net_theta = -thetas[1] - thetas[2] + thetas[0] + thetas[3]

    legs = [
        {"action": "BUY", "type": "PUT", "strike": long_put["strike"] if long_put else lo,
         "target_strike": lo,
         "mid": mids[0], "iv": long_put.get("iv") if long_put else None,
         "theta": thetas[0], "open_interest": long_put.get("open_interest") if long_put else None},
        {"action": "SELL", "type": "PUT", "strike": short_put["strike"] if short_put else center,
         "target_strike": center,
         "mid": mids[1], "iv": short_put.get("iv") if short_put else None,
         "theta": thetas[1], "open_interest": short_put.get("open_interest") if short_put else None},
        {"action": "SELL", "type": "CALL", "strike": short_call["strike"] if short_call else center,
         "target_strike": center,
         "mid": mids[2], "iv": short_call.get("iv") if short_call else None,
         "theta": thetas[2], "open_interest": short_call.get("open_interest") if short_call else None},
        {"action": "BUY", "type": "CALL", "strike": long_call["strike"] if long_call else hi,
         "target_strike": hi,
         "mid": mids[3], "iv": long_call.get("iv") if long_call else None,
         "theta": thetas[3], "open_interest": long_call.get("open_interest") if long_call else None},
    ]
    for leg in legs:
        leg["strike"] = round(leg["strike"], 1)
        leg["target_strike"] = round(leg["target_strike"], 1)

    return {
        "width": width,
        "lower_wing": round(lo, 1),
        "upper_wing": round(hi, 1),
        "legs": legs,
        "net_credit": round(net_credit, 2) if net_credit is not None else None,
        "max_risk": round(max_risk, 2) if max_risk is not None else None,
        "max_profit": round(net_credit, 2) if net_credit is not None else None,
        "lower_breakeven": round(center - net_credit, 2) if net_credit is not None else None,
        "upper_breakeven": round(center + net_credit, 2) if net_credit is not None else None,
        "return_on_risk": round(return_on_risk, 3) if return_on_risk is not None else None,
        "net_theta": round(net_theta, 2) if net_theta is not None else None,
    }


def signal_recommendation(snapshot: dict, center: float | None) -> dict:
    """Live stance: HOLD PIN inside the consolidation band, else directional breakout.

    Targets reuse the prediction-engine outputs (vanna floor for the breakdown,
    upside trigger + 15 for the squeeze) so the panel can never drift from the
    "Single Source of Truth" scenario table.
    """
    spot = snapshot.get("current_level")
    pred = snapshot.get("prediction") or {}
    band = CONSOLIDATION_BAND_PTS
    if spot is None or center is None:
        return {"signal": None, "action": "Insufficient data for a live signal.",
                "target": None, "band": None, "spot": None, "center": None}
    if abs(spot - center) <= band:
        signal = "PIN"
        action = f"Hold the pin; sell the {center:,.0f} straddle and collect theta into the range"
        target = center
    elif spot < center - band:
        signal = "BEARISH_BREAKOUT"
        action = "Cancel pin; long put / bear debit spread into the short-gamma breakdown"
        target = pred.get("downside_floor") or pred.get("vanna_floor")
    else:
        signal = "BULLISH_ACCEL"
        trigger = pred.get("upside_trigger")
        action = "Upside acceleration; long call / bull debit spread"
        target = (trigger + 15) if trigger is not None else None
    return {
        "signal": signal,
        "action": action,
        "target": target,
        "band": f"{center - band:,.0f} – {center + band:,.0f}",
        "spot": round(spot, 2),
        "center": center,
    }


def build_iron_butterfly(snapshot: dict) -> dict | None:
    """Executes the pin-harvest ruleset; None when chain/spot/pin are missing."""
    spot = snapshot.get("current_level")
    chain = snapshot.get("option_chain") or []
    pin = (snapshot.get("prediction") or {}).get("active_pin")
    em = snapshot.get("expected_move") or {}
    if spot is None or not chain or pin is None:
        return None

    strikes = {r["strike"] for r in chain if r.get("strike") is not None}
    center = pin if pin in strikes else min(strikes, key=lambda s: abs(s - spot))
    w = wing_width(em.get("points"))
    if w is None:
        w = IB_CANDIDATE_WINGS[1] if IB_CANDIDATE_WINGS else 50.0

    base = _price(chain, center, w, spot)
    alternates = []
    for cand in IB_CANDIDATE_WINGS:
        if cand == w:
            continue
        alt = _price(chain, center, cand, spot)
        alternates.append({
            "width": cand,
            "net_credit": alt["net_credit"],
            "max_risk": alt["max_risk"],
            "return_on_risk": alt["return_on_risk"],
        })

    return {
        "center_strike": center,
        "spot": round(spot, 2),
        "wing_width": w,
        "width_source": {
            "kind": "expected_move",
            "em_points": round(em["points"], 2) if em.get("points") is not None else None,
            "iv_used": round(em["iv_used"], 4) if em.get("iv_used") is not None else None,
            "band": f"{IB_MIN_W:,.0f}–{IB_MAX_W:,.0f}",
        },
        "strategy": "Iron Butterfly",
        "legs": base["legs"],
        "net_credit": base["net_credit"],
        "max_risk": base["max_risk"],
        "max_profit": base["max_profit"],
        "lower_breakeven": base["lower_breakeven"],
        "upper_breakeven": base["upper_breakeven"],
        "return_on_risk": base["return_on_risk"],
        "net_theta": base["net_theta"],
        "alternates": alternates,
        "signal": signal_recommendation(snapshot, center),
    }
