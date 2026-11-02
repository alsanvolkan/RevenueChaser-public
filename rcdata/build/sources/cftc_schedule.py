"""CFTC's Commitments of Traders release schedule (keyless HTML, fetched once a week), for #227.

The page lists the current year's release dates by month, a ``*`` marking a release a federal holiday moved. Each
release carries the report of the last Tuesday before it. Series ``CFTC_COT_RELEASE_LAG``: one row per release on
that Tuesday, valued in calendar days from the report to its release (3 for an ordinary Friday, 6 for a Monday). Keyed
on the report, so when CFTC moves a release, the next weekly fetch overwrites that key; a key on the release day would
leave the old day served. The page holds one year only: earlier years were never fetched, and their reports have no
row (``store_reads.cot_tff`` falls back to its Tuesday + 3 days estimate, the week's Friday).
"""
from __future__ import annotations

import html
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
# A heading's body stops at any heading close, a nested heading or a table, and the close is optional: an unclosed
# heading still dates its own table and never swallows the next heading (#367 S1).
_HEADING = re.compile(r"<h[1-6][^>]*>((?:(?!<h[1-6][\s>]|<table|</h[1-6]\s*>).)*)(?:</h[1-6]\s*>)?", re.S | re.I)
_YEAR = re.compile(r"\b(\d{4})\s+Release\s+Schedule\b", re.I)
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


def _heading_year(inner: str) -> int | None:
    """The year of a "<year> Release Schedule" heading, read from its text (tags and &nbsp; stripped, #359), so a
    word or a space before the year still counts and a sentence outside a heading never does."""
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", inner)))     # \s covers the no-break space
    m = _YEAR.search(text)
    return int(m.group(1)) if m else None


def releases(page: str) -> list[date]:
    """The release dates on the page, in page order. Each "<year> Release Schedule" heading dates the month rows up
    to the next heading of any kind (December can show two years); rows before the first heading, or under a heading
    without a year, are not read."""
    out = []
    headings = list(_HEADING.finditer(page))
    for i, h in enumerate(headings):
        # Every heading ends the section before it; only a heading with a year dates the rows under it (#367 S2).
        year = _heading_year(h.group(1))
        if year is None:
            continue
        section = page[h.end(): headings[i + 1].start() if i + 1 < len(headings) else len(page)]
        for row in _ROW.findall(section):
            cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)).strip() for c in _CELL.findall(row)]
            month = _MONTHS.get(cells[0].lower()) if cells else None
            if month:
                out += [date(year, month, int(d)) for c in cells[1:] for d in re.findall(r"^(\d{1,2})", c)]
    return out


def parse(text: str, meta: dict) -> list[dict]:
    rows = []
    for released in releases(text):
        report = released - timedelta(days=(released.weekday() - 1) % 7 or 7)    # the last Tuesday before it
        rows.append({"series_id": SERIES, "obs_date": report, "value": float((released - report).days),
                     "frequency": "weekly"})
    return rows
