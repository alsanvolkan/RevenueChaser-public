"""``fundamentals/tables``: no fetch; the served tables and views of ``fundamentals.duckdb`` from the sources' staging
tables, and the build-issue rules (``ISSUE_KINDS``), whose rows go to ``ops.sqlite`` once the new file is live.

``build_kind`` builds every dataset of the kind in ``DATASETS`` order, so the sources' staging tables exist when this
one builds; it is registered last.
"""
from __future__ import annotations

from datetime import date

import duckdb

from ....core import calendar
from ... import ops
from .. import base
from . import KIND, common

NAME = "fundamentals/tables"

COMPANY = [
    ("ticker", "TEXT PRIMARY KEY"), ("cik", "TEXT NOT NULL"), ("gvkey", "TEXT"), ("name", "TEXT"),
    ("exchange", "TEXT"), ("gsector", "TEXT"), ("ggroup", "TEXT"), ("gind", "TEXT"), ("gsubind", "TEXT"),
    ("sector", "TEXT"), ("industry", "TEXT"), ("spine_as_of", "DATE"),
    ("source", "TEXT"), ("run_id", "TEXT"),
]

SCHEMAS: dict[str, list[tuple[str, str]]] = {
    "fundamental_company": COMPANY,
}
TABLE_CHECKS: dict[str, list[str]] = {}
TABLES = tuple(SCHEMAS)

# The named problems, one kind each; ``key`` is the CIK unless the kind says otherwise.
ISSUE_KINDS: dict[str, str] = {
    "source_never_run": "a source this build needs has no data yet (key: the dataset)",
}


def create(con: duckdb.DuckDBPyConnection, table: str) -> None:
    cols = [f'"{c}" {t}' for c, t in SCHEMAS[table]] + TABLE_CHECKS.get(table, [])
    con.execute(f"CREATE TABLE {table} ({', '.join(cols)})")


def _plain(table: str) -> list[tuple[str, str]]:
    return [(c, t.replace(" PRIMARY KEY", "").replace(" NOT NULL", "")) for c, t in SCHEMAS[table]]


def load(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict]) -> int:
    """Create served ``table`` and insert Python ``rows`` into it (typed through ``base.rows_table``)."""
    create(con, table)
    if rows:
        base.rows_table(con, f"_load_{table}", _plain(table), rows)
        con.execute(f"INSERT INTO {table} SELECT * FROM _load_{table}")
        con.execute(f"DROP TABLE _load_{table}")
    return common.count(con, table)


def _companies(ctx: base.RunContext, con: duckdb.DuckDBPyConnection, rows: list[dict]) -> int:
    out = [{"ticker": r["ticker"], "cik": common.norm_cik(r["cik"]), "gvkey": r.get("gvkey"), "name": r.get("name"),
            "exchange": r.get("exchange"), "gsector": r.get("gsector"), "ggroup": r.get("ggroup"),
            "gind": r.get("gind"), "gsubind": r.get("gsubind"), "sector": r.get("sector"),
            "industry": r.get("industry"), "spine_as_of": r.get("source_as_of"), "source": "spine/security",
            "run_id": ctx.run_id} for r in rows]
    return load(con, "fundamental_company", out)


class FundamentalsTables:
    name = NAME
    kind = KIND
    providers: tuple[str, ...] = ()
    tables = TABLES
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list:
        return []                                   # built from the sources' Parquet only

    def backfill(self, ctx: base.RunContext, start, end: date) -> None:
        return None

    def to_parquet(self, ctx, raw) -> list:
        return []

    def to_parquet_all(self, ctx, raws) -> list:
        return []

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        found: list[tuple[str, str, str]] = []
        counts = {"fundamental_company": _companies(ctx, con, common.universe())}
        ctx.pending[NAME] = found
        return counts

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["ticker", "cik", "gvkey", "gsubind"]
        added, removed = common.compare_live(con, f"SELECT {', '.join(cols)} FROM fundamental_company",
                                             "fundamental_company", cols)
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (ctx.as_of or self.default_as_of()).isoformat(),
                "rows": common.count(con, "fundamental_company"), "added": added, "removed": removed}

    def after_swap(self, ctx: base.RunContext) -> None:
        found = ctx.pending.get(NAME)
        if found is None:
            return
        ops.sync_issues(NAME, ISSUE_KINDS, found, run_id=ctx.run_id)


DATASET = FundamentalsTables()
