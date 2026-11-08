"""Tail probabilities against 5-session frequencies (A9, the weekly reports' tail table).

Implied: a two-sided normal on the 5-session log return, sigma = annualized IV x sqrt(5 / 252). Realized: the share of
overlapping 5-session windows in the stored closes whose |log return| exceeded the same move. The window is the
board's stored closes (about 8 weeks), so the realized column is a small sample and says how many windows it has.
"""
from __future__ import annotations

import math

TAIL_MOVES = (0.02, 0.03, 0.05)
HORIZON = 5


def implied_tail(annual_iv: float | None, move: float, horizon: int = HORIZON) -> float | None:
    if not annual_iv or annual_iv <= 0:
        return None
    sigma = annual_iv * math.sqrt(horizon / 252)
    return 2 * (1 - 0.5 * (1 + math.erf(move / sigma / math.sqrt(2))))


def realized_tail(closes: list[float], move: float, horizon: int = HORIZON) -> dict | None:
    c = [float(x) for x in closes if x]
    if len(c) <= horizon:
        return None
    rets = [abs(math.log(c[i + horizon] / c[i])) for i in range(len(c) - horizon)]
    hits = sum(1 for r in rets if r > move)
    return {"windows": len(rets), "hits": hits, "freq": hits / len(rets)}


def tail_table(closes: list[float], annual_iv: float | None) -> list[dict]:
    """One row per move in TAIL_MOVES; [] without an IV or enough closes."""
    if not annual_iv or len([x for x in closes or [] if x]) <= HORIZON:
        return []
    return [{"move_pct": m * 100, "implied": implied_tail(annual_iv, m), "realized": realized_tail(closes, m)}
            for m in TAIL_MOVES]
