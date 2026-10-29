"""Core level mapping and 0DTE (same-day expiry) strike-space metrics."""
from __future__ import annotations

from RevenueChaser.core.gex_profile import cumulative_crossings, nearest_crossing

from .config import to_float


def map_core_levels(gex_levels: dict) -> dict:
    """Map UW ``gex-levels`` fields onto the internal (technical) level keys."""
    gl = gex_levels or {}
    return {
        "equilibrium_point": to_float(gl.get("gamma_magnet")),
        "upper_resistance_band": to_float(gl.get("call_wall")),
        "lower_support_band": to_float(gl.get("put_wall")),
        "regime_threshold": to_float(gl.get("gamma_flip")),
    }


def map_flip_levels(gex_levels_oi: dict, gex_levels_vol: dict) -> dict:
    """Two gamma-flip values: all-tenor (OI) and 0DTE-ish (volume mode)."""
    oi = gex_levels_oi or {}
    vol = gex_levels_vol or {}
    return {
        "gamma_flip_all": to_float(oi.get("gamma_flip")),
        "gamma_flip_0dte": to_float(vol.get("gamma_flip")),
        "nearby_flips_oi": [to_float(x) for x in (oi.get("nearby_flips") or [])],
        "nearby_flips_vol": [to_float(x) for x in (vol.get("nearby_flips") or [])],
    }


def map_volume_levels(gex_levels_vol: dict) -> dict:
    """Intraday (directionalized-volume) walls/flip from ``gex-levels source=vol``.

    OI levels are the structural baseline; these volume-derived levels reflect the
    day's ask/bid-sided flow (the intraday "engine") and often pin much tighter.
    """
    vol = gex_levels_vol or {}
    return {
        "call_wall_vol": to_float(vol.get("call_wall")),
        "put_wall_vol": to_float(vol.get("put_wall")),
        "gamma_magnet_vol": to_float(vol.get("gamma_magnet")),
        "gamma_flip_vol": to_float(vol.get("gamma_flip")),
    }


DTE_BANDS = [
    ("0-30", lambda d: d is not None and d <= 30),
    ("31-90", lambda d: d is not None and 30 < d <= 90),
    ("91-180", lambda d: d is not None and 90 < d <= 180),
    ("181-365", lambda d: d is not None and 180 < d <= 365),
    ("365+", lambda d: d is not None and d > 365),
]


def _walls(rows: list[dict], spot: float) -> dict:
    """Put and call wall of ``rows``: the strike below / above spot whose net gamma, summed over the rows' expiries,
    is the largest (Fable review S5: the largest single (expiry, strike) cell let one big expiry win)."""
    by_strike: dict[float, float] = {}
    for r in rows:
        by_strike[r["strike"]] = by_strike.get(r["strike"], 0.0) + r["net_gamma"]
    below = [k for k in by_strike if k < spot]
    above = [k for k in by_strike if k > spot]
    pw = max(below, key=lambda k: by_strike[k]) if below else None
    cw = max(above, key=lambda k: by_strike[k]) if above else None
    return {
        "put_wall": pw,
        "put_wall_net": by_strike[pw] if pw is not None else None,
        "call_wall": cw,
        "call_wall_net": by_strike[cw] if cw is not None else None,
    }


def band_walls(strike_expiry_rows: list[dict], spot: float | None) -> list[dict]:
    """Per-DTE-band put/call walls (max positive net below/above spot, per strike summed over the band's expiries).

    The 181-365 day band yields the deepest
    put-support level (7500). Each row: band, put_wall, put_wall_net,
    call_wall, call_wall_net.
    """
    if spot is None:
        return []
    usable = [r for r in strike_expiry_rows if r.get("strike") is not None and r.get("net_gamma") is not None]
    out = [{"band": label, **_walls([r for r in usable if keep(r.get("dte"))], spot)} for label, keep in DTE_BANDS]
    # All expiries row
    out.append({"band": "ALL", **_walls(usable, spot)})
    return out


def _zero_dte(strike_expiry_rows: list[dict], asof_date: str) -> list[dict]:
    return [r for r in strike_expiry_rows if r.get("expiry") == asof_date]


def intraday_concentration(strike_expiry_rows: list[dict], asof_date: str) -> float | None:
    """0DTE Gamma Concentration: strike with the largest |net_gamma|."""
    valid = [r for r in _zero_dte(strike_expiry_rows, asof_date) if r.get("net_gamma") is not None]
    if not valid:
        return None
    best = max(valid, key=lambda r: abs(r["net_gamma"]))
    return best["strike"] if abs(best["net_gamma"]) != 0 else None


