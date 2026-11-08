"""The 22:00 close check (A3): the 02-06 Nov report's order for reading a session's close.

1. Price band: which scenario band the close sits in. 2. VIX threshold: VIX against a threshold. 3. Week gamma: the
sign of the week's net gamma and the close against its turning point. The report's VIX threshold is MenthorQ's VIX
HVL plus a one-day VIX move; MenthorQ is not a source here, so the board uses a stated substitute: the prior week's
last VIX close plus the average one-day VIX move over the stored window.
"""
from __future__ import annotations

from datetime import date, timedelta

from .scenarios import realized_class, scenario_bands

NOTE = ("VIX threshold is a substitute for the report's VIX HVL, which comes from a source the board does not use: "
        "the prior week's last VIX close plus the average one-day VIX move over the stored window (about two weeks).")


def vix_threshold(history: list[dict], asof_date: str) -> dict | None:
    rows = sorted((h for h in history or [] if h.get("close") is not None), key=lambda h: h["date"])
    if len(rows) < 3:
        return None
    d = date.fromisoformat(asof_date)
    monday = (d - timedelta(days=d.weekday())).isoformat()
    prior = [h for h in rows if h["date"] < monday]
    if not prior:
        return None
    moves = [abs(b["close"] - a["close"]) for a, b in zip(rows, rows[1:])]
    one_day = sum(moves) / len(moves)
    base = prior[-1]
    return {"base": base["close"], "base_date": base["date"], "one_day_move": one_day,
            "threshold": base["close"] + one_day}


def close_card(snapshot: dict) -> dict:
    spot = snapshot.get("current_level")
    steps = []

    bands = scenario_bands(snapshot) if spot is not None else None
    cls = realized_class(bands, spot) if bands else None
    if cls:
        lo, hi = bands[cls.lower()]
        span = f"{lo:,.0f} – {hi:,.0f}" if hi is not None else f"above {lo:,.0f}"
        steps.append({"step": "Price band", "state": cls.lower(),
                      "reading": f"close {spot:,.2f} is in {cls} ({span})"})
    else:
        steps.append({"step": "Price band", "state": None, "reading": "price band n/a"})

    vix = snapshot.get("vix") or {}
    t = vix_threshold(vix.get("history") or [], snapshot.get("asof_date") or "") if snapshot.get("asof_date") else None
    v = vix.get("vix")
    if t and v is not None:
        above = v > t["threshold"]
        steps.append({"step": "VIX threshold", "state": "stress" if above else "calm",
                      "reading": (f"VIX {v:.2f} is {'above' if above else 'at or below'} {t['threshold']:.2f} "
                                  f"({t['base_date']} close {t['base']:.2f} + one-day move {t['one_day_move']:.2f})")})
    else:
        steps.append({"step": "VIX threshold", "state": None, "reading": "VIX threshold n/a"})

    wg = snapshot.get("week_gamma") or {}
    g, tp = wg.get("net_gamma"), wg.get("turning_point")
    if g is not None and spot is not None:
        sign = "positive" if g > 0 else "negative" if g < 0 else "flat"
        where = (f", close {'above' if spot > tp else 'below'} the turning point {tp:,.0f}" if tp is not None else "")
        steps.append({"step": "Week gamma", "state": sign,
                      "reading": f"week net gamma is {sign} (expiries through {wg.get('target')}){where}"})
    else:
        steps.append({"step": "Week gamma", "state": None, "reading": "week gamma n/a"})
    return {"steps": steps, "note": NOTE}
