"""Staging tables: a source's Parquet loaded into a TEMP table of the build connection, and the live comparisons.

A TEMP table lives in the connection, not in the file, so staging never reaches ``spine.duckdb``; ``spine/security``
reads them in the same build. Every source creates its staging tables even with no Parquet yet (empty, typed), so
the security build never meets a missing table.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import duckdb

from .. import base
from . import KIND


def create(con: duckdb.DuckDBPyConnection, name: str, schema: list[tuple[str, str]],
           files: Iterable[Path] = (), select: str | None = None) -> int:
    """``CREATE TEMP TABLE name`` with ``schema`` and load ``files`` (Parquet, by name) into it; its row count.

    ``select`` is an optional query over ``{src}`` (the union of the files) that shapes what is loaded, e.g. the
    newest row per key.
    """
    cols = ", ".join(f'"{c}" {t}' for c, t in schema)
    con.execute(f"CREATE OR REPLACE TEMP TABLE {name} ({cols})")
    files = sorted(Path(f) for f in files)
    if files:
        src = f"read_parquet([{', '.join(base.sql_path(f) for f in files)}], union_by_name = true)"
        # A column added to a schema after the Parquet was written is in none of the files, and selecting it would
        # fail the build; it is read as NULL until the source is fetched again (#240 added ``company.sic``).
        have = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
        names = ", ".join(f'"{c}"' if c in have else f'NULL AS "{c}"' for c, _ in schema)
        body = select.replace("{src}", src) if select else f"SELECT {names} FROM {src}"
        con.execute(f"INSERT INTO {name} SELECT {names} FROM ({body})")
    return count(con, name)


def count(con: duckdb.DuckDBPyConnection, table: str, where: str = "") -> int:
    return int(con.execute(f"SELECT count(*) FROM {table}" + (f" WHERE {where}" if where else "")).fetchone()[0])


def partitions(dataset_dir: Path, prefix: str, name: str = "part-0.parquet") -> list[tuple[str, Path]]:
    """``[(value, file)]`` of the partitions ``<dataset_dir>/<prefix>=<value>/<name>`` that exist, by value."""
    if not dataset_dir.is_dir():
        return []
    out = []
    for d in dataset_dir.iterdir():
        if d.is_dir() and d.name.startswith(prefix + "=") and (d / name).is_file():
            out.append((d.name[len(prefix) + 1:], d / name))
    return sorted(out)


def compare_live(con: duckdb.DuckDBPyConnection, select_new: str, table: str, columns: list[str],
                 where: str = "") -> tuple[int, int]:
    """``(added, removed)``: rows of ``select_new`` (over the build connection) against ``table`` of the live
    ``spine.duckdb`` on ``columns``, the live rows filtered by ``where``. No live file or table: all added."""
    cols = ", ".join(f'"{c}"' for c in columns)
    new_rows = int(con.execute(f"SELECT count(*) FROM ({select_new})").fetchone()[0])
    from ... import serving
    live = serving.live_path(KIND)
    if not live.exists():
        return new_rows, 0
    con.execute(f"ATTACH {base.sql_path(live)} AS live_spine (READ_ONLY)")
    try:
        known = {r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'live_spine'").fetchall()}
        if table not in known:
            return new_rows, 0
        old = f'SELECT {cols} FROM live_spine."{table}"' + (f" WHERE {where}" if where else "")
        new = f"SELECT {cols} FROM ({select_new})"
        added = con.execute(f"SELECT count(*) FROM ({new} EXCEPT ALL {old})").fetchone()[0]
        removed = con.execute(f"SELECT count(*) FROM ({old} EXCEPT ALL {new})").fetchone()[0]
        return int(added), int(removed)
    finally:
        con.execute("DETACH live_spine")