def intraday_put_concentration(strike_expiry_rows: list[dict], asof_date: str) -> float | None:
    """0DTE Put Concentration: strike with the most negative ``put_gex``."""
    valid = [r for r in _zero_dte(strike_expiry_rows, asof_date) if r.get("put_gex") is not None]
    if not valid:
        return None
    best = min(valid, key=lambda r: r["put_gex"])
    return best["strike"] if best["put_gex"] < 0 else None


def cumulative_gex_envelope(strike_rows: list[dict], spot: float | None, window_pct: float = 0.05) -> dict | None:
    """Absorption band vs air gap from the cumulative signed Net GEX profile.

    Mirrors the report's liquidity-layer section (Sekil 2): walking strikes from
    the bottom up within a window around spot, the running cumulative signed
    Net GEX forms two envelopes — the negative region is the "air gap"
    (amplifying, dealers trade WITH the move) and the positive region is the
    "absorption" (dampening, dealers trade AGAINST the move). The zero-crossing
    of the cumulative profile approximates the gamma flip.

    The window is kept tight around spot (±5%) so the bands reflect today's
    dealer-flow regime, not a far-away call-wall concentration. When no positive
    (or negative) cumulative region exists inside the window, that band is None.

    Returns a dict with the absorption band, air-gap band, their net values,
    the zero-crossing, and per-25pt hedge intensity, or None when data is
    insufficient.
    """
    if spot is None:
        return None
    lo_all = spot * (1 - window_pct)
    hi_all = spot * (1 + window_pct)
    rows = sorted(
        (r for r in strike_rows
         if r.get("strike") is not None and r.get("net_gamma") is not None
         and lo_all <= r["strike"] <= hi_all),
        key=lambda r: r["strike"],
    )
    if len(rows) < 2:
        return None

    # cumulative runs sign -> strike interval [lo, hi]
    cumulative = 0.0
    runs: list[dict] = []
    current_sign = None
    lo = hi = 0.0
    prev_strike = None
    for r in rows:
        strike = r["strike"]
        cumulative += r["net_gamma"]
        sign = 1 if cumulative > 0 else (-1 if cumulative < 0 else current_sign or 0)
        if sign != current_sign:
            if current_sign is not None:
                runs.append({"sign": current_sign, "lo": lo, "hi": prev_strike})
            lo = strike
            current_sign = sign
        hi = strike
        prev_strike = strike
    if current_sign is not None:
        runs.append({"sign": current_sign, "lo": lo, "hi": hi})

    def _net(lo: float, hi: float) -> float:
        return sum((r["net_gamma"] or 0.0) for r in rows if lo <= r["strike"] <= hi)

    def _per25(lo: float, hi: float, net: float) -> float | None:
        width = hi - lo
        return abs(net) * 25.0 / width if width > 0 else None

    positive = [r for r in runs if r["sign"] > 0]
    negative = [r for r in runs if r["sign"] < 0]

    def _dist(run: dict) -> float:
        # distance from spot to the nearest edge of the band (anchored bands)
        lo, hi = run["lo"], run["hi"]
        if spot is not None:
            if lo <= spot <= hi:
                return 0.0
            return min(abs(spot - lo), abs(spot - hi))
        return 0.0

    # pick the band whose interval is closest to (or contains) spot, so the
    # "liquidity layer" reflects the dealer-flow regime around today's price,
    # not a far-away call-wall concentration.
    absorption = min(positive, key=_dist) if positive else None
    air_gap = min(negative, key=_dist) if negative else None

    def _band(run: dict | None, kind: str) -> dict | None:
        if run is None:
            return None
        net = _net(run["lo"], run["hi"])
        return {
            "kind": kind,
            "lo": run["lo"],
            "hi": run["hi"],
            "net_gex": net,
            "per_25pt": _per25(run["lo"], run["hi"], net),
        }

    # The same zero-crossing walk the regime cross-check uses (core.gex_profile).
    crossings = cumulative_crossings([(r["strike"], r["net_gamma"]) for r in rows])
    crossing = nearest_crossing(crossings, spot) if spot is not None else (crossings[0] if crossings else None)

    return {
        "absorption": _band(absorption, "absorption"),
        "air_gap": _band(air_gap, "air_gap"),
        "zero_crossing": crossing,
    }


def cumulative_gex_crossing(strike_expiry_rows: list[dict], asof_date: str, spot: float | None) -> float | None:
    """0DTE Cumulative GEX Crossing: interpolated cumulative net-GEX sign change nearest spot."""
    pts = [(r["strike"], r["net_gamma"]) for r in _zero_dte(strike_expiry_rows, asof_date)
           if r.get("strike") is not None and r.get("net_gamma") is not None]
    return nearest_crossing(cumulative_crossings(pts), spot)
