"""US Treasury daily par yield curve rates (keyless CSV, one file per year), series ``UST_PAR_<tenor>``.

Past years are fetched once (``raw/treasury/par_yield/<year>.csv``); the current year is fetched once per as-of
date (``raw/treasury/par_yield/<as-of>/<year>.csv``), because it grows every session. In the first days of January
the year just ended is fetched once more, whole. The backfill starts in 1990.
"""
from __future__ import annotations

import csv
import io
import os
import re
from datetime import date, datetime
from pathlib import Path

from .. import base

SOURCE, DATASET_DIR = "treasury", "par_yield"
BASE = os.getenv("RC_TREASURY_BASE") or \
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
FIRST_YEAR = 1990
_TENOR = re.compile(r"^\s*([\d.]+)\s*(mo|month|months|yr|year|years)\s*$", re.I)


def _url(year: int) -> tuple[str, dict]:
    return f"{BASE}/{year}/all", {"type": "daily_treasury_yield_curve", "field_tdr_date_value": str(year),
                                  "page": "", "_format": "csv"}


def _folder() -> Path:
    return base.raw_root() / SOURCE / DATASET_DIR


def _get_year(ctx, year: int, path: Path, as_of: date) -> Path:
    from ..market_series import save_raw
    if path.exists():
        return path
    url, params = _url(year)
    r = base.http_get(ctx, SOURCE, url, params)
    return save_raw(ctx, path, r.content, as_of, url)


def fetch(ctx, as_of: date) -> list[Path]:
    out = [_get_year(ctx, as_of.year, _folder() / as_of.isoformat() / f"{as_of.year}.csv", as_of)]
    if as_of.month == 1 and as_of.day <= 10:
        out.append(_get_year(ctx, as_of.year - 1, _folder() / f"{as_of.year - 1}.csv", as_of))
    return out


def backfill(ctx, start: date, end: date) -> list[Path]:
    out = [_get_year(ctx, y, _folder() / f"{y}.csv", end) for y in range(max(start.year, FIRST_YEAR), end.year)]
    return out + fetch(ctx, end)


def tenor(header: str) -> str | None:
    m = _TENOR.match(header)
    if not m:
        return None
    n = m.group(1).rstrip("0").rstrip(".") if "." in m.group(1) else m.group(1)
    return f"{n}{'M' if m.group(2).lower().startswith('mo') else 'Y'}"


def parse(text: str, meta: dict) -> list[dict]:
    rows = []
    for rec in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        raw_date = (rec.get("Date") or "").strip()
        try:
            obs = datetime.strptime(raw_date, "%m/%d/%Y").date()
        except ValueError:
            continue
        for header, cell in rec.items():
            t = tenor(header or "") if header != "Date" else None
            if t is None or cell in (None, "", "N/A"):
                continue
            try:
                value = float(cell)
            except ValueError:
                continue
            rows.append({"series_id": f"UST_PAR_{t}", "obs_date": obs, "value": value, "frequency": "daily"})
    return rows
