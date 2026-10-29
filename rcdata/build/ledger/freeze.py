"""``ledger/freeze``: append one board's frozen picks to ``forecast_ledger`` (PR 10, G4).

``core.ledger.write_picks`` decides whether a board may freeze (the session gate, the payload, SPY's anchor, every
pick's complete bar) and queues this dataset with the picks as ``params["rows"]``; nothing else queues it, so it is
never due (``by_request``). The build copies the live ledger, then appends the request's rows per (source, day) unless
that day already has picks: the first freeze of a day wins, also when two Fetches queued it before the builder ran.
The check refuses a build that would remove or change a live row (append-only), so the swap leaves the file as it
was.
"""
from __future__ import annotations

from datetime import date

import duckdb

from ....core import calendar
from .. import base
from . import KIND, LEDGER, LEDGER_KEY, LEDGER_SCHEMA, append_only_problem, copy_live

NAME = "ledger/freeze"
PICK = "pick"
_COLUMNS = [c for c, _ in LEDGER_SCHEMA]


def _copy_live(con: duckdb.DuckDBPyConnection, table: str, schema: list[tuple[str, str]]) -> int:
    return copy_live(con, table, schema, LEDGER_KEY)


def _day(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _row(raw: dict) -> dict:
    """One request row, checked and typed; a malformed row raises (the whole request fails, nothing is written)."""
    row = {c: raw.get(c) for c in _COLUMNS}
    row["subject"] = str(raw.get("subject") or "").strip().upper()
    if not row["subject"] or not str(raw.get("source") or "").strip():
        raise ValueError(f"a ledger row needs a source and a subject: {raw!r}"[:300])
    rank = raw.get("rank")
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise ValueError(f"rank is 1-based; got {rank!r} for {row['subject']}")
    for c in ("forecast_date", "target_date"):
        row[c] = _day(raw[c])
    if raw.get("price_session"):
        row["price_session"] = _day(raw["price_session"])
    return row


class Freeze:
    name = NAME
    kind = KIND
    providers: tuple[str, ...] = ()
    tables = (LEDGER,)
    by_request = True          # only a board's freeze queues it (``core.ledger.write_picks``): never due
    needs_parquet = False      # its rows come in the request, not from raw files

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list:
        return []

    def to_parquet(self, ctx: base.RunContext, raw) -> list:
        return []

    def to_parquet_all(self, ctx: base.RunContext, raws: list) -> list:
        return []

    def backfill(self, ctx: base.RunContext, start, end) -> None:
        raise ValueError(f"{NAME} has no backfill: its rows come from a board's freeze")

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        _copy_live(con, LEDGER, LEDGER_SCHEMA)
        if ctx.dataset == NAME:
            self._append(ctx, con, [_row(r) for r in (ctx.params or {}).get("rows") or []])
        return {LEDGER: int(con.execute(f"SELECT count(*) FROM {LEDGER}").fetchone()[0])}

    def _append(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection, rows: list[dict]) -> None:
        days: dict[tuple[str, date], list[dict]] = {}
        for r in rows:
            days.setdefault((r["source"], r["forecast_date"]), []).append(r)
        fresh, skipped = [], []
        for (source, day), group in sorted(days.items()):
            hit = con.execute(f"SELECT 1 FROM {LEDGER} WHERE source = ? AND forecast_date = ? AND target_type = ? "
                              "LIMIT 1", [source, day, PICK]).fetchone()
            if hit:
                skipped.append(f"{source} already frozen for {day}")
                continue
            seen = set()
            for r in group:                          # a subject twice in one request: the first one wins
                if (r["subject"], r["target_type"], r["target_date"]) not in seen:
                    seen.add((r["subject"], r["target_type"], r["target_date"]))
                    fresh.append(r)
        ctx.pending["ledger_skipped"] = skipped
        ctx.pending["ledger_days"] = sorted({d for _, d in days})
        if not fresh:
            return
        base.rows_table(con, "_new_picks", LEDGER_SCHEMA, fresh)
        try:
            cols = ", ".join(f'"{c}"' for c in _COLUMNS)
            con.execute(f"INSERT INTO {LEDGER} ({cols}) SELECT {cols} FROM _new_picks")
        finally:
            con.execute("DROP TABLE _new_picks")

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        problem = append_only_problem(con, LEDGER, LEDGER_SCHEMA)
        added, removed = base.compare_with_live(con, KIND, LEDGER, _COLUMNS)
        rows = int(con.execute(f"SELECT count(*) FROM {LEDGER}").fetchone()[0])
        days = ctx.pending.get("ledger_days") or []
        note = "; ".join(ctx.pending.get("ledger_skipped") or []) or None
        out = {"dataset": NAME, "rows": rows, "added": added, "removed": removed,
               "source_as_of": max(days).isoformat() if days else None}
        if problem:
            return {**out, "result": "failed", "note": problem}
        return {**out, "result": "changed" if added or removed else "unchanged", "note": note}


DATASET = Freeze()
