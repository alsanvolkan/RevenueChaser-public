"""``ledger/outcomes`` and ``ledger/baselines``: the forecast ledger's derived tables, recomputed by the builder (PR 10).

They were the nightly ``RC-Forecast-Outcomes`` and ``RC-Forecast-Baselines`` automations (``scripts/forecast_*.py``),
writing ``raw.duckdb``. Now ``ledger.duckdb`` is a serving kind only the builder writes, so they are two of its
datasets: each build copies the live table, and the requested one recomputes its rows in the build file
(``core/outcomes.py``, ``core/baselines.py``; the rules are unchanged, the bars come from the prices store). A request
can ask for ``{"full": true}`` (recompute every row, after a bar backfill or a rule change); the daily one skips the
closed rows.

Due once a day: the as-of is the prices store's newest session, so each runs once per new day of bars (``due``'s daily
rule waits for the next session after the one it ran for), and never before the builder has read that day's bars.
Their check also runs the ledger's append-only guard: a build of the kind must never change a frozen pick, whichever
dataset asked for it.
"""
from __future__ import annotations

from datetime import date

import duckdb

from ....core import baselines, calendar, market_bars, outcomes
from .. import base
from . import (BASELINES, BASELINES_KEY, BASELINES_SCHEMA, KIND, LEDGER, LEDGER_SCHEMA, OUTCOMES, OUTCOMES_KEY,
               OUTCOMES_SCHEMA, append_only_problem, copy_live)


class _Derived:
    kind = KIND
    providers: tuple[str, ...] = ()
    needs_parquet = False      # computed from the ledger and the prices store, never from raw files
    name: str
    table: str
    schema: list[tuple[str, str]]
    key: list[str]
    module = None              # core.outcomes or core.baselines: ``run(con, run_id=, full=)``

    @property
    def tables(self) -> tuple[str, ...]:
        return (self.table,)

    def default_as_of(self) -> date:
        """The prices store's newest session (one run per new day of bars); the latest complete session without
        bars."""
        return market_bars.newest_session() or calendar.latest_complete_session()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list:
        return []

    def to_parquet(self, ctx: base.RunContext, raw) -> list:
        return []

    def to_parquet_all(self, ctx: base.RunContext, raws: list) -> list:
        return []

    def backfill(self, ctx: base.RunContext, start, end) -> None:
        raise ValueError(f"{self.name} has no backfill: queue it with {{\"full\": true}} to recompute every row")

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        copy_live(con, self.table, self.schema, self.key)
        if ctx.dataset == self.name:
            ctx.pending[self.name] = self.module.run(con, run_id=ctx.run_id,
                                                     full=(ctx.params or {}).get("full") is True)
        return {self.table: int(con.execute(f"SELECT count(*) FROM {self.table}").fetchone()[0])}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        problem = append_only_problem(con, LEDGER, LEDGER_SCHEMA)
        added, removed = base.compare_with_live(con, KIND, self.table, [c for c, _ in self.schema])
        rows = int(con.execute(f"SELECT count(*) FROM {self.table}").fetchone()[0])
        res = ctx.pending.get(self.name) or {}
        out = {"dataset": self.name, "rows": rows, "added": added, "removed": removed}
        if problem:
            return {**out, "result": "failed", "note": problem}
        note = (f"{res.get('rows', 0)} row(s) computed{' (full)' if res.get('full') else ''}, "
                f"status {res.get('status') or {}}")
        return {**out, "result": "changed" if added or removed else "unchanged", "note": note}


class Outcomes(_Derived):
    name = "ledger/outcomes"
    table = OUTCOMES
    schema = OUTCOMES_SCHEMA
    key = OUTCOMES_KEY
    module = outcomes


class Baselines(_Derived):
    name = "ledger/baselines"
    table = BASELINES
    schema = BASELINES_SCHEMA
    key = BASELINES_KEY
    module = baselines


OUTCOMES_DATASET = Outcomes()
BASELINES_DATASET = Baselines()
