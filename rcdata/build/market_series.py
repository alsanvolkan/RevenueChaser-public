"""``market_series``: the market-wide series in one serving table, keyed ``(series_id, obs_date, source)``.

Sources (``rcdata/build/sources/``): Treasury par yields, Cboe index files, FRED, CFTC COT (keyed on contract code),
FOMC meeting dates, CPI release dates and Yahoo's daily chart for MOVE and wheat (PR 9). Each source writes raw files under ``raw/<source>/<dataset>/`` with a
``<name>.meta.json`` beside each (the as-of date, the fetch time, the run id and the URL without its query; written
first, so the raw file itself stays the resume marker), and the rows go to
``parquet/<source>/<dataset>/year=<YYYY>/part-0.parquet``, merged by key, the newest fetch winning a key. Nothing is
dropped from Parquet by a later fetch: a series that a source serves only for a window keeps the days it once had.

A source that fails is a build issue and the others go on; its earlier Parquet still builds, so its rows stay
served. All sources failing is a failed run.

``session_date`` is the New York session on or before ``obs_date`` from ``core/calendar.py``, and empty outside its
2025-2028 table (the calendar's rule C).
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import duckdb

from ...core import calendar
from . import base
from .sources import bls, cboe, cftc, fed, fred, treasury, yahoo

NAME = "market_series"
TABLE = "market_series"
SOURCES = (treasury, cboe, fred, cftc, fed, bls, yahoo)
SCHEMA = [("series_id", "TEXT"), ("obs_date", "DATE"), ("source", "TEXT"), ("value", "DOUBLE"),
          ("open", "DOUBLE"), ("high", "DOUBLE"), ("low", "DOUBLE"), ("close", "DOUBLE"), ("frequency", "TEXT"),
          ("session_date", "DATE"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
KEY = ["series_id", "obs_date", "source"]
# What makes a row "changed": its data, not when or by which run it was fetched (every fetch restamps those).
COMPARE = [c for c, _ in SCHEMA if c not in ("fetched_at", "run_id", "source_as_of")]
META_SUFFIX = ".meta.json"


def meta_path(raw: Path) -> Path:
    return raw.with_name(raw.name + META_SUFFIX)


def save_raw(ctx: base.RunContext, path: Path, content: bytes, as_of: date, url: str, **extra) -> Path:
    """Write one raw file and its metadata sidecar (the sidecar first: the raw file is the marker)."""
    base.write_raw_json(meta_path(path), {"as_of": as_of.isoformat(), "fetched_at": time.time(),
                                          "run_id": ctx.run_id, "url": url, **extra})
    return base.write_raw(path, content)


def _source_of(raw: Path):
    rel = raw.relative_to(base.raw_root())
    for src in SOURCES:
        if rel.parts[:2] == (src.SOURCE, src.DATASET_DIR):
            return src
    raise ValueError(f"{raw} is not a market_series raw file")


def partition_path(src, year: int) -> Path:
    return base.parquet_root() / src.SOURCE / src.DATASET_DIR / f"year={year}" / "part-0.parquet"


def _session_dates(ctx: base.RunContext, source: str, rows: list[dict]) -> None:
    """Set each row's session date. Before the calendar's table is expected (rule C); a date after it (a meeting
    already announced for a year the table does not cover yet) is reported once, and its rows keep no session."""
    cache: dict[date, date | None] = {}
    last_year = max(calendar.MARKET_HOLIDAYS)
    ahead = 0
    for r in rows:
        d = r["obs_date"]
        if d not in cache:
            cache[d] = base.session_date_of(d)
        r["session_date"] = cache[d]
        ahead += d.year > last_year
    if ahead:
        ctx.issue("outside_calendar", source,
                  f"{ahead} rows after {last_year}, the last year of core/calendar.py's table: no session date")


class MarketSeries:
    name = NAME
    kind = "market"
    providers = tuple(getattr(s, "PROVIDER", s.SOURCE) for s in SOURCES)   # Yahoo counts under yfinance
    tables = (TABLE,)

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def _each_source(self, ctx: base.RunContext, step) -> list[Path]:
        out, failed = [], []
        for src in SOURCES:
            try:
                out += step(src)
            except Exception as e:  # noqa: BLE001 - one source down must not stop the others
                failed.append(src.SOURCE)
                ctx.issue("source_failed", src.SOURCE, f"{type(e).__name__}: {e}"[:300])
        if failed:
            ctx.notes.append("failed sources: " + ", ".join(failed))
            if len(failed) == len(SOURCES):
                raise base.SourceError("every market_series source failed")
        return out

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        return self._each_source(ctx, lambda src: src.fetch(ctx, as_of))

    def backfill(self, ctx: base.RunContext, start: date, end: date) -> None:
        self.to_parquet_all(ctx, self._each_source(ctx, lambda src: src.backfill(ctx, start, end)))

    def _typed_rows(self, ctx: base.RunContext, raw: Path):
        src = _source_of(raw)
        meta = base.read_raw_json(meta_path(raw))
        rows = src.parse(base.read_raw(raw).decode("utf-8", errors="replace"), meta)
        if not rows:
            ctx.issue("no_rows", raw.relative_to(base.raw_root()).as_posix(), "the raw file parsed to no rows")
        as_of = date.fromisoformat(meta["as_of"])
        for r in rows:
            r.update(source=src.SOURCE, source_as_of=as_of, fetched_at=meta["fetched_at"], run_id=meta["run_id"])
        return src, rows

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        """Type every raw file of a run, then write each touched (source, year) partition once."""
        groups: dict[tuple[str, int], tuple[object, list[dict]]] = {}
        for raw in raws:
            src, rows = self._typed_rows(ctx, raw)
            _session_dates(ctx, src.SOURCE, rows)
            for r in rows:
                groups.setdefault((src.SOURCE, r["obs_date"].year), (src, []))[1].append(r)
        return [base.upsert_partition(rows, SCHEMA, KEY, partition_path(src, year))
                for (_, year), (src, rows) in sorted(groups.items(), key=lambda kv: kv[0])]

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        cols = ", ".join(f'"{c}" {t}' for c, t in SCHEMA)
        con.execute(f"CREATE TABLE {TABLE} ({cols}, PRIMARY KEY ({', '.join(KEY)}))")
        parts = sorted(p for src in SOURCES
                       for p in (base.parquet_root() / src.SOURCE / src.DATASET_DIR).glob("year=*/part-0.parquet"))
        if parts:
            files = ", ".join(base.sql_path(p) for p in parts)
            names = ", ".join(f'"{c}"' for c, _ in SCHEMA)
            con.execute(f"INSERT INTO {TABLE} SELECT {names} FROM read_parquet([{files}], union_by_name = true)")
        return {TABLE: int(con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        added, removed = base.compare_with_live(con, self.kind, TABLE, COMPARE)
        rows = int(con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])
        sources = {r[0] for r in con.execute(f"SELECT DISTINCT source FROM {TABLE}").fetchall()}
        as_of = ctx.as_of or self.default_as_of()
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": as_of.isoformat(), "rows": rows, "added": added, "removed": removed,
                "coverage": round(len(sources) / len(SOURCES), 4)}


DATASET = MarketSeries()
