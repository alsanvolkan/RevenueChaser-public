"""Staging of the prices store: the spine's helpers (``create``, ``count``, ``partitions``) and the live comparison
against ``prices.duckdb``.

A TEMP table lives in the build connection, not in the file, so staging never reaches ``prices.duckdb``; every
source creates its staging tables even with no Parquet yet (empty, typed), so ``prices/serving`` never meets a
missing table.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from .. import base
from ..spine.stage import count, create, partitions  # noqa: F401 - the one staging helper, shared with the spine
from . import KIND


def write_rows(rows: list[dict], schema: list[tuple[str, str]], final: Path, order: str = "") -> Path:
    """Write ``rows`` typed by ``schema`` to the Parquet file ``final`` (``.tmp`` and a rename)."""
    con = duckdb.connect()
    try:
        base.rows_table(con, "rows_out", schema, rows)
        return base.write_parquet(con, "SELECT * FROM rows_out" + (f" ORDER BY {order}" if order else ""), final)
    finally:
        con.close()


def read_rows(path: Path) -> list[dict]:
    """Every row of one Parquet file, as dicts (``[]`` when it does not exist)."""
    if not path.is_file():
        return []
    con = duckdb.connect()
    try:
        cur = con.execute(f"SELECT * FROM read_parquet({base.sql_path(path)})")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()


def compare_live(con: duckdb.DuckDBPyConnection, table: str, columns: list[str], where: str = "") -> tuple[int, int]:
    """``(added, removed)``: rows of the build's ``table`` against the live ``prices.duckdb`` on ``columns``, both
    sides filtered by ``where``. No live file or table: every row is added."""
    cols = ", ".join(f'"{c}"' for c in columns)
    cond = f" WHERE {where}" if where else ""
    new = f'SELECT {cols} FROM "{table}"{cond}'
    new_rows = int(con.execute(f"SELECT count(*) FROM ({new})").fetchone()[0])
    from ... import serving
    live = serving.live_path(KIND)
    if not live.exists():
        return new_rows, 0
    con.execute(f"ATTACH {base.sql_path(live)} AS live_prices (READ_ONLY)")
    try:
        known = {r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'live_prices'").fetchall()}
        if table not in known:
            return new_rows, 0
        old = f'SELECT {cols} FROM live_prices."{table}"{cond}'
        added = con.execute(f"SELECT count(*) FROM ({new} EXCEPT ALL {old})").fetchone()[0]
        removed = con.execute(f"SELECT count(*) FROM ({old} EXCEPT ALL {new})").fetchone()[0]
        return int(added), int(removed)
    finally:
        con.execute("DETACH live_prices")
