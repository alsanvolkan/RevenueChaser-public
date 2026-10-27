"""FOMC meeting dates from the Federal Reserve's calendar page (keyless HTML, fetched once per as-of date).

Series ``FOMC_MEETING`` (value 1 on each day of a meeting) and ``FOMC_DECISION`` (value 1 on its last day, when the
statement comes). A notation vote is not a meeting and is left out. The page covers the current and the last few
years; older meetings come from the historical pages, not fetched here.
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta
from pathlib import Path

from .. import base

SOURCE, DATASET_DIR = "fed", "fomc"
BASE = os.getenv("RC_FED_BASE") or "https://www.federalreserve.gov"
PATH = "/monetarypolicy/fomccalendars.htm"
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"), 1)}
_YEAR = re.compile(r"(\d{4})\s+FOMC\s+Meetings", re.I)
_MEETING = re.compile(r'fomc-meeting__month[^>]*>\s*(?:<[^>]+>\s*)*([A-Za-z/]+)\s*<.*?'
                      r'fomc-meeting__date[^>]*>\s*([^<]*)<', re.S)


def fetch(ctx, as_of: date) -> list[Path]:
    from ..market_series import save_raw
    path = base.raw_root() / SOURCE / DATASET_DIR / f"{as_of.isoformat()}.htm"
    if not path.exists():
        url = BASE + PATH
        save_raw(ctx, path, base.http_get(ctx, SOURCE, url).content, as_of, url)
    return [path]


def backfill(ctx, start: date, end: date) -> list[Path]:
    return fetch(ctx, end)


def meetings(html: str) -> list[list[date]]:
    """The days of each meeting on the page, in page order."""
    out = []
    marks = list(_YEAR.finditer(html))
    for i, m in enumerate(marks):
        year = int(m.group(1))
        segment = html[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(html)]
        for month_text, date_text in _MEETING.findall(segment):
            if "notation" in date_text.lower():
                continue
            months = [_MONTHS.get(p.strip()[:3].lower()) for p in month_text.split("/")]
            days = [int(d) for d in re.findall(r"\d+", date_text.split("(")[0])[:2]]
            if not days or None in months:
                continue
            first = date(year, months[0], days[0])
            if len(days) == 1:
                out.append([first])
                continue
            # "Apr/May 30-1": the second day is in the second month (and in the next year for "Dec/Jan").
            month = months[-1] if days[1] < days[0] else months[0]
            last = date(year + 1 if month < months[0] else year, month, days[1])
            out.append([first + timedelta(days=k) for k in range((last - first).days + 1)])
    return out


def parse(text: str, meta: dict) -> list[dict]:
    rows = []
    for days in meetings(text):
        for d in days:
            rows.append({"series_id": "FOMC_MEETING", "obs_date": d, "value": 1.0, "frequency": "event"})
        rows.append({"series_id": "FOMC_DECISION", "obs_date": days[-1], "value": 1.0, "frequency": "event"})
    return rows
