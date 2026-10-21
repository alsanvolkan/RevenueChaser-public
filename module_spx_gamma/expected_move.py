"""Expected move: spot * annualized_iv / sqrt(252); None when IV missing or invalid."""
from __future__ import annotations

from .config import IV_MAX, IV_MIN, to_float


def expected_move(spot: float | None, annualized_iv: float | None, trading_days: int = 252) -> dict | None:
    if not spot or not annualized_iv:
        return None
    if annualized_iv <= IV_MIN or annualized_iv > IV_MAX:
        return None  # unit sanity gate: reject implausible annualized IV
    move = spot * annualized_iv / (trading_days ** 0.5)
    return {"points": move, "lower": spot - move, "upper": spot + move, "iv_used": annualized_iv}


def pick_iv(iv_rows: list[dict]) -> float | None:
    """Annualized ATM IV for the 1-day tenor from ``/interpolated-iv``."""
    if not iv_rows:
        return None
    for r in iv_rows:
        if to_float(r.get("days")) == 1 and to_float(r.get("volatility")) is not None:
            return to_float(r["volatility"])
    candidates = [r for r in iv_rows if to_float(r.get("volatility")) is not None]
    if not candidates:
        return None
    best = min(candidates, key=lambda r: to_float(r.get("days")) if to_float(r.get("days")) is not None else 1e9)
    return to_float(best["volatility"])
