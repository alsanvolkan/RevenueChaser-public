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


def session_hedge_flow(expiry_rows: list[dict], asof_date: str | None, spot: float | None) -> list[dict]:
    """A6: each session's expiring book from the board's day through the target Friday.

    Net gamma, the hedge for a 50-point move (|Net GEX per 1 %| x 50 / spot x 100 %, the reports' formula), net charm
    and net vanna, and the effect in words. Expiring open interest needs OI per expiry (phase 2).
    """
    if not asof_date:
        return []
    from .brier import target_friday
    from .levels import hedge_for_move
    end = target_friday(asof_date)
    out = []
    for r in expiry_rows or []:
        e = r.get("expiry")
        if not e or not (asof_date <= str(e) <= end):
            continue
        g, ch = r.get("net_gex"), r.get("net_charm")
        parts = []
        if g:
            parts.append("dampens moves (positive gamma)" if g > 0 else "amplifies moves (negative gamma)")
        if ch:
            parts.append("dealers' charm buys into the close (positive charm)" if ch > 0
                         else "dealers' charm sells into the close (negative charm)")
        out.append({"expiry": e, "net_gex": g, "hedge_50pt": hedge_for_move(g, spot, 50.0), "net_charm": ch,
                    "net_vanna": r.get("net_vanna"), "effect": "; ".join(parts) or "flat"})
    return out


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
