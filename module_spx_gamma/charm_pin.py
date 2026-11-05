"""Charm Peak/Pin qualification and Vanna Strike from spot-exposure data."""
from __future__ import annotations

from .config import CHARM_PIN_DOMINANCE_RATIO, CHARM_PIN_MAX_DISTANCE_PCT, net_sum, to_float

MODE_OPEN_INTEREST = "open_interest"
MODE_DIRECTIONALIZED_VOLUME = "directionalized_volume"

# F6: the 02-06 Nov report's warning, shown wherever a summed charm sign is read as pressure.
CHARM_SIGN_CAVEAT = ("Sign caveat: this is UW's summed charm; its sign can differ from a Black-Scholes sum over the "
                     "same contracts (02-06 Nov report), so treat the direction as UW's, not a confirmed dealer hedge.")


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


def _expired_by(asof_date: str, fetched_at) -> bool:
    """Whether ``asof_date``'s own expiry had settled when the rows were fetched (16:00 New York that day).

    Without a fetch time (a raw day written before ``_meta`` carried one) nothing is known, and the series counts as
    before.
    """
    if fetched_at is None:
        return False
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    close = datetime.fromisoformat(asof_date).replace(hour=16, tzinfo=ny)
    return fetched_at.astimezone(ny) >= close


def report_charm_pin(strike_expiry_rows: list[dict], asof_date: str | None, fetched_at=None) -> dict:
    """The 30 Oct report's charm pin (p.6): the strike of the largest |net charm| in the front expiry.

    The front expiry is the nearest one still trading whose rows carry charm: after ``asof_date``, or ``asof_date``
    itself only when the rows were fetched before its 16:00 New York close (``fetched_at``). A Fetch of a closed
    session carries that day's settled 0DTE rows too; a pin read from them points at a series that no longer
    exists (real UW check, 5 Nov 2026: 7650 from the 4 Nov expiry instead of 7750 from 5 Nov). The report adds that a
    pin is not a confirmed attractor (2 Oct audit, p.8), so the card says so; the vol-mode ``qualify_charm`` stays
    as a second reading. ``strike`` is None when no row carries charm.
    """
    empty = {"strike": None, "net_charm": None, "expiry": None,
             "rule": "largest front-expiry net charm (30 Oct report)"}
    rows = [r for r in strike_expiry_rows or []
            if r.get("strike") is not None and r.get("net_charm") is not None and r.get("expiry")
            and (asof_date is None or str(r["expiry"]) > asof_date
                 or (str(r["expiry"]) == asof_date and not _expired_by(asof_date, fetched_at)))]
    if not rows:
        return empty
    front = min(str(r["expiry"]) for r in rows)
    top = max((r for r in rows if str(r["expiry"]) == front), key=lambda r: abs(r["net_charm"]))
    if top["net_charm"] == 0:
        return empty
    return {**empty, "strike": top["strike"], "net_charm": top["net_charm"], "expiry": front}


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
