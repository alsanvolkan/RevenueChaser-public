"""What the ownership datasets share: symbols named by the spine, Parquet loaded into a served table, and the
comparison with the live file.

The spine is read from its live file (``spine.duckdb``), read-only and closed again; a build with no spine names no
ticker and says so in its notes, it never guesses one from the symbol.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import duckdb

from ....core.identity.sources import norm_ticker
from ... import serving
from .. import base
from ..spine.security import squash

SPINE = "spine"
# How a source's symbol became a ticker (``ticker_source``).
EXACT, SQUASHED = "spine:exact", "spine:squash"


def norm_symbol(raw) -> str:
    """A source's symbol in the app's ticker form: ``BRK.B``, ``BRK/B`` and ``BRK B`` all read ``BRK-B``."""
    text = norm_ticker(raw)
    return "-".join(part for part in text.replace("/", " ").replace("-", " ").split())


def spine_tickers() -> set[str] | None:
    """Every ticker of the live spine's ``security`` table; None when there is no spine file yet."""
    try:
        con = serving.open_read(SPINE)
    except serving.ServingFileMissing:
        return None
    try:
        return {r[0] for r in con.execute("SELECT ticker FROM security").fetchall()}
    finally:
        con.close()


def symbol_tickers(symbols: Iterable[str], tickers: set[str] | None) -> dict[str, tuple[str | None, str | None]]:
    """``{symbol: (ticker, ticker_source)}``: the symbol itself when ``tickers`` holds it, else the one ticker that
    squashes to it (FINRA writes Brown-Forman's class A as ``BFA``), else ``(None, None)``."""
    if not tickers:
        return {s: (None, None) for s in symbols}
    squashed = squash(tickers)
    out = {}
    for s in symbols:
        if s in tickers:
            out[s] = (s, EXACT)
        else:
            hit = squashed.get(s.replace("-", ""))
            out[s] = (hit, SQUASHED) if hit else (None, None)
    return out


def files_sql(files: Iterable[Path]) -> str:
    return f"read_parquet([{', '.join(base.sql_path(f) for f in sorted(files))}], union_by_name = true)"


def create_table(con: duckdb.DuckDBPyConnection, table: str, schema: list[tuple[str, str]],
                 select: str | None = None) -> int:
    """``CREATE TABLE table`` with ``schema`` in the build file, filled by ``select`` (its columns by name) when
    given; its row count."""
    cols = ", ".join(f'"{c}" {t}' for c, t in schema)
    con.execute(f"CREATE TABLE {table} ({cols})")
    if select:
        names = ", ".join(f'"{c}"' for c, _ in schema)
        con.execute(f"INSERT INTO {table} SELECT {names} FROM ({select})")
    return count(con, table)


def count(con: duckdb.DuckDBPyConnection, table: str, where: str = "") -> int:
    return int(con.execute(f"SELECT count(*) FROM {table}" + (f" WHERE {where}" if where else "")).fetchone()[0])


def temp_rows(con: duckdb.DuckDBPyConnection, name: str, schema: list[tuple[str, str]], rows: list[dict]) -> None:
    """A TEMP table ``name`` of the build connection (never stored in the file) holding ``rows``."""
    cols = ", ".join(f'"{c}" {t}' for c, t in schema)
    con.execute(f"CREATE OR REPLACE TEMP TABLE {name} ({cols})")
    if not rows:
        return
    import pandas as pd
    frame = pd.DataFrame([{c: r.get(c) for c, _ in schema} for r in rows], columns=[c for c, _ in schema])
    con.register("_temp_rows_frame", frame)
    try:
        casts = ", ".join(f'CAST("{c}" AS {t})' for c, t in schema)
        con.execute(f"INSERT INTO {name} SELECT {casts} FROM _temp_rows_frame")
    finally:
        con.unregister("_temp_rows_frame")


def symbol_map(con: duckdb.DuckDBPyConnection, symbols_sql: str, notes: list[str], label: str) -> int:
    """``_symbol_map(symbol, ticker, ticker_source)`` (TEMP) for the distinct symbols ``symbols_sql`` selects; the
    number it names. With no spine every ticker is NULL and ``notes`` says why."""
    symbols = [r[0] for r in con.execute(f"SELECT DISTINCT symbol FROM ({symbols_sql}) WHERE symbol IS NOT NULL")
               .fetchall()]
    tickers = spine_tickers()
    if tickers is None and symbols:
        notes.append(f"{label}: no spine file yet, so no symbol is named a ticker")
    named = symbol_tickers(symbols, tickers)
    temp_rows(con, "_symbol_map", [("symbol", "TEXT"), ("ticker", "TEXT"), ("ticker_source", "TEXT")],
              [{"symbol": s, "ticker": t, "ticker_source": src} for s, (t, src) in named.items()])
    return sum(1 for t, _ in named.values() if t)
