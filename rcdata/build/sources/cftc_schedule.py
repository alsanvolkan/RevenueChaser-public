"""CFTC's Commitments of Traders release schedule (keyless HTML, fetched once a week), for #227.

The page lists the current year's release dates by month, a ``*`` marking a release a federal holiday moved. Each
release carries the report of the last Tuesday before it. Series ``CFTC_COT_RELEASE_LAG``: one row per release on
that Tuesday, valued in calendar days from the report to its release (3 for an ordinary Friday, 6 for a Monday). Keyed
on the report, so when CFTC moves a release, the next weekly fetch overwrites that key; a key on the release day would
leave the old day served. The page holds one year only: earlier years were never fetched, and their reports have no
row (``store_reads.cot_tff`` falls back to its Tuesday + 3 days estimate, the week's Friday).
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta
from pathlib import Path

from .. import base

SOURCE, DATASET_DIR = "cftc_schedule", "release"
PROVIDER = "cftc"                       # counted and capped under CFTC, like its COT data
SERIES = "CFTC_COT_RELEASE_LAG"
BASE = os.getenv("RC_CFTC_SCHEDULE_BASE") or "https://www.cftc.gov"
PATH = "/MarketReports/CommitmentsofTraders/ReleaseSchedule/index.htm"
_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"), 1)}
_YEAR = re.compile(r"<h\d[^>]*>(?:\s*<[^>]+>)*\s*(\d{4})\s+Release\s+Schedule", re.I)   # a heading only
_ROW = re.compile(r"<tr.*?</tr>", re.S | re.I)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)


def fetch(ctx, as_of: date) -> list[Path]:
    from ..market_series import save_raw
    week = as_of - timedelta(days=as_of.weekday())      # one file per Monday-to-Sunday week: weekly, not daily
    path = base.raw_root() / SOURCE / DATASET_DIR / f"{week.isoformat()}.htm"
    if not path.exists():
        url = BASE + PATH
        save_raw(ctx, path, base.http_get(ctx, PROVIDER, url).content, as_of, url)
    return [path]


def backfill(ctx, start: date, end: date) -> list[Path]:
    return fetch(ctx, end)


def releases(html: str) -> list[date]:
    """The release dates on the page, in page order. Each "<year> Release Schedule" heading dates the month rows up
    to the next heading (December can show two years); rows before the first heading are not read."""
    out = []
    marks = list(_YEAR.finditer(html))
    for i, m in enumerate(marks):
        section = html[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(html)]
        for row in _ROW.findall(section):
            cells = [re.sub(r"<[^>]+>|&nbsp;", " ", c).strip() for c in _CELL.findall(row)]
            month = _MONTHS.get(cells[0].lower()) if cells else None
            if month:
                out += [date(int(m.group(1)), month, int(d)) for c in cells[1:] for d in re.findall(r"^(\d{1,2})", c)]
    return out


def parse(text: str, meta: dict) -> list[dict]:
    rows = []
    for released in releases(text):
        report = released - timedelta(days=(released.weekday() - 1) % 7 or 7)    # the last Tuesday before it
        rows.append({"series_id": SERIES, "obs_date": report, "value": float((released - report).days),
                     "frequency": "weekly"})
    return rows
