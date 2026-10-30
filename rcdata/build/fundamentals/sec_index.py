"""The SEC daily index: which company filed which form on a day, the trigger of every SEC re-read (#43 C14).

``GET <RC_SEC_WWW_BASE>/Archives/edgar/daily-index/<year>/QTR<n>/master.<YYYYMMDD>.idx``: a few header lines, then
``CIK|Company Name|Form Type|Date Filed|Filename`` per filing, the accession in the file name
(``edgar/data/789019/0000950170-26-000123.txt``). The SEC publishes a day's index that night; a weekend or SEC holiday
has none (403: ``base.is_missing``).

Raw: ``raw/sec/daily_index/<date>.idx.gz`` with a ``.meta.json`` sidecar, shared by every dataset that reads it (the
first to need a day fetches it; a raw file that exists is not fetched again). A day before yesterday the archive holds
no index for writes ``<date>.absent.json`` (no index will come), so it is not asked again, but **only when a federal
holiday explains it**: the same answer comes from a path or bucket refusing everything, and a stretch of
``base.MISSING_RUN`` open weekdays with no index stops the run with nothing written off (``base.MissingStreak``).
The answer for yesterday or today writes nothing (the index may not be published yet). Weekends are never asked.

**The scan window.** A run reads the last ``LOOKBACK_DAYS`` days, or further back when it has to: a dataset that
reads the index writes ``raw/<dataset>/_scanned.json`` (the last day a run of it read whole) at the end of each run,
and the next run starts the day after it (``lookback``). Without that file (a dataset only backfilled so far), the
start is the day after the oldest newest read the caller names. So a builder that was down for three weeks still
reads the filings of those three weeks (the Opus review's S1), up to ``MAX_LOOKBACK_DAYS`` back; a filing older than
that is not read, and the company's statements show as stale in the check.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from .. import base
from . import common

DATASET_DIR = "sec/daily_index"
LOOKBACK_DAYS = 7
MAX_LOOKBACK_DAYS = 120
SCANNED = "_scanned.json"
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


def lookback(ctx: base.RunContext, dataset: str, as_of: date, since: Callable[[], date | None]) -> int:
    """How many days before ``as_of`` a run of ``dataset`` reads: back to the day after its last whole scan
    (``_scanned.json``; ``since()`` when it has none), never fewer than ``LOOKBACK_DAYS`` nor more than
    ``MAX_LOOKBACK_DAYS``. A widened or capped window is said in the run's note."""
    path = common.raw_dir(dataset) / SCANNED
    last = date.fromisoformat(base.read_raw_json(path)["through"]) if path.exists() else since()
    gap = (as_of - last).days - 1 if last else 0
    if gap <= LOOKBACK_DAYS:
        return LOOKBACK_DAYS
    if gap > MAX_LOOKBACK_DAYS:
        ctx.notes.append(f"daily index read for {MAX_LOOKBACK_DAYS} days only (last whole scan {last}): a filing "
                         f"before {as_of - timedelta(days=MAX_LOOKBACK_DAYS)} is not read")
        return MAX_LOOKBACK_DAYS
    ctx.notes.append(f"daily index read for {gap} days (last whole scan {last})")
    return gap


def mark_scanned(ctx: base.RunContext, dataset: str, through: date) -> None:
    """Record that a run of ``dataset`` read every filing through ``through`` (its next run starts the day after)."""
    base.write_raw_json(common.raw_dir(dataset) / SCANNED, {"through": through.isoformat(), "run_id": ctx.run_id,
                                                            "written_at": time.time()})


def fetch_day(ctx: base.RunContext, day: date, as_of: date,
              streak: base.MissingStreak | None = None) -> Path | None:
    """The raw index of ``day`` (fetched now if it is not stored); None when there is none (yet).

    ``streak`` counts the open weekdays this walk found no index for and raises on a stretch of them
    (``base.MissingStreak``); a lone call without one writes a day off only when a holiday explains it.
    """
    raw = raw_path(day)
    if raw.exists():
        return raw
    if absent_path(day).exists():
        return None
    recent = day >= as_of - timedelta(days=1)
    # An index the archive does not hold answers 403 (``base.is_missing``), whether it is a day EDGAR keeps closed
    # or a day not published yet; which of the two it is, is the day's age. A 403 that is not that answer is the
    # SEC refusing, and fails the run however old the day is.
    r = common.sec_get(ctx, url(day), missing_object=True)
    if r is None:
        # The same answer comes from a path or bucket that refuses everything, so a day is written off only when a
        # holiday explains it; a stretch of open weekdays raises and nothing is written off.
        if not recent and (streak or base.MissingStreak("daily index")).missing(ctx, day):
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
    streak = base.MissingStreak("daily index")
    for day in days(as_of, lookback):
        raw = fetch_day(ctx, day, as_of, streak)
        if raw is None:
            continue
        for row in parse(base.read_raw(raw).decode("latin-1")):
            if (forms is None or row["form"] in forms) and (ciks is None or row["cik"] in ciks):
                out.append(row)
    return out
