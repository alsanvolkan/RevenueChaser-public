"""Charm Peak/Pin qualification and Vanna Strike from spot-exposure data."""
from __future__ import annotations

from .config import CHARM_PIN_DOMINANCE_RATIO, CHARM_PIN_MAX_DISTANCE_PCT, net_sum, to_float

MODE_OPEN_INTEREST = "open_interest"
MODE_DIRECTIONALIZED_VOLUME = "directionalized_volume"


def normalize_spot_exposure_rows(payload: dict | None) -> list[dict]:
    if not payload or "data" not in payload:
        return []
    rows = []
    for r in payload["data"]:
        rows.append({
            "strike": to_float(r.get("strike")),
            "call_charm_oi": to_float(r.get("call_charm_oi")),
            "put_charm_oi": to_float(r.get("put_charm_oi")),
            "call_charm_vol": to_float(r.get("call_charm_vol")),
            "put_charm_vol": to_float(r.get("put_charm_vol")),
            "call_vanna_oi": to_float(r.get("call_vanna_oi")),
            "put_vanna_oi": to_float(r.get("put_vanna_oi")),
            "call_vanna_vol": to_float(r.get("call_vanna_vol")),
            "put_vanna_vol": to_float(r.get("put_vanna_vol")),
            "call_gamma_oi": to_float(r.get("call_gamma_oi")),
            "put_gamma_oi": to_float(r.get("put_gamma_oi")),
            "call_gamma_vol": to_float(r.get("call_gamma_vol")),
            "put_gamma_vol": to_float(r.get("put_gamma_vol")),
        })
    return rows


def _suffix(mode: str) -> str:
    return "_oi" if mode == MODE_OPEN_INTEREST else "_vol"


def net_charm(row: dict, mode: str) -> float | None:
    s = _suffix(mode)
    return net_sum(row.get(f"call_charm{s}"), row.get(f"put_charm{s}"))


def charm_direction(total: float | None) -> str | None:
    """The one reading of a summed charm's sign, shared by the Relations card and the Charm card (review N1):
    positive buys, negative sells, an exact zero is flat, and no value has no direction."""
    if total is None:
        return None
    if total > 0:
        return "Buy pressure"
    if total < 0:
        return "Sell pressure"
    return "Flat"


def net_vanna(row: dict, mode: str) -> float | None:
    s = _suffix(mode)
    return net_sum(row.get(f"call_vanna{s}"), row.get(f"put_vanna{s}"))


def call_vanna(row: dict, mode: str) -> float | None:
    return to_float(row.get(f"call_vanna{_suffix(mode)}"))


def put_vanna(row: dict, mode: str) -> float | None:
    return to_float(row.get(f"put_vanna{_suffix(mode)}"))


def qualify_charm(rows: list[dict], spot: float | None, mode: str = MODE_OPEN_INTEREST) -> dict:
    """Deterministic Charm Peak vs Charm Pin qualification."""
    ranked = sorted(
        (r for r in rows if r.get("strike") is not None and net_charm(r, mode) is not None),
        key=lambda r: abs(net_charm(r, mode)),
        reverse=True,
    )
    if not ranked:
        return {
            "label": "Charm Peak",
            "strike": None,
            "signed_net_charm": None,
            "distance_pct_from_spot": None,
            "dominance_ratio": None,
            "is_near_spot": False,
            "is_dominant": False,
            "qualification_passed": False,
            "mode": mode,
        }
    top = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    top_charm = net_charm(top, mode)
    second_charm = net_charm(second, mode) if second else None

    distance_pct = abs(top["strike"] - spot) / spot if spot else None
    is_near = distance_pct is not None and distance_pct <= CHARM_PIN_MAX_DISTANCE_PCT
    dominance = None
    is_dominant = False
    if second_charm is not None and abs(second_charm) > 0:
        dominance = abs(top_charm) / abs(second_charm)
        is_dominant = dominance >= CHARM_PIN_DOMINANCE_RATIO

    passed = bool(is_near and is_dominant)
    return {
        "label": "Charm Pin" if passed else "Charm Peak",
        "strike": top["strike"],
        "signed_net_charm": top_charm,
        "distance_pct_from_spot": distance_pct,
        "dominance_ratio": dominance,
        "is_near_spot": is_near,
        "is_dominant": is_dominant,
        "qualification_passed": passed,
        "mode": mode,
    }


def vanna_peaks(rows: list[dict], mode: str = MODE_DIRECTIONALIZED_VOLUME) -> dict:
    """Call-side and put-side vanna concentration peaks (directionalized volume).

    Mirrors the portal's Vanna tab: the call peak is ``argmax(call_vanna)`` and
    the put peak is ``argmax(put_vanna)`` — two separate lobes of the vanna
    profile, not a single ``max|net_vanna|``.
    """
    calls = [r for r in rows if r.get("strike") is not None and call_vanna(r, mode) is not None]
    puts = [r for r in rows if r.get("strike") is not None and put_vanna(r, mode) is not None]
    call_peak = max(calls, key=lambda r: call_vanna(r, mode)) if calls else None
    put_peak = max(puts, key=lambda r: put_vanna(r, mode)) if puts else None
    return {
        "call_peak": {"strike": call_peak["strike"], "signed_call_vanna": call_vanna(call_peak, mode)} if call_peak else None,
        "put_peak": {"strike": put_peak["strike"], "signed_put_vanna": put_vanna(put_peak, mode)} if put_peak else None,
        "mode": mode,
    }
