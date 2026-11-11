"""#446: the ``market`` kind built on a copy of the live file, reloading only the Parquet partitions that changed.

Every table of ``market`` is a plain union of its Parquet partitions, and each partition holds exactly the rows of one
key: ``uw/spot_expiry_strike`` and ``uw/greek_expiry_strike`` a session (``date=<YYYY-MM-DD>``, the rows'
``session_date``), ``uw/economic_calendar`` a snapshot day (``date=``, ``snapshot_date``),
``market_series`` a source and a year (``<source>/<dir>/year=<YYYY>``, the rows' ``source`` and
``year(obs_date)``). So a build on the live copy deletes the served rows of each dirty key and reads back that key's
partition when it still exists (a gone one only deletes), and the check compares only those keys with the live file:
every other row is the live file's own. A whole build (``ctx.incremental`` None) is the old ``CREATE`` and union.

The proof is ``rcdata/tests/test_market_incremental.py``: a build on the live copy serves what a whole build serves.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import duckdb

from .. import serving
from . import base


def dirty_keys(ctx: Any, name: str) -> frozenset[str] | None:
    """The keys a build on the live copy reloads for dataset ``name``; None for a whole build."""
    plan = getattr(ctx, "incremental", None)
    return None if plan is None else plan.dirty[name].keys


def where_any(keys: frozenset[str], where_of: Callable[[str], str]) -> str:
    """One SQL condition true for the rows of any of ``keys`` (``FALSE`` for none)."""
    return " OR ".join(f"({where_of(k)})" for k in sorted(keys)) or "FALSE"


def load(ctx: Any, con: duckdb.DuckDBPyConnection, name: str, table: str, schema: list[tuple[str, str]],
         key: Any, parts: dict[str, Path], where_of: Callable[[str], str]) -> dict[str, int]:
    """Build ``table`` from ``parts`` (``{key: partition}``): whole, or on the live copy its dirty keys only."""
    keys = dirty_keys(ctx, name)
    if keys is None:
        cols = ", ".join(f'"{c}" {t}' for c, t in schema)
        con.execute(f"CREATE TABLE {table} ({cols}, PRIMARY KEY ({', '.join(key)}))")
        files = [parts[k] for k in sorted(parts)]
    else:
        con.execute(f"DELETE FROM {table} WHERE {where_any(keys, where_of)}")
        files = [parts[k] for k in sorted(keys) if k in parts]
    if files:
        names = ", ".join(f'"{c}"' for c, _ in schema)
        listed = ", ".join(base.sql_path(p) for p in files)
        con.execute(f"INSERT INTO {table} SELECT {names} FROM read_parquet([{listed}], union_by_name = true)")
    return {table: int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])}


def compare(ctx: Any, con: duckdb.DuckDBPyConnection, kind: str, name: str, table: str, columns: list[str],
            where_of: Callable[[str], str]) -> tuple[int, int]:
    """``base.compare_with_live``, over the dirty keys only on a build on the live copy."""
    keys = dirty_keys(ctx, name)
    if keys is None:
        return base.compare_with_live(con, kind, table, columns)
    if not keys:
        return 0, 0
    cond = where_any(keys, where_of)
    cols = ", ".join(f'"{c}"' for c in columns)
    con.execute(f"ATTACH {base.sql_path(serving.live_path(kind))} AS live_cmp (READ_ONLY)")
    try:
        new = f'SELECT {cols} FROM "{table}" WHERE {cond}'
        old = f'SELECT {cols} FROM live_cmp."{table}" WHERE {cond}'
        added = con.execute(f"SELECT count(*) FROM ({new} EXCEPT ALL {old})").fetchone()[0]
        removed = con.execute(f"SELECT count(*) FROM ({old} EXCEPT ALL {new})").fetchone()[0]
        return int(added), int(removed)
    finally:
        con.execute("DETACH live_cmp")
