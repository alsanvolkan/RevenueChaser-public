"""The SEC daily index: which company filed which form on a day, the trigger of every SEC re-read (#43 C14).

``GET <RC_SEC_WWW_BASE>/Archives/edgar/daily-index/<year>/QTR<n>/master.<YYYYMMDD>.idx``: a few header lines, then
``CIK|Company Name|Form Type|Date Filed|Filename`` per filing, the accession in the file name
(``edgar/data/789019/0000950170-26-000123.txt``). The SEC publishes a day's index that night; a weekend or SEC holiday
has none (404).

Raw: ``raw/sec/daily_index/<date>.idx.gz`` with a ``.meta.json`` sidecar, shared by every dataset that reads it (the
first to need a day fetches it; a raw file that exists is not fetched again). A 404 for a day before yesterday writes
``<date>.absent.json`` (no index will come: a holiday), so it is not asked again; a 404 for yesterday or today writes
nothing (the index may not be published yet). Weekends are never asked.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from pathlib import Path

from .. import base
from . import common

DATASET_DIR = "sec/daily_index"
LOOKBACK_DAYS = 7
_FILE = re.compile(r"(\d{10}-\d{2}-\d{6})\.txt$")


def url(day: date) -> str:
    q = (day.month - 1) // 3 + 1
    return f"{common.sec_www()}/Archives/edgar/daily-index/{day.year}/QTR{q}/master.{day:%Y%m%d}.idx"


def raw_path(day: date) -> Path:
    return common.raw_dir(DATASET_DIR) / f"{day.isoformat()}.idx.gz"


def absent_path(day: date) -> Path:
    return common.raw_dir(DATASET_DIR) / f"{day.isoformat()}.absent.json"


def days(as_of: date, lookback: int = LOOKBACK_DAYS) -> list[date]:
    """The weekdays from ``as_of - lookback`` to ``as_of``, oldest first."""
    out = [as_of - timedelta(days=n) for n in range(lookback, -1, -1)]
    return [d for d in out if d.weekday() < 5]


def fetch_day(ctx: base.RunContext, day: date, as_of: date) -> Path | None:
    """The raw index of ``day`` (fetched now if it is not stored); None when there is none (yet)."""
    raw = raw_path(day)
    if raw.exists():
        return raw
    if absent_path(day).exists():
        return None
    r = common.sec_get(ctx, url(day), missing=(404,))
    if r is None:
        if day < as_of - timedelta(days=1):
            base.write_raw_json(absent_path(day), {"day": day.isoformat(), "fetched_at": time.time(),
                                                   "run_id": ctx.run_id, "url": url(day)})
        return None
    rows = parse(r.text)
    if not rows and "Form Type" not in r.text:
        raise base.SourceError(f"sec daily index {day}: not the documented shape")
    base.write_raw_json(common.meta_path(raw), {"day": day.isoformat(), "fetched_at": time.time(),
                                                "run_id": ctx.run_id, "url": url(day), "rows": len(rows)})
    return base.write_raw(raw, r.content)


def parse(text: str) -> list[dict]:
    """``[{cik, company, form, filed, accession}]`` of one index file (the header lines are skipped)."""
    out = []
    for line in (text or "").splitlines():
        parts = line.split("|")
        if len(parts) != 5 or not parts[0].strip().isdigit():
            continue
        m = _FILE.search(parts[4].strip())
        if not m:
            continue
        filed = parts[3].strip()
        if len(filed) == 8 and filed.isdigit():
            filed = f"{filed[:4]}-{filed[4:6]}-{filed[6:]}"
        out.append({"cik": str(int(parts[0])), "company": parts[1].strip(), "form": parts[2].strip(),
                    "filed": filed, "accession": m.group(1)})
    return out


def filings(ctx: base.RunContext, as_of: date, forms: set[str] | None = None,
            ciks: set[str] | None = None, lookback: int = LOOKBACK_DAYS) -> list[dict]:
    """The filings of the last ``lookback`` days (indexes fetched as needed), kept to ``forms`` and ``ciks``."""
    out = []
    for day in days(as_of, lookback):
        raw = fetch_day(ctx, day, as_of)
        if raw is None:
            continue
        for row in parse(base.read_raw(raw).decode("latin-1")):
            if (forms is None or row["form"] in forms) and (ciks is None or row["cik"] in ciks):
                out.append(row)
    return out
