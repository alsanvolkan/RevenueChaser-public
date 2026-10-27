"""CPI release dates from the BLS release calendar (keyless ``.ics``, fetched once per as-of date).

Series ``CPI_RELEASE``: value 1 on each day an event titled "Consumer Price Index" is scheduled.
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

from .. import base

SOURCE, DATASET_DIR = "bls", "calendar"
BASE = os.getenv("RC_BLS_BASE") or "https://www.bls.gov"
PATH = "/schedule/news_release/bls.ics"
_DTSTART = re.compile(r"^DTSTART[^:]*:(\d{8})", re.M)


def fetch(ctx, as_of: date) -> list[Path]:
    from ..market_series import save_raw
    path = base.raw_root() / SOURCE / DATASET_DIR / f"{as_of.isoformat()}.ics"
    if not path.exists():
        url = BASE + PATH
        save_raw(ctx, path, base.http_get(ctx, SOURCE, url).content, as_of, url)
    return [path]


def backfill(ctx, start: date, end: date) -> list[Path]:
    return fetch(ctx, end)


def parse(text: str, meta: dict) -> list[dict]:
    unfolded = re.sub(r"\r?\n[ \t]", "", text)        # RFC 5545 line folding
    rows, seen = [], set()
    for block in unfolded.split("BEGIN:VEVENT")[1:]:
        block = block.split("END:VEVENT")[0]
        summary = next((line.split(":", 1)[1] for line in block.splitlines() if line.startswith("SUMMARY")), "")
        m = _DTSTART.search(block)
        if not m or "consumer price index" not in summary.lower():
            continue
        s = m.group(1)
        day = date(int(s[:4]), int(s[4:6]), int(s[6:]))
        if day not in seen:
            seen.add(day)
            rows.append({"series_id": "CPI_RELEASE", "obs_date": day, "value": 1.0, "frequency": "event"})
    return rows
