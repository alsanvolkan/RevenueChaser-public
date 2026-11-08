"""The week-gamma profile, its turning point and the zone map (A1, the weekly reports' core).

Gamma is read only over the expiries from the board's day through the target Friday (the day the week resolves on).
The turning point is where the week profile's cumulative net gamma changes sign nearest spot. The zone map walks the
strikes within +-5 % of spot. The reports name the zones but print no thresholds, so the board's rule is its own and
is stated on the page: negative gamma is an air gap; positive gamma is a dense block at 2x or more of the window's
mean |gamma|, a braked staircase from 0.5x to 2x, and half-full below 0.5x.
"""
from __future__ import annotations

from RevenueChaser.core.gex_profile import cumulative_crossings, nearest_crossing

from .brier import target_friday

WINDOW_PCT = 0.05
BUCKET = 25.0     # strikes are summed into 25-point buckets first: 5-point SPX strikes split the map into dozens of
                  # one-strike zones (UW 4 Nov, expiries 5-6 Nov: 25 zones unbucketed, 5 with 25-point buckets)
DENSE_X = 2.0
THIN_X = 0.5
RULE = ("Board rule (the reports name the zones but print no thresholds): negative gamma = air gap; positive "
        "gamma = dense block at 2x or more of the window's mean |gamma|, braked staircase 0.5x-2x, half-full "
        "below 0.5x. Strikes summed in 25-point buckets, window +-5 % of spot, week = expiries through the target "
        "Friday.")


def week_profile(rows: list[dict], asof_date: str, target: str) -> list[tuple[float, float]]:
    by: dict[float, float] = {}
    for r in rows or []:
        e, k, g = r.get("expiry"), r.get("strike"), r.get("net_gamma")
        if e is None or k is None or g is None or not (asof_date <= str(e) <= target):
            continue
        by[k] = by.get(k, 0.0) + g
    return sorted(by.items())


def _kind(g: float, mean_abs: float) -> str:
    if g < 0:
        return "Air gap"
    x = abs(g) / mean_abs if mean_abs else 0.0
    if x >= DENSE_X:
        return "Dense block"
    if x >= THIN_X:
        return "Braked staircase"
    return "Half-full"


def bucketed(profile: list[tuple[float, float]], width: float = BUCKET) -> list[tuple[float, float]]:
    out: dict[float, float] = {}
    for k, g in profile:
        b = (k // width) * width
        out[b] = out.get(b, 0.0) + g
    return sorted(out.items())


def window_bars(profile: list[tuple[float, float]], spot: float | None,
                width: float = BUCKET) -> list[tuple[float, float]]:
    """The bucketed profile inside the zone window (+-5 % of spot): the bars the board draws."""
    if not spot:
        return []
    return [(k, g) for k, g in bucketed(profile, width) if abs(k + width / 2 - spot) <= spot * WINDOW_PCT]


def zones(profile: list[tuple[float, float]], spot: float | None, width: float = BUCKET) -> list[dict]:
    """Zones over the bucketed profile; each zone's range is [lo, hi) in index points."""
    if not spot:
        return []
    win = window_bars(profile, spot, width)
    if not win:
        return []
    total_abs = sum(abs(g) for _, g in win)
    mean_abs = total_abs / len(win)
    out: list[dict] = []
    for k, g in win:
        kind = _kind(g, mean_abs)
        if out and out[-1]["kind"] == kind:
            out[-1]["hi"] = k + width
            out[-1]["gamma"] += g
            out[-1]["abs"] += abs(g)
        else:
            out.append({"kind": kind, "lo": k, "hi": k + width, "gamma": g, "abs": abs(g)})
    for z in out:
        z["share_pct"] = z.pop("abs") / total_abs * 100.0 if total_abs else 0.0
    return out


def week_map(rows: list[dict], asof_date: str, spot: float | None) -> dict | None:
    target = target_friday(asof_date)
    prof = week_profile(rows, asof_date, target)
    if not prof:
        return None
    expiries = sorted({str(r["expiry"]) for r in rows if r.get("expiry") and asof_date <= str(r["expiry"]) <= target
                       and r.get("net_gamma") is not None})
    crossings = cumulative_crossings(prof)
    return {"target": target, "expiries": expiries, "net_gamma": sum(g for _, g in prof),
            "turning_point": nearest_crossing(crossings, spot) if spot else (crossings[0] if crossings else None),
            "zones": zones(prof, spot), "bars": window_bars(prof, spot), "rule": RULE}
