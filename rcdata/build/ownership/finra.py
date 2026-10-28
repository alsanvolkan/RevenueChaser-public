"""``finra/short_interest``: FINRA's consolidated short interest, one settlement date at a time (keyless).

``GET <RC_FINRA_API_BASE>/partitions/group/otcMarket/name/consolidatedShortInterest`` lists the settlement dates
FINRA has published (``{"availablePartitions": [{"partitions": ["2026-10-15"]}, ...]}``; 212 of them back to
2017-12-29 when read on 2026-10-28). ``POST <base>/data/group/otcMarket/name/consolidatedShortInterest`` with
``{"limit": 5000, "offset": n, "compareFilters": [{"compareType": "EQUAL", "fieldName": "settlementDate",
"fieldValue": "<date>"}]}`` answers one page of that date's rows; its ``record-total`` header names the date's row
count (22,537 for 2026-10-15), so a date is about five pages. A date whose pages do not add up to that total is
refused, never kept short.

A daily run reads the list (one call) and fetches the published dates newer than the newest one stored, at most
``DAILY_MAX_DATES``; the first run fetches the newest date only, and a backfill every missing date of its range (from
the first published one when no ``--from`` is given).

Raw: ``raw/finra/short_interest/<settlement-date>.json.gz`` (FINRA's pages as answered, and the run's provenance);
it is the resume marker, so a date is fetched once. FINRA's later revisions of a date are not fetched again (a known
limit). Parquet: ``parquet/finra/short_interest/date=<settlement-date>/part-0.parquet``. Served:
``finra_short_interest``, one row per settlement date and symbol, the symbol named by the spine (``common``).
"""
from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from ..spine.stage import partitions
from . import KIND, common

