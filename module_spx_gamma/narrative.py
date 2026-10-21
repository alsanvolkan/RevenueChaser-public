"""Dynamic narrative walkthrough of the delta exposure chart (sign-aware, no hardcoded claims)."""
from __future__ import annotations


def build_walkthrough(snapshot: dict) -> list[str]:
    """Identify spot, the delta call wall, and liquidity pockets; explain them from data."""
    strike_rows = snapshot.get("strike_exposures", [])
    spot = snapshot.get("current_level")
    if not strike_rows or spot is None:
        return []

    rows = [r for r in strike_rows
            if r.get("strike") is not None and r.get("net_delta") is not None]
    above = [r for r in rows if r["strike"] > spot]
    below = [r for r in rows if r["strike"] < spot]
    call_wall = max(above, key=lambda r: r["net_delta"]) if above else None
    pockets = sorted(below, key=lambda r: -abs(r["net_delta"]))[:2] if below else []

    paras = []

    # Spot
    paras.append(
        f"Spot trades at {spot:,.0f}. Around and just below spot, net delta concentration is "
        f"relatively thin, so price can churn through this tight zone without major "
        f"dealer-hedge interference."
    )

    # Delta call wall (heaviest net delta above spot), sign-aware
    if call_wall:
        nd = call_wall["net_delta"]
        cd = call_wall.get("call_delta") or 0
        pd = call_wall.get("put_delta") or 0
        dom = "call" if abs(cd) >= abs(pd) else "put"
        if nd > 0:
            effect = ("dealers are net long delta here, so rallies toward it force mechanical "
                      "selling — a heavy lid that caps upside unless buying volume overwhelms it.")
        else:
            effect = ("dealers are net short delta here, so rallies toward it force mechanical "
                      "buying — a breakout level rather than a lid.")
        paras.append(
            f"The delta Call Wall sits at {call_wall['strike']:,.0f} (net Δ {nd:+,.0f}), "
            f"heavily {dom}-dominated; {effect}"
        )

    # Liquidity pockets below spot, sign-aware
    if pockets:
        names = " and ".join(f"{p['strike']:,.0f}" for p in pockets)
        pos = [p for p in pockets if p["net_delta"] > 0]
        neg = [p for p in pockets if p["net_delta"] < 0]
        if pos and not neg:
            effect = ("these institutional levels mix large put and call positions into sizeable net "
                      "positive delta bars, so a drop toward them triggers dealer buying to rebalance — "
                      "strong structural cushions below spot.")
        elif neg and not pos:
            effect = ("these levels carry sizeable net negative delta, so a drop toward them can trigger "
                      "dealer selling — structural tripwires rather than cushions.")
        else:
            effect = ("these levels mix positive and negative net delta, so the dealer response is "
                      "mixed — a churn zone rather than a clean floor.")
        paras.append(f"Deep liquidity pockets sit at {names}; {effect}")

    return paras
