"""``alpaca/corporate_actions``: Alpaca's splits, dividends, spin-offs, mergers and name changes, every symbol at once.

**One run** asks ``/v1/corporate-actions`` for every action Alpaca processes from ``LOOKBACK`` days before ``as_of``
to ``AHEAD`` days after it (``start`` / ``end`` filter the process date, inclusive), with no ``symbols``: the whole
market is a few pages of 1,000, fewer requests than asking the scope 100 at a time, and ``prices/serving`` keeps only
the actions of the securities it knows. The window reaches ahead because a cash dividend's process date is its
payable date, weeks after its ex-date, while Alpaca's adjusted bars apply it from the ex-date: a window that ended
on ``as_of`` missed JPM's dividend that went ex on 6 Oct 2026 and is paid on the 31st (the server's sample,
29 Oct 2026). ``prices/serving`` applies an action once its ex-date has a session. A backfill asks windows of
``BACKFILL_DAYS`` of its own, up to ``AHEAD`` days past its end.

**Raw**: ``raw/alpaca/corporate_actions/<as_of>.json.gz`` (a backfill window: ``backfill/<start>_<end>.json.gz``),
every page; a file that exists is not asked again.

**Parquet**: ``parquet/alpaca/corporate_actions/year=<Y>/part-0.parquet`` keyed on Alpaca's action ``id``, ``Y`` the
year of the ex-date (the effective date of a unit split, the process date of a name change), the newest fetch
winning an id. One row per action: its type in the singular (``reverse_splits`` -> ``reverse_split``), the symbol it
applies to (``symbol``, else ``source_symbol`` of a spin-off, else ``old_symbol`` of a name change), the new symbol,
the CUSIP, the dates, the rates (``old_rate`` / ``new_rate`` of a split, ``rate`` of a dividend) and the flags, and
the whole item as JSON (``payload``) for the fields the columns do not hold.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from ..sources import alpaca
from . import KIND, stage

NAME = "alpaca/corporate_actions"
LOOKBACK = timedelta(days=14)
AHEAD = timedelta(days=90)          # past as_of: the payable dates of the dividends already ex
BACKFILL_DAYS = 90
LIMIT = 1000
SOURCE_DIR = ("alpaca", "corporate_actions")
SCHEMA = [("action_id", "TEXT"), ("action_type", "TEXT"), ("symbol", "TEXT"), ("new_symbol", "TEXT"),
          ("cusip", "TEXT"), ("new_cusip", "TEXT"), ("ex_date", "DATE"), ("record_date", "DATE"),
          ("payable_date", "DATE"), ("process_date", "DATE"), ("effective_date", "DATE"), ("old_rate", "DOUBLE"),
          ("new_rate", "DOUBLE"), ("rate", "DOUBLE"), ("special", "BOOLEAN"), ("foreign", "BOOLEAN"),
          ("sub_type", "TEXT"), ("currency", "TEXT"), ("payload", "TEXT"), ("source_as_of", "DATE"),
          ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
KEY = ["action_id"]
COMPARE = [c for c, _ in SCHEMA if c not in ("source_as_of", "fetched_at", "run_id")]
DATES = ("ex_date", "record_date", "payable_date", "process_date", "effective_date")


def raw_path(as_of: date) -> Path:
    return base.raw_root().joinpath(*SOURCE_DIR, f"{as_of.isoformat()}.json.gz")


def backfill_path(start: date, end: date) -> Path:
    return base.raw_root().joinpath(*SOURCE_DIR, "backfill", f"{start.isoformat()}_{end.isoformat()}.json.gz")


def partition_path(year: int) -> Path:
    return base.parquet_root().joinpath(*SOURCE_DIR, f"year={year}", "part-0.parquet")


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value not in (None, "") else None
    except ValueError:
        return None


def _num(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def rows_of(payload: dict) -> list[dict]:
    """The typed rows of one raw file's pages."""
    out = []
    as_of = date.fromisoformat(payload["as_of"])
    for page in payload["pages"]:
        for kind, items in (page or {}).items():
            for item in items or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                row = {"action_id": str(item["id"]), "action_type": kind[:-1] if kind.endswith("s") else kind,
                       "symbol": item.get("symbol") or item.get("source_symbol") or item.get("old_symbol")
                       or item.get("target_symbol"),
                       "new_symbol": item.get("new_symbol"),
                       "cusip": item.get("cusip") or item.get("old_cusip") or item.get("source_cusip"),
                       "new_cusip": item.get("new_cusip"),
                       **{d: _day(item.get(d)) for d in DATES},
                       "old_rate": _num(item.get("old_rate") or item.get("source_rate")),
                       "new_rate": _num(item.get("new_rate")), "rate": _num(item.get("rate")),
                       "special": item.get("special"), "foreign": item.get("foreign"),
                       "sub_type": item.get("sub_type"), "currency": item.get("currency"),
                       "payload": json.dumps(item, sort_keys=True), "source_as_of": as_of,
                       "fetched_at": payload["fetched_at"], "run_id": payload["run_id"]}
                out.append(row)
    return out


