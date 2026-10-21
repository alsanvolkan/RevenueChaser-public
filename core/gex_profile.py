"""Strike-level GEX profile for single-name earnings (R4 vehicle layer).

Consumes the same payload shape as ``module_spx_gamma`` reads from
``/stock/{ticker}/spot-exposures/strike`` but is deliberately self-contained:
earnings scoring must not import a dashboard module.

Two books exist in the payload:
- ``open_interest`` (``*_oi``) — structural, slow-moving, far walls;
- ``directionalized_volume`` (``*_vol``) — today's flow, MM-short-on-ask /
  MM-long-on-bid. For an earnings-night vehicle read the **volume** book is the
  live surface (UW: use the volume flip for the post-earnings open).

Everything here is pure: feed it the raw ``payload`` (or normalized rows) and a
spot and get deterministic levels. Missing fields stay ``None``, never 0.
"""
from __future__ import annotations

MODE_OPEN_INTEREST = "open_interest"
MODE_DIRECTIONALIZED_VOLUME = "directionalized_volume"

_GREEKS = ("gamma", "charm", "vanna")


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _suffix(mode: str) -> str:
    return "_oi" if mode == MODE_OPEN_INTEREST else "_vol"


def _net(rows: list[dict], greek: str, mode: str, index: int) -> float | None:
    r = rows[index]
    s = _suffix(mode)
    c = _to_float(r.get(f"call_{greek}{s}"))
    p = _to_float(r.get(f"put_{greek}{s}"))
    if c is None and p is None:
        return None
    return (c or 0.0) + (p or 0.0)


def normalize_strike_rows(payload: dict | list | None) -> list[dict]:
    """Flatten spot-exposures into strike rows (both books retained).

    Accepts either the raw payload (``{"data": [...]}``) or the client's
    already-unwrapped row list (``UWClient.spot_exposures_by_strike``).
    """
    if payload is None:
        return []
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    rows = []
    for r in data:
        if not isinstance(r, dict):
            continue
        strike = _to_float(r.get("strike"))
        if strike is None:
            continue
        row = {"strike": strike}
        for g in _GREEKS:
            for book in ("_oi", "_vol"):
                row[f"call_{g}{book}"] = _to_float(r.get(f"call_{g}{book}"))
                row[f"put_{g}{book}"] = _to_float(r.get(f"put_{g}{book}"))
        rows.append(row)
    rows.sort(key=lambda x: x["strike"])
    return rows


def net_gamma(row: dict, mode: str = MODE_DIRECTIONALIZED_VOLUME) -> float | None:
    s = _suffix(mode)
    c = _to_float(row.get(f"call_gamma{s}"))
    p = _to_float(row.get(f"put_gamma{s}"))
    if c is None and p is None:
        return None
    return (c or 0.0) + (p or 0.0)


def cumulative_crossings(points: list[tuple[float, float | None]]) -> list[float]:
    """Strikes where the running sum of ``value`` over ascending strikes changes sign.

    ``points`` are ``(strike, value)`` pairs in any order; ``None`` values are skipped.
    Each crossing is interpolated between the two strikes around it, weighted by the
    cumulative magnitude on either side. A cumulative that touches exactly zero is not
    a crossing. This is the one zero-crossing walk the SPX Gamma board and the earnings
    profile share.
    """
    cum = 0.0
    prev_strike = None
    prev_cum = None
    crossings: list[float] = []
    for strike, value in sorted((p for p in points if p[1] is not None), key=lambda p: p[0]):
        cum += value
        if prev_strike is not None and prev_cum is not None:
            if (prev_cum > 0) != (cum > 0) and prev_cum != 0 and cum != 0:
                denom = abs(prev_cum) + abs(cum)
                if denom > 0:
                    w = abs(prev_cum) / denom
                    crossings.append(prev_strike + w * (strike - prev_strike))
        prev_strike = strike
        prev_cum = cum
    return crossings