NAME = "finra/short_interest"
PROVIDER = "finra"
TABLE = "finra_short_interest"
BASE_ENV = "RC_FINRA_API_BASE"
DEFAULT_BASE = "https://api.finra.org"
PARTITIONS = "/partitions/group/otcMarket/name/consolidatedShortInterest"
DATA = "/data/group/otcMarket/name/consolidatedShortInterest"
PAGE = 5000
MAX_PAGES = 20
DAILY_MAX_DATES = 6
HEADERS = {"Accept": "application/json"}
SCHEMA = [("settlement_date", "DATE"), ("symbol", "TEXT"), ("issue_name", "TEXT"), ("market_class", "TEXT"),
          ("current_short", "BIGINT"), ("previous_short", "BIGINT"), ("change_shares", "BIGINT"),
          ("change_pct", "DOUBLE"), ("avg_daily_volume", "BIGINT"), ("days_to_cover", "DOUBLE"),
          ("split_flag", "TEXT"), ("revision_flag", "TEXT"), ("source_symbol", "TEXT"),
          ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
SERVED = SCHEMA[:2] + [("ticker", "TEXT"), ("ticker_source", "TEXT")] + SCHEMA[2:] + [("source", "TEXT")]
# FINRA's field names, by the column each one fills.
FIELDS = {"issue_name": "issueName", "market_class": "marketClassCode",
          "current_short": "currentShortPositionQuantity", "previous_short": "previousShortPositionQuantity",
          "change_shares": "changePreviousNumber", "change_pct": "changePercent",
          "avg_daily_volume": "averageDailyVolumeQuantity", "days_to_cover": "daysToCoverQuantity",
          "split_flag": "stockSplitFlag", "revision_flag": "revisionFlag"}


def base_url() -> str:
    return (os.getenv(BASE_ENV) or DEFAULT_BASE).rstrip("/")


def raw_dir() -> Path:
    return base.raw_root() / "finra" / "short_interest"


def parquet_dir() -> Path:
    return base.parquet_root() / "finra" / "short_interest"


def raw_path(day: date) -> Path:
    return raw_dir() / f"{day.isoformat()}.json.gz"


def stored_dates() -> list[date]:
    if not raw_dir().is_dir():
        return []
    return sorted(date.fromisoformat(p.name[:10]) for p in raw_dir().glob("*.json.gz"))


def published(ctx: base.RunContext) -> list[date]:
    """The settlement dates FINRA lists, oldest first; ``SourceError`` for an empty or malformed list."""
    r = base.http_get(ctx, PROVIDER, base_url() + PARTITIONS, headers=HEADERS)
    try:
        groups = r.json()["availablePartitions"]
        days = {date.fromisoformat(str(p)[:10]) for g in groups for p in g["partitions"]}
    except (ValueError, KeyError, TypeError) as e:
        raise base.SourceError(f"finra {PARTITIONS}: not the documented shape ({type(e).__name__}: {e})") from e
    if not days:
        raise base.SourceError(f"finra {PARTITIONS}: no settlement date listed")
    return sorted(days)


def fetch_date(ctx: base.RunContext, day: date) -> Path:
    """Every page of one settlement date, written as one raw file once they add up to FINRA's ``record-total``."""
    pages, rows, total = [], 0, None
    for n in range(MAX_PAGES):
        body = {"limit": PAGE, "offset": n * PAGE, "compareFilters": [
            {"compareType": "EQUAL", "fieldName": "settlementDate", "fieldValue": day.isoformat()}]}
        r = base.http_post(ctx, PROVIDER, base_url() + DATA, json=body, headers=HEADERS)
        try:
            page = r.json() if r.content.strip() else []
        except ValueError as e:
            raise base.SourceError(f"finra {day}: page {n} is not JSON") from e
        if not isinstance(page, list):
            raise base.SourceError(f"finra {day}: page {n} is not a list of rows")
        header = str(r.headers.get("record-total") or "").strip()
        total = int(header) if header.isdigit() else total
        pages.append(page)
        rows += len(page)
        if len(page) < PAGE or (total is not None and rows >= total):
            break
    else:
        raise base.SourceError(f"finra {day}: more than {MAX_PAGES} pages")
    if not rows:
        raise base.SourceError(f"finra {day}: a listed settlement date answered no rows")
    if total is not None and rows != total:
        raise base.SourceError(f"finra {day}: {rows} rows answered, record-total says {total}")
    return base.write_raw_json(raw_path(day), {
        "settlement_date": day.isoformat(), "record_total": total, "pages": pages, "fetched_at": time.time(),
        "run_id": ctx.run_id, "url": base_url() + DATA})


def _int(v):
    try:
        return None if v in (None, "") else int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def parse(body: dict) -> tuple[list[dict], int]:
    """A raw file's rows typed, and how many repeated a symbol of the date (the first one is kept)."""
    day = date.fromisoformat(body["settlement_date"])
    out: dict[str, dict] = {}
    repeats = 0
    for page in body["pages"]:
        for rec in page:
            symbol = common.norm_symbol(rec.get("symbolCode"))
            if not symbol:
                continue
            if symbol in out:
                repeats += 1
                continue
            row = {"settlement_date": day, "symbol": symbol, "source_symbol": rec.get("symbolCode"),
                   "fetched_at": body["fetched_at"], "run_id": body["run_id"]}
            for col, field in FIELDS.items():
                v = rec.get(field)
                row[col] = (_float(v) if col in ("change_pct", "days_to_cover") else
                            _int(v) if dict(SCHEMA)[col] == "BIGINT" else (None if v in (None, "") else str(v)))
            out[symbol] = row
    return list(out.values()), repeats


class FinraShortInterest:
    name = NAME
    kind = KIND
    providers = (PROVIDER,)
    tables = (TABLE,)
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True             # the first run is the server thread's, after a backup

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        listed = [d for d in published(ctx) if d <= as_of]
        stored = stored_dates()
        newer = [d for d in listed if not stored or d > stored[-1]]
        todo = newer[-DAILY_MAX_DATES:] if stored else listed[-1:]
        if len(newer) > len(todo) and stored:
            ctx.notes.append(f"{len(newer) - len(todo)} older missing dates left for a backfill")
        return [fetch_date(ctx, d) for d in todo]

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        have = set(stored_dates())
        todo = [d for d in published(ctx) if (start is None or d >= start) and d <= end and d not in have]
        ctx.notes.append(f"{len(todo)} settlement dates to fetch")
        self.to_parquet_all(ctx, [fetch_date(ctx, d) for d in todo])

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out, repeats = [], 0
        for raw in raws:
            rows, rep = parse(base.read_raw_json(raw))
            repeats += rep
            final = parquet_dir() / f"date={rows[0]['settlement_date'].isoformat()}" / "part-0.parquet"
            con = duckdb.connect()
            try:
                base.rows_table(con, "rows", SCHEMA, rows)
                out.append(base.write_parquet(con, "SELECT * FROM rows ORDER BY symbol", final))
            finally:
                con.close()
        if repeats:
            ctx.notes.append(f"{repeats} repeated symbols of a settlement date (the first row kept)")
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        files = [f for _, f in partitions(parquet_dir(), "date")]
        if not files:
            return {TABLE: common.create_table(con, TABLE, SERVED)}
        src = common.files_sql(files)
        common.symbol_map(con, f"SELECT symbol FROM {src}", ctx.notes, NAME)
        select = (f"SELECT p.*, m.ticker, m.ticker_source, '{NAME}' AS source FROM {src} p "
                  f"LEFT JOIN _symbol_map m USING (symbol) ORDER BY p.settlement_date, p.symbol")
        return {TABLE: common.create_table(con, TABLE, SERVED, select)}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["settlement_date", "symbol", "ticker", "current_short", "days_to_cover"]
        added, removed = base.compare_with_live(con, KIND, TABLE, cols)
        newest = con.execute(f"SELECT max(settlement_date) FROM {TABLE}").fetchone()[0]
        rows = common.count(con, TABLE)
        named = (common.count(con, TABLE, f"settlement_date = DATE '{newest}' AND ticker IS NOT NULL")
                 if newest else 0)
        latest = common.count(con, TABLE, f"settlement_date = DATE '{newest}'") if newest else 0
        if newest:
            ctx.notes.append(f"{latest} rows on {newest}, {named} named by the spine")
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (newest or ctx.as_of or self.default_as_of()).isoformat(), "rows": rows,
                "added": added, "removed": removed, "coverage": round(named / latest, 4) if latest else None}


DATASET = FinraShortInterest()
