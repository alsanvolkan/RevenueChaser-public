"""The forecast ledger's serving kind, ``ledger.duckdb`` (PR 10, G4): what each board forecast on a day.

``forecast_ledger``, ``forecast_outcomes`` and ``forecast_baselines`` were rawstore tables in ``raw.duckdb``. They are data, not operational state, so they do not go to ``ops.sqlite``; they become a serving kind
that only the builder writes. Unlike every other kind, its rows come from a board's Fetch through the request queue
(``core.ledger.write_picks`` queues ``ledger/freeze`` with the picks in its params), not from raw files: the file
cannot be rebuilt from the HDD, so a build never starts from Parquet. Each build copies the live tables into the new
file and then applies its own change, and the nightly ``rcdata backup`` copies it like ``ops.sqlite`` (it loops over
``serving.KINDS``).

The old tables on homesrv held 0 rows when this moved (server count, 29 Oct 2026), so nothing is imported.
"""
from __future__ import annotations

import duckdb

from ... import serving
from .. import base

KIND = "ledger"
LEDGER = "forecast_ledger"

# ``core/rawstore.py``'s ``forecast_ledger`` columns, plus ``price_session``: the session of the shown price, per row
# (review AG SF-1; a card's bars can stop before the freeze day).
LEDGER_SCHEMA = [
    ("source", "TEXT"), ("forecast_date", "DATE"), ("subject", "TEXT"), ("target_type", "TEXT"),
    ("target_date", "DATE"), ("frozen_at", "DOUBLE"), ("run_id", "TEXT"), ("model_version", "TEXT"),
    ("rank", "INTEGER"), ("score", "DOUBLE"), ("bucket", "TEXT"), ("probs", "JSON"), ("market_cap", "DOUBLE"),
    ("sector", "TEXT"), ("shown_price", "DOUBLE"), ("stop", "DOUBLE"), ("target", "DOUBLE"),
    ("price_session", "DATE"),
]
LEDGER_KEY = ["source", "forecast_date", "subject", "target_type", "target_date"]

# The derived tables, recomputed by ``ledger/outcomes`` and ``ledger/baselines`` (``core/outcomes.py``,
# ``core/baselines.py``): the rawstore columns, the same keys.
OUTCOMES = "forecast_outcomes"
OUTCOMES_SCHEMA = [
    ("source", "TEXT"), ("forecast_date", "DATE"), ("subject", "TEXT"), ("target_type", "TEXT"),
    ("target_date", "DATE"), ("horizon_days", "INTEGER"), ("ref_price", "DOUBLE"), ("mfe", "DOUBLE"),
    ("mae", "DOUBLE"), ("sessions_seen", "INTEGER"), ("status", "TEXT"), ("computed_at", "DOUBLE"),
    ("model_version", "TEXT"), ("run_id", "TEXT"),
]
OUTCOMES_KEY = [*LEDGER_KEY, "horizon_days"]
BASELINES = "forecast_baselines"
BASELINES_SCHEMA = [
    ("forecast_date", "DATE"), ("horizon_days", "INTEGER"), ("basis", "TEXT"), ("n", "INTEGER"),
    ("ge20", "INTEGER"), ("ge10", "INTEGER"), ("ge5", "INTEGER"), ("mfe_mean", "DOUBLE"), ("mae_mean", "DOUBLE"),
    ("vol_lo", "DOUBLE"), ("vol_hi", "DOUBLE"), ("status", "TEXT"), ("computed_at", "DOUBLE"),
    ("model_version", "TEXT"), ("run_id", "TEXT"),
]
BASELINES_KEY = ["forecast_date", "horizon_days", "basis"]


def create_table(con: duckdb.DuckDBPyConnection, table: str, schema: list[tuple[str, str]], key: list[str]) -> None:
    cols = ", ".join(f'"{c}" {t}' for c, t in schema)
    con.execute(f'CREATE TABLE "{table}" ({cols}, PRIMARY KEY ({", ".join(key)}))')


def copy_live(con: duckdb.DuckDBPyConnection, table: str, schema: list[tuple[str, str]], key: list[str]) -> int:
    """Create ``table`` in the build file and fill it with the live file's rows (none before the first build); a
    column the live file lacks reads as NULL. The row count copied."""
    create_table(con, table, schema, key)
    live = serving.live_path(KIND)
    if not live.exists():
        return 0
    con.execute(f"ATTACH {base.sql_path(live)} AS live_copy (READ_ONLY)")
    try:
        have = {r[0] for r in con.execute(
            "SELECT column_name FROM duckdb_columns() WHERE database_name = 'live_copy' AND table_name = ?",
            [table]).fetchall()}
        if not have:
            return 0
        cols = ", ".join(f'"{c}"' for c, _ in schema)
        select = ", ".join(f'"{c}"' if c in have else f"CAST(NULL AS {t})" for c, t in schema)
        con.execute(f'INSERT INTO "{table}" ({cols}) SELECT {select} FROM live_copy."{table}"')
    finally:
        con.execute("DETACH live_copy")
    return int(con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])


def append_only_problem(con: duckdb.DuckDBPyConnection, table: str, schema: list[tuple[str, str]]) -> str | None:
    """Why the build breaks the append-only rule, or None: any live row missing or changed in the build (compared on
    the columns the live file has, so a column added later does not count as a change)."""
    live = serving.live_path(KIND)
    if not live.exists():
        return None
    con.execute(f"ATTACH {base.sql_path(live)} AS live_ao (READ_ONLY)")
    try:
        have = {r[0] for r in con.execute(
            "SELECT column_name FROM duckdb_columns() WHERE database_name = 'live_ao' AND table_name = ?",
            [table]).fetchall()}
        if not have:
            return None
        cols = ", ".join(f'"{c}"' for c, _ in schema if c in have)
        gone = con.execute(f'SELECT count(*) FROM (SELECT {cols} FROM live_ao."{table}" '
                           f'EXCEPT ALL SELECT {cols} FROM "{table}")').fetchone()[0]
    finally:
        con.execute("DETACH live_ao")
    if gone:
        return f"append-only: {gone} live {table} row(s) would be removed or changed"
    return None