def nearest_crossing(crossings: list[float], spot: float | None) -> float | None:
    """The crossing closest to ``spot``; None without crossings or a spot."""
    if not crossings or spot is None:
        return None
    return min(crossings, key=lambda c: abs(c - spot))


def volume_flip(rows: list[dict], spot: float | None) -> float | None:
    """Nearest zero-crossing of cumulative net gamma (directionalized volume).

    Flip = price where cumulative net gamma crosses zero, interpolated between the
    two strikes whose cumulative sign flips; the crossing nearest to ``spot``.
    """
    if not rows or spot is None:
        return None
    pts = [(row["strike"], net_gamma(row, MODE_DIRECTIONALIZED_VOLUME)) for row in rows]
    return nearest_crossing(cumulative_crossings(pts), spot)


LONG_GAMMA_LABEL = "Long Gamma — Dampening"
SHORT_GAMMA_LABEL = "Short Gamma — Amplifying"
AT_FLIP_LABEL = "At the gamma flip"


def _side(spot: float, flip: float | None) -> str | None:
    if flip is None:
        return None
    return "long" if spot > flip else ("short" if spot < flip else "at_flip")


def regime(spot: float | None, flip_oi: float | None, flip_vol: float | None = None,
           cumulative_flip: float | None = None) -> dict:
    """The one gamma-regime decision: where spot sits against the gamma flip.

    Above the flip dealers are long gamma (they trade against the move, dampening);
    below it they are short gamma (with the move, amplifying). ``flip_oi`` (UW's
    open-interest flip) decides. UW does not document how it computes its flip, and the
    OI and volume flips can differ, so ``flip_vol`` and a locally computed
    ``cumulative_flip`` are returned side by side as cross-checks and never vote.
    Missing spot or flip is ``unknown`` with no label, never a default regime.
    """
    flips = {"oi": flip_oi, "vol": flip_vol, "cumulative": cumulative_flip}
    if spot is None:
        sides = {k: None for k in flips}
    else:
        sides = {k: _side(spot, v) for k, v in flips.items()}
    state = sides["oi"] or "unknown"
    label = {"long": LONG_GAMMA_LABEL, "short": SHORT_GAMMA_LABEL,
             "at_flip": AT_FLIP_LABEL}.get(state)
    seen = {v for v in sides.values() if v is not None}
    return {
        "state": state,
        "label": label,
        "spot": spot,
        "flip": flip_oi,
        "flip_source": "oi",
        "flips": flips,
        "sides": sides,
        "agree": (len(seen) <= 1) if state != "unknown" else None,
    }


def magnet(rows: list[dict], spot: float | None,
           window_pct: float = 0.15) -> dict:
    """Strike with the largest |net gamma| in an ATM band around spot (vol book)."""
    if not rows or spot is None:
        return {}
    lo, hi = spot * (1 - window_pct), spot * (1 + window_pct)
    best = None
    for row in rows:
        ng = net_gamma(row, MODE_DIRECTIONALIZED_VOLUME)
        if ng is None or not (lo <= row["strike"] <= hi):
            continue
        if best is None or abs(ng) > abs(best["net"]):
            best = {"strike": row["strike"], "net": ng}
    if not best:
        return {}
    return {
        "strike": best["strike"],
        "signed": best["net"],
        "distance_pct": round((best["strike"] - spot) / spot, 4),
        "side": "above" if best["strike"] > spot else "below",
    }


def _charm_net(row: dict, mode: str = MODE_DIRECTIONALIZED_VOLUME) -> float | None:
    s = _suffix(mode)
    c = _to_float(row.get(f"call_charm{s}"))
    p = _to_float(row.get(f"put_charm{s}"))
    if c is None and p is None:
        return None
    return (c or 0.0) + (p or 0.0)


