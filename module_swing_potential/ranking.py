"""Swing ranking — weighted composite, RS rating percentile, execution levels.

Pure helpers: no network, no I/O. The pipeline feeds in the raw metric values
and receives 0-100 normalized scores plus ATR-based entry/stop/target levels.
"""
from __future__ import annotations

from . import config
from . import settings as swing_settings


def percentile_ranks(pairs: list[tuple]) -> dict:
    """Map each key to its 0-100 percentile rank within non-None values."""
    valid = [(k, float(v)) for k, v in pairs if v is not None and float(v) == float(v)]
    if not valid:
        return {}
    n = len(valid)
    if n == 1:
        return {k: 100.0 for k, _ in valid}
    sorted_vals = sorted(v for _, v in valid)
    out = {}
    for k, v in valid:
        less = _count_less(sorted_vals, v)
        out[k] = round(less / (n - 1) * 100.0, 2)
    return out


def _count_less(sorted_vals: list[float], v: float) -> int:
    import bisect
    return bisect.bisect_left(sorted_vals, v)


def composite(mom_pct: float | None, prox_pct: float | None, cons_pct: float | None, eff=None) -> float | None:
    """Weighted composite 0-100 = 0.50·momentum + 0.25·proximity + 0.25·consistency (the tie-break).

    The proximity and consistency weights come from the settings (#384 SW-S4); momentum's does not (it is
    not editable). The weights are normalised by their own sum, so only their ratio moves the ranking.
    """
    e = eff or swing_settings.effective()
    parts = []
    weights = []
    for pct, w in ((mom_pct, config.W_12_1), (prox_pct, e.w_prox), (cons_pct, e.w_cons)):
        if pct is not None:
            parts.append(pct * w)
            weights.append(w)
    if not weights or sum(weights) <= 0:      # both editable weights may be 0: no weight left, no composite
        return None
    return round(sum(parts) / sum(weights), 2)


def quality_403030(m_count: int, w_count: int, q_count: int) -> int:
    """#65's 40/30/30 quality: ``round(40·M/8 + 30·W/5 + 30·Q/6)`` (#384 SW-S1).

    ``M``/``W``/``Q`` are the numbers of KNOWN-passed rules in the Minervini (8), Weinstein (5) and
    Qullamaggie (6) gates. The weights and rule totals live in ``config``.
    """
    score = sum(config.Q_WEIGHTS[key] * count / config.Q_RULE_TOTALS[key]
                for key, count in (("minervini", m_count), ("weinstein", w_count), ("qullamaggie", q_count)))
    return int(round(score))


def execution_levels(ind: dict, eff=None) -> dict:
    """ATR-based entry / stop / target / R:R (the stop and target multiples come from the settings)."""
    e = eff or swing_settings.effective()
    entry = ind.get("price")
    atr = ind.get("atr14")
    if entry is None or not atr:
        return {"entry": entry, "stop": None, "target": None, "rr": None, "atr": atr}
    stop = entry - e.stop_atr * atr
    risk = entry - stop
    target = entry + e.target_r * risk
    rr = e.target_r if risk > 0 else None
    return {
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "rr": round(rr, 2) if rr is not None else None,
        "atr": round(atr, 2),
    }


def ma_alignment(ind: dict) -> str:
    s50, s150, s200, price = ind.get("sma50"), ind.get("sma150"), ind.get("sma200"), ind.get("price")
    if None in (s50, s150, s200):
        return "unknown"
    if s50 > s150 > s200 and price is not None and price > s50:
        return "aligned"
    if s50 > s150 and s50 > s200:
        return "partial"
    return "broken"
