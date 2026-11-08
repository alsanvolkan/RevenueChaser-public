"""The week's volatility calendar (A8) from UW's /market/economic-calendar.

Events from the board's day through the Friday the week resolves on, high-impact releases marked, times in
Europe/Amsterdam (the owner's clock). UW repeats rows, so (event, time) duplicates are dropped.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .brier import target_friday

AMS = ZoneInfo("Europe/Amsterdam")
HIGH_IMPACT = ("cpi", "employment report", "nonfarm", "ppi", "fomc", "fed interest rate", "pce", "gdp",
               "retail sales")


def week_events(payload: dict | None, asof_date: str) -> list[dict]:
    rows = (payload or {}).get("data") or []
    end = target_friday(asof_date)
    seen, out = set(), []
    for r in rows:
        if not isinstance(r, dict) or not r.get("time") or not r.get("event"):
            continue
        try:
            t = datetime.fromisoformat(str(r["time"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        local = t.astimezone(AMS)
        day = t.date().isoformat()
        if not (asof_date <= day <= end):
            continue
        key = (r["event"], r["time"])
        if key in seen:
            continue
        seen.add(key)
        name = str(r["event"])
        out.append({"event": name, "type": r.get("type"), "time": r["time"],
                    "local": f"{local:%a} {local.day} {local:%b %H:%M}",
                    "prev": r.get("prev"), "forecast": r.get("forecast"),
                    "high_impact": any(k in name.lower() for k in HIGH_IMPACT)})
    out.sort(key=lambda r: r["time"])
    return out
