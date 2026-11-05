"""A2: the weekly reports' trade structures, priced on the target Friday's quoted chain.

**Report structure, not advice.** The board prices the shapes the reports print (30 Oct 2026: long put condor as the
main trade, a put debit spread for the downside scenario; 16-23 Oct: iron condor and credit spread) from this week's
band edges, so a reader sees what each costs on UW's quotes now. It picks no trade and sizes nothing.

Strikes come from the scenario bands (``scenarios.scenario_bands``), snapped to the 25-point grid the reports use;
a wing is ``WING`` (50 points, "the next stop") beyond its short leg. A leg prices only at its exact strike with a
quoted mid (``chains.normalize_option_chain`` price_source "quote"): a nearest-strike fallback would price a
different trade, and a modeled mid is not a market price. Values are SPX index points; XSP is a tenth of SPX.

Payoff at expiry is piecewise linear between strikes, so maximum profit, maximum loss and breakevens are read off
its kinks exactly (no grid).

shortcut: the report's further rules (short leg outside the 1-sd band, minimum credit, entry windows, stops, the
candidate score) are not modelled; the strikes are the band edges.
"""
from __future__ import annotations

GRID = 25.0
WING = 50.0
#: Downside and upside debit spreads buy one grid step inside the band edge: the report's confirm level (7575).
DEBIT_OFFSET = 25.0
LABEL = "Report structure, not advice: priced from UW quotes (mid) on the target Friday's chain."
XSP_PER_SPX = 0.1


def snap_strike(level: float) -> float:
    return round(float(level) / GRID) * GRID


def _intrinsic(typ: str, strike: float, s: float) -> float:
    return max(s - strike, 0.0) if typ == "C" else max(strike - s, 0.0)


def price_structure(name: str, legs, chain, family: str = "") -> dict:
    """Price ``legs`` = [(+1 long / -1 short, "P"/"C", strike)] off ``chain``; net > 0 is a credit."""
    quotes = {(r.get("type"), float(r["strike"])): r.get("mid") for r in chain or []
              if r.get("price_source") == "quote" and r.get("mid") is not None and r.get("strike") is not None}
    out_legs, missing, net = [], [], 0.0
    for sign, typ, strike in legs:
        mid = quotes.get((typ, float(strike)))
        out_legs.append({"action": "BUY" if sign > 0 else "SELL", "type": typ, "strike": float(strike), "mid": mid})
        if mid is None:
            missing.append(f"{typ} {strike:,.0f}")
        else:
            net -= sign * mid
    result = {"name": name, "family": family, "legs": out_legs, "missing": missing, "net": None,
              "max_profit": None, "max_loss": None, "breakevens": [], "reward_risk": None, "xsp_net": None}
    if missing:
        return result

    strikes = sorted({float(k) for _s, _t, k in legs})
    points = [strikes[0] - 1000.0] + strikes + [strikes[-1] + 1000.0]

    def pnl(s: float) -> float:
        return net + sum(sign * _intrinsic(typ, k, s) for sign, typ, k in legs)

    values = [pnl(s) for s in points]
    bounded = abs(values[0] - values[1]) < 1e-9 and abs(values[-1] - values[-2]) < 1e-9
    breakevens = []
    for (s0, v0), (s1, v1) in zip(zip(points, values), zip(points[1:], values[1:])):
        if v0 == 0.0 and (not breakevens or breakevens[-1] != s0) and s0 not in (points[0],):
            breakevens.append(s0)
        elif v0 * v1 < 0:
            breakevens.append(s0 + (s1 - s0) * (-v0) / (v1 - v0))
    hi, lo = max(values), min(values)
    result.update({
        "net": round(net, 2),
        "max_profit": round(hi, 2) if bounded else None,
        "max_loss": round(-lo, 2) if bounded else None,
        "breakevens": [round(b, 2) for b in breakevens],
        "reward_risk": round(hi / -lo, 2) if bounded and lo < 0 and hi > 0 else None,
        "xsp_net": round(net * XSP_PER_SPX, 2),
    })
    return result


def build_structures(bands: dict | None, chain, expiry: str | None) -> dict | None:
    """The five report shapes on this week's bands; None without bands or a chain."""
    if not bands or not chain:
        return None
    cons_lo, cons_hi = (snap_strike(v) for v in bands["consolidation"])
    down_lo, down_hi = (snap_strike(v) for v in bands["downside"])
    up_lo, up_hi = (snap_strike(v) if v is not None else None for v in bands["upside"])
    shapes = [
        ("Iron condor", "consolidation", [(+1, "P", cons_lo - WING), (-1, "P", cons_lo),
                                          (-1, "C", cons_hi), (+1, "C", cons_hi + WING)]),
        ("Put credit spread", "consolidation", [(-1, "P", cons_lo), (+1, "P", cons_lo - WING)]),
        ("Long put condor", "downside", [(+1, "P", down_hi + WING), (-1, "P", down_hi),
                                         (-1, "P", down_lo), (+1, "P", down_lo - WING)]),
        ("Put debit spread", "downside", [(+1, "P", down_hi - DEBIT_OFFSET), (-1, "P", down_lo)]),
    ]
    if up_hi is not None:   # an open upside (pin at or below the flip) has no short leg to sell
        shapes.append(("Call debit spread", "upside", [(+1, "C", up_lo + DEBIT_OFFSET), (-1, "C", up_hi)]))
    structures = []
    for name, family, legs in shapes:
        if len({k for _s, _t, k in legs}) < len(legs) or any(
                legs[i][2] == legs[i + 1][2] for i in range(len(legs) - 1)):
            continue   # a band narrower than a grid step collapses a spread: no structure
        structures.append(price_structure(name, legs, chain, family))
    return {"expiry": expiry, "label": LABEL, "structures": structures}


__all__ = ["GRID", "WING", "LABEL", "snap_strike", "price_structure", "build_structures"]
