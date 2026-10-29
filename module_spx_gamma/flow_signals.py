"""Volume/flow confluence signals — OI walls tested against intraday flow.

Methodology (walls tested against flow):
  - OI levels are the structural baseline (macro walls / gamma flip).
  - Volume GEX levels (``source=vol``) are the intraday engine (0DTE pressure).
  - ``flow-per-strike`` micro-confirms whether the day's ask/bid-sided flow
    actually supports a wall break or is fading it.

Signals:
  - SQUEEZE_ALERT : price at an OI wall with strong call flow on the Ask side
                    (MM can't defend -> upside breakout risk).
  - FAKEOUT_RISK  : price above an OI wall but flow is not confirming
                    (mean-reversion risk back inside the wall).
  - WALL_HOLDING  : neither condition — structural wall is holding.
  - ABOVE_CALL_WALL / BELOW_PUT_WALL : price is past a wall by more than the 1% band, so the wall is
                    no longer being tested (review N3: "at the wall" had no far edge, so a spot 3% past
                    the wall still read as testing it, or as a wall that holds).
"""
from __future__ import annotations

from .config import to_float

WALL_DISTANCE_PCT = 0.01      # price within 1% of an OI wall counts as "at" the wall
CALL_FLOW_RATIO = 1.5         # call_volume >= put_volume * 1.5 -> dominant call flow
ASK_SIDE_RATIO = 0.55         # ask_side_volume_ratio > 0.55 -> aggressive buyers


def _wall_rows(flow_rows: list[dict], strike: float | None, radius: float = 25.0) -> list[dict]:
    """Flow rows within ``radius`` points of ``strike``."""
    if strike is None:
        return []
    return [r for r in flow_rows
            if r.get("strike") is not None and abs(r["strike"] - strike) <= radius]


def _call_vs_put(rows: list[dict]) -> tuple[float, float]:
    cv = sum(r.get("call_volume") or 0 for r in rows)
    pv = sum(r.get("put_volume") or 0 for r in rows)
    return cv, pv


def _ask_ratio(rows: list[dict]) -> float | None:
    cv = sum(r.get("call_volume") or 0 for r in rows)
    cva = sum(r.get("call_volume_ask_side") or 0 for r in rows)
    return (cva / cv) if cv else None


def _bid_ratio(rows: list[dict]) -> float | None:
    cv = sum(r.get("call_volume") or 0 for r in rows)
    cvb = sum(r.get("call_volume_bid_side") or 0 for r in rows)
    return (cvb / cv) if cv else None


def evaluate_flow_signal(snapshot: dict) -> dict:
    """Evaluate the intraday flow signal from a normalized snapshot.

    Returns ``{signal, label, tone, narrative}`` where ``signal`` is one of
    ``SQUEEZE_ALERT`` / ``FAKEOUT_RISK`` / ``WALL_HOLDING`` / ``ABOVE_CALL_WALL`` / ``BELOW_PUT_WALL``,
    or None when there is nothing to test: missing flow rows or a missing spot are not a wall that holds (DF26).
    """
    spot = snapshot.get("current_level")
    levels = snapshot.get("levels", {})
    flow_rows = snapshot.get("flow_strikes", [])
    if not flow_rows:
        return {"signal": None, "label": "No flow data",
                "tone": "flat", "narrative": "No flow-per-strike data for this day."}
    if spot is None:
        return {"signal": None, "label": "No spot",
                "tone": "flat", "narrative": "No spot price to test the walls against."}

    call_wall_oi = to_float(levels.get("upper_resistance_band"))
    call_wall_vol = to_float(levels.get("call_wall_vol"))
    put_wall_oi = to_float(levels.get("lower_support_band"))

    def _at_wall(strike: float | None) -> bool:
        # Within the band on either side of the wall (review N3).
        return strike is not None and abs(spot - strike) <= strike * WALL_DISTANCE_PCT

    # Confluence (squeeze / breakout): at the OI call wall with strong call flow.
    if _at_wall(call_wall_oi):
        rows = _wall_rows(flow_rows, call_wall_oi)
        cv, pv = _call_vs_put(rows)
        ask = _ask_ratio(rows)
        if cv > 0 and cv >= pv * CALL_FLOW_RATIO and (ask or 0) > ASK_SIDE_RATIO:
            ratio = f"{cv / pv:.1f}x" if pv else "all"
            return {
                "signal": "SQUEEZE_ALERT", "label": "Squeeze Alert",
                "tone": "green",
                "narrative": (f"Price at Call Wall {call_wall_oi:,.0f} with "
                              f"{ratio} Call flow on Ask side — breakout risk."),
            }

    # Fakeout (mean-reversion): price above the OI call wall but flow not confirming.
    if call_wall_oi is not None and spot > call_wall_oi:
        rows = _wall_rows(flow_rows, call_wall_oi)
        cv, pv = _call_vs_put(rows)
        bid = _bid_ratio(rows)
        weak_call = cv <= pv or (bid or 0) > ASK_SIDE_RATIO
        vol_wall_below = call_wall_vol is not None and call_wall_vol < spot
        if weak_call and vol_wall_below:
            return {
                "signal": "FAKEOUT_RISK", "label": "Fakeout Risk",
                "tone": "red",
                "narrative": (f"Price above Call Wall {call_wall_oi:,.0f} but call flow "
                              f"is not confirming ({cv:.0f} vs {pv:.0f} puts) with vol wall "
                              f"{call_wall_vol:,.0f} below — mean-reversion risk."),
            }

    # Holding at the put wall? symmetric confirmation check.
    if _at_wall(put_wall_oi):
        rows = _wall_rows(flow_rows, put_wall_oi)
        cv, pv = _call_vs_put(rows)
        if pv > 0 and pv >= cv * CALL_FLOW_RATIO:
            ratio = f"{pv / cv:.1f}x" if cv else "all"
            return {
                "signal": "WALL_HOLDING", "label": "Put Wall Defense",
                "tone": "flat",
                "narrative": (f"Put Wall {put_wall_oi:,.0f} defended with "
                              f"{ratio} Put flow."),
            }

    if call_wall_oi is not None and spot > call_wall_oi * (1 + WALL_DISTANCE_PCT):
        return {
            "signal": "ABOVE_CALL_WALL", "label": "Above Call Wall",
            "tone": "flat",
            "narrative": (f"Price {(spot / call_wall_oi - 1) * 100:.1f}% above Call Wall {call_wall_oi:,.0f}: "
                          "the wall is behind price, not being tested, and no fakeout signal."),
        }
    if put_wall_oi is not None and spot < put_wall_oi * (1 - WALL_DISTANCE_PCT):
        return {
            "signal": "BELOW_PUT_WALL", "label": "Below Put Wall",
            "tone": "flat",
            "narrative": (f"Price {(1 - spot / put_wall_oi) * 100:.1f}% below Put Wall {put_wall_oi:,.0f}: "
                          "the wall has given way and is not being tested."),
        }
    return {
        "signal": "WALL_HOLDING", "label": "Wall Holding",
        "tone": "flat",
        "narrative": "Structural OI walls are holding — no confluence or fakeout trigger.",
    }
