"""FRED series through the FRED API (``FRED_API_KEY``, read by name through ``core/credentials.py``).

One JSON answer per series and as-of date (``raw/fred/series/<as-of>/<SERIES>.json``), each the whole series from
its start (the high-yield and investment-grade OAS series serve only from 27 Oct 2023), so a daily fetch is also the
backfill. Paced under 120 calls a minute (FRED's limit). Without a key the source is skipped with a build issue.
The key travels in the query string, which is never logged and never stored in the raw metadata.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from ....core import credentials
from .. import base

SOURCE, DATASET_DIR = "fred", "series"
BASE = os.getenv("RC_FRED_BASE") or "https://api.stlouisfed.org/fred/series/observations"
PER_MINUTE = 110
SERIES = {
    "DGS2": "daily", "DGS5": "daily", "DGS10": "daily", "DGS20": "daily", "DGS30": "daily",
    "T10Y2Y": "daily", "T10YIE": "daily", "DCOILWTICO": "daily", "BAMLH0A0HYM2": "daily", "BAMLC0A0CM": "daily",
    "MORTGAGE30US": "weekly", "WALCL": "weekly", "RRPONTSYD": "daily",
}


def fetch(ctx, as_of: date) -> list[Path]:
    from ..market_series import save_raw
    key = credentials.key_for("fred")
    if not key:
        ctx.issue("missing_key", "fred", credentials.missing_message("fred"))
        return []
    pacer = base.Pacer(PER_MINUTE, clock=ctx.clock, sleep=ctx.sleep)
    out, failed = [], 0
    for series in SERIES:
        path = base.raw_root() / SOURCE / DATASET_DIR / as_of.isoformat() / f"{series}.json"
        if not path.exists():
            pacer.wait()
            try:
                r = base.http_get(ctx, SOURCE, BASE, {"series_id": series, "api_key": key, "file_type": "json"})
            except base.SourceError as e:
                ctx.issue("fetch_failed", f"fred:{series}", str(e)[:300])
                failed += 1
                continue
            save_raw(ctx, path, r.content, as_of, BASE, series=series)
        out.append(path)
    if failed and not out:
        raise base.SourceError(f"every fred file failed ({failed})")
    return out


def backfill(ctx, start: date, end: date) -> list[Path]:
    return fetch(ctx, end)


def parse(text: str, meta: dict) -> list[dict]:
    series = meta["series"]
    rows = []
    for obs in json.loads(text).get("observations", []):
        try:
            value = float(obs.get("value"))
            day = date.fromisoformat(obs.get("date"))
        except (TypeError, ValueError):
            continue                     # FRED writes "." for a missing value
        rows.append({"series_id": series, "obs_date": day, "value": value,
                     "frequency": SERIES.get(series, "daily")})
    return rows
