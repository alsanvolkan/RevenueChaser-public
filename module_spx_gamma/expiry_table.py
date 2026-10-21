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