def effective(row: dict) -> date | None:
    return row["ex_date"] or row["effective_date"] or row["process_date"]


class AlpacaActions:
    name = NAME
    kind = KIND
    providers = (alpaca.PROVIDER,)
    tables = ("corporate_action",)
    cadence = "daily"
    starts_by_hand = True

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def _ask(self, ctx: base.RunContext, path: Path, start: date, end: date, as_of: date, pace) -> Path:
        if path.exists():
            return path
        params = {"start": start.isoformat(), "end": end.isoformat(), "limit": LIMIT, "sort": "asc"}
        pages = alpaca.get_pages(ctx, "/v1/corporate-actions", params, pace, "corporate_actions")
        return base.write_raw_json(path, {"as_of": as_of.isoformat(), "start": params["start"],
                                          "end": params["end"], "pages": pages,
                                          "fetched_at": calendar._now().timestamp(), "run_id": ctx.run_id})

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        return [self._ask(ctx, raw_path(as_of), as_of - LOOKBACK, as_of + AHEAD, as_of, alpaca.pacer(ctx))]

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        pace, out, lo, last = alpaca.pacer(ctx), [], start, end + AHEAD
        while lo <= last:
            hi = min(lo + timedelta(days=BACKFILL_DAYS - 1), last)
            out.append(self._ask(ctx, backfill_path(lo, hi), lo, hi, end, pace))
            lo = hi + timedelta(days=1)
        self.to_parquet_all(ctx, out)

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        years: dict[int, list[dict]] = {}
        for raw in raws:
            for row in rows_of(base.read_raw_json(raw)):
                day = effective(row)
                if day is None:
                    ctx.issue("action_undated", row["action_id"], f"{row['action_type']} {row['symbol']}: no date")
                    continue
                years.setdefault(day.year, []).append(row)
        kinds: dict[str, int] = {}
        for rows in years.values():
            for r in rows:
                kinds[r["action_type"]] = kinds.get(r["action_type"], 0) + 1
        if kinds:
            ctx.notes.append("actions read: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())))
        return [base.upsert_partition(rows, SCHEMA, KEY, partition_path(y)) for y, rows in sorted(years.items())]

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        stage.create(con, "stg_alpaca_actions", SCHEMA,
                     [f for _, f in stage.partitions(base.parquet_root().joinpath(*SOURCE_DIR), "year")])
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["security_id", "ex_date", "action", "ratio", "amount", "source_id"]
        added, removed = stage.compare_live(con, "corporate_action", cols, f"source = '{NAME}'")
        as_of = ctx.as_of or self.default_as_of()
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": as_of.isoformat(), "rows": stage.count(con, "stg_alpaca_actions"),
                "added": added, "removed": removed, "coverage": None}


DATASET = AlpacaActions()