def charm_pin(rows: list[dict], spot: float | None,
              near_pct: float = 0.05, dominance_ratio: float = 1.5) -> dict:
    """Charm Pin (near spot + dominant) vs Charm Peak, directionalized volume."""
    if not rows or spot is None:
        return {}
    cands = [r for r in rows if _charm_net(r) is not None]
    if not cands:
        return {}
    cands.sort(key=lambda r: abs(_charm_net(r)), reverse=True)
    top = cands[0]
    top_charm = _charm_net(top)
    dist_pct = abs(top["strike"] - spot) / spot if spot else None
    dominance = None
    if len(cands) > 1:
        second = abs(_charm_net(cands[1]))
        if second > 0:
            dominance = abs(top_charm) / second
    is_near = dist_pct is not None and dist_pct <= near_pct
    is_dominant = dominance is not None and dominance >= dominance_ratio
    return {
        "label": "Charm Pin" if (is_near and is_dominant) else "Charm Peak",
        "strike": top["strike"],
        "signed": top_charm,
        "distance_pct": round(dist_pct, 4) if dist_pct is not None else None,
        "dominance_ratio": round(dominance, 2) if dominance is not None else None,
        "is_near_spot": is_near,
        "is_dominant": is_dominant,
    }


def vanna_lobes(rows: list[dict], spot: float | None,
                window_pct: float = 0.25) -> dict:
    """Call-side and put-side vanna concentration (directionalized volume).

    Window defaults wider (0.25) than magnet's 0.15: vanna lobes sit at the
    walls (UW: put lobe below, call lobe up at the dominant wall), and near-spot
    vanna is deliberately small and not the lobe.
    """
    if not rows or spot is None:
        return {}
    lo, hi = spot * (1 - window_pct), spot * (1 + window_pct)
    calls, puts = [], []
    for row in rows:
        if not (lo <= row["strike"] <= hi):
            continue
        s = _suffix(MODE_DIRECTIONALIZED_VOLUME)
        cv = _to_float(row.get(f"call_vanna{s}"))
        pv = _to_float(row.get(f"put_vanna{s}"))
        if cv is not None:
            calls.append((cv, row["strike"]))
        if pv is not None:
            puts.append((pv, row["strike"]))
    out = {}
    if calls:
        peak_cv, peak_strike = max(calls, key=lambda t: abs(t[0]))
        out["call_peak"] = {"strike": peak_strike, "signed": peak_cv}
    if puts:
        peak_pv, peak_strike = max(puts, key=lambda t: abs(t[0]))
        out["put_peak"] = {"strike": peak_strike, "signed": peak_pv}
    return out


def build_profile(rows: list[dict], spot: float | None, em_pct: float | None = None,
                  window_pct: float = 0.15) -> dict:
    """One deterministic profile dict for R4.

    Returns only the keys R4 actually consumes; anything absent means the
    strike payload did not carry it (never invented).

    ``regime`` follows the heatmap semantics, not an interpolated flip: it is
    the **sign of net gamma inside the expected-move band** around spot
    (red = negative = short gamma / amplified; green = positive = long gamma /
    damped). The band is ``em_pct`` when given, else a default ATM window.
    """
    if not rows or spot is None:
        return {}
    prof: dict = {"book": MODE_DIRECTIONALIZED_VOLUME}

    band = (em_pct or 0.0) / 100.0 if em_pct else window_pct
    band = max(band, 0.03)
    in_band = [r for r in rows if abs(r["strike"] - spot) / spot <= band]
    net_in = sum(net_gamma(r) or 0.0 for r in in_band)
    if in_band:
        prof["regime"] = "short" if net_in < 0 else "long"

    flip = volume_flip(rows, spot)
    if flip is not None:
        prof["flip"] = round(flip, 2)
    mag = magnet(rows, spot, window_pct)
    if mag:
        prof["magnet"] = mag
    charm = charm_pin(rows, spot)
    if charm:
        prof["charm"] = charm
    vanna = vanna_lobes(rows, spot, window_pct)
    if vanna:
        prof["vanna"] = vanna
    return prof
