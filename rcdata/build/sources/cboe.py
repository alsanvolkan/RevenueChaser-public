"""Cboe index history files (keyless CSV, the whole history in one file per symbol), fetched once per as-of date.

``SPX`` from 1975, ``VIX`` 1990, ``VVIX`` 2006, ``VIX9D`` 2011 and the rest from each file's start: a daily fetch is
already the backfill. ``VIX``, ``VIX9D``, ``VIX3M``, ``VIX6M`` and ``VIX1Y`` have OPEN/HIGH/LOW/CLOSE, which give
those and ``value`` = CLOSE; ``SPX``, ``VVIX`` and ``OVX`` are one-column files (``DATE,SPX`` and so on), which give
``value`` only (checked against the live files, 27 Oct 2026). Series id = the symbol. ``RC_CBOE_BASE`` is the same seam
``module_earnings_radar/inputs/vol_index.py`` reads, with the same default.
"""
from __future__ import annotations

import csv
import io
import os
from datetime import date, datetime
from pathlib import Path

from .. import base

SOURCE, DATASET_DIR = "cboe", "index"
BASE = os.getenv("RC_CBOE_BASE") or "https://cdn-api.cboe.com/api/global/us_indices/daily_prices"
SYMBOLS = ("SPX", "VIX", "VIX9D", "VIX3M", "VIX6M", "VIX1Y", "VVIX", "OVX")


def fetch(ctx, as_of: date) -> list[Path]:
    from ..market_series import save_raw
    out, failed = [], 0
    for sym in SYMBOLS:
        path = base.raw_root() / SOURCE / DATASET_DIR / as_of.isoformat() / f"{sym}_History.csv"
        if not path.exists():
            url = f"{BASE}/{sym}_History.csv"
            try:
                r = base.http_get(ctx, SOURCE, url)
            except base.CapReached:
                raise
            except base.SourceError as e:
                ctx.issue("fetch_failed", f"cboe:{sym}", str(e)[:300])
                failed += 1
                continue
            save_raw(ctx, path, r.content, as_of, url, symbol=sym)
        out.append(path)
    if failed and not out:
        raise base.SourceError(f"every cboe file failed ({failed})")
    return out


def backfill(ctx, start: date, end: date) -> list[Path]:
    return fetch(ctx, end)


def _num(cell) -> float | None:
    try:
        return float(str(cell).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse(text: str, meta: dict) -> list[dict]:
    sym = meta["symbol"]
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    fields = {f.strip().upper(): f for f in (reader.fieldnames or [])}
    single = next((f for k, f in fields.items() if k not in ("DATE", "OPEN", "HIGH", "LOW", "CLOSE")), None)
    rows = []
    for rec in reader:
        try:
            obs = datetime.strptime((rec.get(fields.get("DATE", "DATE")) or "").strip(), "%m/%d/%Y").date()
        except ValueError:
            continue
        if "CLOSE" in fields:
            o, h, lo, c = (_num(rec.get(fields[k])) if k in fields else None for k in ("OPEN", "HIGH", "LOW", "CLOSE"))
            value = c
        else:
            o = h = lo = c = None
            value = _num(rec.get(single)) if single else None
        if value is None:
            continue
        rows.append({"series_id": sym, "obs_date": obs, "value": value, "open": o, "high": h, "low": lo,
                     "close": c, "frequency": "daily"})
    return rows
