"""Expiry rows: normalize the /greek-exposure/expiry payload (0DTE validation)."""
from __future__ import annotations

from .config import net_sum, to_float


def normalize_expiry_rows(payload: dict | None) -> list[dict]:
    if not payload or "data" not in payload:
        return []
    rows = []
    for r in payload["data"]:
        dte = to_float(r.get("dte"))
        rows.append({
            "expiry": r.get("expiry") or r.get("date"),
            "dte": dte,
            "band": dte_band(dte),
            "net_gex": net_sum(r.get("call_gex"), r.get("put_gex")),
            "net_dex": net_sum(r.get("call_delta"), r.get("put_delta")),
            "net_vanna": net_sum(r.get("call_vanna"), r.get("put_vanna")),
            "net_charm": net_sum(r.get("call_charm"), r.get("put_charm")),
        })
    rows.sort(key=lambda x: x["expiry"] or "")
    return rows


def front_expiry_share(expiry_rows: list[dict], asof_date: str | None) -> dict | None:
    """The front expiry's share of the total Net GEX (30 Oct report p.9: 17.18 %), F7.

    The front expiry is the earliest on or after ``asof_date``; expired series count in neither part. None when
    there is no front row or the total is zero.
    """
    live = [r for r in expiry_rows or [] if r.get("expiry") and r.get("net_gex") is not None
            and (asof_date is None or str(r["expiry"]) >= asof_date)]
    if not live:
        return None
    front = min(live, key=lambda r: str(r["expiry"]))
    total = sum(r["net_gex"] for r in live)
    if total == 0:
        return None
    return {"expiry": front["expiry"], "front_net_gex": front["net_gex"], "total_net_gex": total,
            "share_pct": front["net_gex"] / total * 100.0}


def dte_band(dte: float | None) -> str:
    if dte is None:
        return ""
    if dte < 0:
        return "expired"
    if dte == 0:
        return "0DTE"
    if dte <= 7:
        return "1–7D"
    if dte <= 30:
        return "8–30D"
    if dte <= 90:
        return "31–90D"
    return "90D+"
