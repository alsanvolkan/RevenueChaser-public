"""CFTC Commitments of Traders: Traders in Financial Futures and Disaggregated, from CFTC's Socrata API (keyless).

Futures only: the TFF dataset (``udgc-27he``) holds a ``FutOnly`` and a ``Combined`` row per contract and date, and
the query keeps ``FutOnly`` (Combined is a later choice); the Disaggregated dataset (``72hh-3qpy``) is futures-only.
Rows are keyed on the **contract code** (``cftc_contract_market_code``), never the market name: CFTC renamed the
markets on 8 Feb 2022 and the code stayed. Series id ``cftc:<report>:<contract_code>:<field>`` for every position and
open-interest field of the row (``*positions*`` and ``open_interest*``, not the percentages or the changes).

One raw file per report date, ``raw/cftc/cot/<report-date>.json.gz``, holding both reports' rows. The daily run asks
only for report dates after the newest file there; the backfill (from 13 Jun 2006) asks one year per report and
writes ``<year>.done`` once a past year's files are all written, which is its resume marker.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

from ....core.config import to_float
from .. import base

SOURCE, DATASET_DIR = "cftc", "cot"
TFF_BASE = os.getenv("RC_CFTC_BASE") or "https://publicreporting.cftc.gov/resource/udgc-27he.json"
DISAGG_BASE = os.getenv("RC_CFTC_DISAGG_BASE") or TFF_BASE.rsplit("/", 1)[0] + "/72hh-3qpy.json"
FIRST_REPORT = date(2006, 6, 13)
SOCRATA_LIMIT = 50_000
DATE_FIELD = "report_date_as_yyyy_mm_dd"
CODE_FIELD = "cftc_contract_market_code"
KIND_FIELD, KIND = "futonly_or_combined", "FutOnly"
# The contracts kept, by report (avo to tune): equity index, rates and VIX futures in TFF; crude and gold in
# Disaggregated.
CONTRACTS = {
    "tff": ("13874A",   # E-mini S&P 500
            "209742",   # Nasdaq-100 E-mini
            "1170E1",   # VIX futures
            "042601",   # 2-year T-note
            "043602",   # 10-year T-note
            "020601"),  # US Treasury bond
    "disagg": ("067651",  # WTI crude oil (NYMEX)
               "088691"),  # Gold (COMEX)
}


def _base(report: str) -> str:
    return TFF_BASE if report == "tff" else DISAGG_BASE


def _folder() -> Path:
    return base.raw_root() / SOURCE / DATASET_DIR


def _query(ctx, report: str, start: date, end: date) -> list[dict]:
    codes = ", ".join(f"'{c}'" for c in CONTRACTS[report])
    where = (f"{CODE_FIELD} in ({codes}) AND {DATE_FIELD} between "
             f"'{start.isoformat()}T00:00:00' and '{end.isoformat()}T23:59:59'")
    if report == "tff":
        # udgc-27he ("TFF_All") holds a FutOnly and a Combined row per contract and date; the futures-only one is
        # kept (Combined is a later choice for avo). 72hh-3qpy is futures-only already.
        where += f" AND {KIND_FIELD}='{KIND}'"
    params = {"$where": where,
              "$order": f"{DATE_FIELD}, {CODE_FIELD}", "$limit": str(SOCRATA_LIMIT)}
    body = base.http_get(ctx, SOURCE, _base(report), params).json()
    if not isinstance(body, list):
        raise base.SourceError(f"cftc {report}: not a list")
    if len(body) >= SOCRATA_LIMIT:
        ctx.issue("possibly_truncated", f"cftc:{report}:{start.year}", f"{len(body)} rows = the page limit")
    return body


def _write_by_report_date(ctx, rows: dict[str, list[dict]], as_of: date) -> list[Path]:
    from ..market_series import save_raw
    by_day: dict[str, dict[str, list]] = {}
    for report, items in rows.items():
        for item in items:
            day = str(item.get(DATE_FIELD, ""))[:10]
            if len(day) == 10:
                by_day.setdefault(day, {"tff": [], "disagg": []})[report].append(item)
    out = []
    for day in sorted(by_day):
        out.append(save_raw(ctx, _folder() / f"{day}.json.gz", json.dumps(by_day[day], sort_keys=True).encode(),
                            as_of, TFF_BASE, urls=[TFF_BASE, DISAGG_BASE]))
    return out


def _known_dates() -> list[str]:
    return sorted(p.name[:10] for p in _folder().glob("????-??-??.json.gz"))


def fetch(ctx, as_of: date) -> list[Path]:
    known = _known_dates()
    # The newest Tuesday whose Friday release has passed; nothing to ask while we already hold it.
    target = as_of - timedelta(days=(as_of.weekday() - 1) % 7)
    if (as_of - target).days < 3:
        target -= timedelta(days=7)
    if known and known[-1] >= target.isoformat():
        return []
    start = date.fromisoformat(known[-1]) + timedelta(days=1) if known else target
    rows = {r: _query(ctx, r, start, as_of) for r in CONTRACTS}
    return _write_by_report_date(ctx, rows, as_of)


def backfill(ctx, start: date, end: date) -> list[Path]:
    out = []
    for year in range(max(start, FIRST_REPORT).year, end.year + 1):
        done = _folder() / f"{year}.done"
        if done.exists():
            continue
        lo, hi = max(date(year, 1, 1), start, FIRST_REPORT), min(date(year, 12, 31), end)
        out += _write_by_report_date(ctx, {r: _query(ctx, r, lo, hi) for r in CONTRACTS}, end)
        if year < end.year:
            base.write_raw(done, b"")
    return out + fetch(ctx, end)


def _kept_field(name: str) -> bool:
    return ("positions" in name or name.startswith("open_interest")) and not name.startswith(("pct_", "change_"))


def parse(text: str, meta: dict) -> list[dict]:
    rows = []
    for report, items in json.loads(text).items():
        for item in items:
            code = str(item.get(CODE_FIELD) or "").strip()
            try:
                day = date.fromisoformat(str(item.get(DATE_FIELD))[:10])
            except ValueError:
                continue
            if not code or item.get(KIND_FIELD, KIND) != KIND:     # a Combined row the query let through
                continue
            for name, cell in item.items():
                value = to_float(cell) if _kept_field(name) else None
                if value is not None:
                    rows.append({"series_id": f"cftc:{report}:{code}:{name}", "obs_date": day, "value": value,
                                 "frequency": "weekly"})
    return rows
