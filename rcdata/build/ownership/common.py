"""What the ownership datasets share: symbols named by the spine, Parquet loaded into a served table, and the
comparison with the live file.

The spine is read from its live file (``spine.duckdb``), read-only and closed again; a build with no spine names no
ticker and says so in its notes, it never guesses one from the symbol.

**The build on the live copy (#446).** Every source table is keyed by the partition its rows come from (``date=``,
``half=``, ``day=``), and each row carries that key as a column (``settlement_date``, ``half``, ``index_day``).
:func:`build_keyed` builds such a table whole from every partition on a whole build; on a build over a copy of the
live file it deletes the rows of the dirty keys (a changed, new or gone partition, and a key the file serves whose
partition is gone) and loads only those partitions. Its tickers come from the live spine, which changes without a
partition changing, so it then names every row again from a map over the table's own distinct symbols and rewrites
the rows whose name moved (:func:`restate`). A map is per symbol (``symbol_tickers``, ``issuer_tickers``,
``cusip_issuer_map``), so the result is the whole build's, row for row. A new source of the kind (N-PORT, ETF review
D2) uses the same two calls: a ``dirty`` of :func:`keyed_dirty` over its partition field and a ``build`` of
:func:`build_keyed`.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable, NamedTuple

import duckdb

from ....core import calendar
from ....core.identity.sources import norm_ticker
from ... import serving
from .. import base, dirty
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


def quarter_end(col: str) -> str:
    """SQL: ``col`` (a DATE) moved to the nearest calendar quarter end: the quarter the day falls in, or the one
    before when the day is within 15 days after its end (so 10-01 goes to 09-30, not 12-31; review S1 of #300).
    A 13F report period is a quarter end, but a filer writes 09-29 or 10-01 for the September quarter now and
    then; read as written it became a quarter of its own with one filer (13F review B2, 2026-10-30)."""
    return f"CAST(date_trunc('quarter', {col} - INTERVAL 15 DAY) + INTERVAL 3 MONTH - INTERVAL 1 DAY AS DATE)"


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


# ── Sessions ───────────────────────────────────────────────────────────────────────────────────────────────────

def is_session(d: date) -> bool:
    """An NYSE session (``core/calendar.py``); outside its 1993-2028 table, a weekday (a list that does not exist
    on a holiday answers 404 and is marked absent, never invented)."""
    try:
        return calendar.is_trading_day(d)
    except ValueError:
        return d.weekday() < 5


def sessions(start: date, end: date) -> list[date]:
    """Every session from ``start`` to ``end`` inclusive, oldest first."""
    out, d = [], start
    while d <= end:
        if is_session(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def session_before(d: date, n: int = 1) -> date:
    """The ``n``-th session before ``d`` (``d`` itself not counted)."""
    for _ in range(n):
        d -= timedelta(days=1)
        while not is_session(d):
            d -= timedelta(days=1)
    return d


# ── Issuers named by CIK and symbol (Form 4, Schedule 13D/G) ───────────────────────────────────────────────────

ISSUER_CIK = "spine:cik"


def spine_ciks() -> dict[str, str | None] | None:
    """``{ticker: cik}`` of the live spine's ``security`` table; None when there is no spine file yet."""
    try:
        con = serving.open_read(SPINE)
    except serving.ServingFileMissing:
        return None
    try:
        return {t: c for t, c in con.execute("SELECT ticker, cik FROM security").fetchall()}
    finally:
        con.close()


def issuer_tickers(pairs: Iterable[tuple[str | None, str | None]], spine: dict[str, str | None] | None
                   ) -> dict[tuple, tuple[str | None, str | None]]:
    """``{(issuer cik, symbol): (ticker, ticker_source)}``: the symbol's ticker (exact or squashed) when the spine's
    CIK for it is the issuer's (or the filing names no CIK), else the issuer CIK's one ticker in the spine, else
    none. A symbol that belongs to another issuer is never taken: filers mistype symbols, CIKs they do not."""
    if not spine:
        return {p: (None, None) for p in pairs}
    by_cik: dict[str, list[str]] = {}
    for t, c in spine.items():
        if c:
            by_cik.setdefault(c, []).append(t)
    pairs = list(pairs)
    named = symbol_tickers({s for _, s in pairs if s}, set(spine))
    out = {}
    for cik, symbol in pairs:
        hit, src = named.get(symbol, (None, None)) if symbol else (None, None)
        if hit and (cik is None or spine.get(hit) == cik):
            out[(cik, symbol)] = (hit, src)
        elif cik and len(by_cik.get(cik, [])) == 1:
            out[(cik, symbol)] = (by_cik[cik][0], ISSUER_CIK)
        else:
            out[(cik, symbol)] = (None, None)
    return out


def issuer_map(con: duckdb.DuckDBPyConnection, pairs_sql: str, notes: list[str], label: str) -> None:
    """``_issuer_map(issuer_cik, symbol, ticker, ticker_source)`` (TEMP) for the distinct pairs ``pairs_sql``
    selects (columns ``issuer_cik``, ``symbol``); join it with ``IS NOT DISTINCT FROM``."""
    pairs = [tuple(r) for r in con.execute(f"SELECT DISTINCT issuer_cik, symbol FROM ({pairs_sql})").fetchall()]
    spine = spine_ciks()
    if spine is None and pairs:
        notes.append(f"{label}: no spine file yet, so no issuer is named a ticker")
    named = issuer_tickers(pairs, spine)
    temp_rows(con, "_issuer_map", [("issuer_cik", "TEXT"), ("symbol", "TEXT"), ("ticker", "TEXT"),
                                   ("ticker_source", "TEXT")],
              [{"issuer_cik": c, "symbol": s, "ticker": t, "ticker_source": src} for (c, s), (t, src) in named.items()])


ISSUER_CUSIP = "spine:cusip"


def spine_cusips() -> dict[str, str] | None:
    """``{cusip: ticker}`` of the live spine's current CUSIPs held by one ticker only; None with no spine file."""
    try:
        con = serving.open_read(SPINE)
    except serving.ServingFileMissing:
        return None
    try:
        rows = con.execute("SELECT cusip, min(ticker) FROM security WHERE cusip IS NOT NULL GROUP BY cusip "
                           "HAVING count(*) = 1").fetchall()
        return {c: t for c, t in rows}
    finally:
        con.close()


def cusip_issuer_map(con: duckdb.DuckDBPyConnection, pairs_sql: str, notes: list[str], label: str) -> None:
    """``_cusip_issuer_map(issuer_cik, cusip, ticker, ticker_source)`` (TEMP): the ticker whose current CUSIP the
    filing names (it tells a company's share classes apart), else the issuer CIK's one ticker."""
    pairs = [tuple(r) for r in con.execute(f"SELECT DISTINCT issuer_cik, cusip FROM ({pairs_sql})").fetchall()]
    by_cusip, spine = spine_cusips(), spine_ciks()
    if spine is None and pairs:
        notes.append(f"{label}: no spine file yet, so no issuer is named a ticker")
    by_cik = issuer_tickers([(c, None) for c, _ in pairs], spine)
    rows = []
    for cik, cusip in pairs:
        hit = (by_cusip or {}).get(cusip) if cusip else None
        ticker, src = (hit, ISSUER_CUSIP) if hit else by_cik.get((cik, None), (None, None))
        rows.append({"issuer_cik": cik, "cusip": cusip, "ticker": ticker, "ticker_source": src})
    temp_rows(con, "_cusip_issuer_map", [("issuer_cik", "TEXT"), ("cusip", "TEXT"), ("ticker", "TEXT"),
                                         ("ticker_source", "TEXT")], rows)


# ── The build on the live copy (#446) ─────────────────────────────────────────────────────────────────────────

class Naming(NamedTuple):
    """How a source's rows get their ticker: the columns the map is keyed by, the function that writes it (TEMP,
    over a query selecting those columns) and the TEMP table's name."""
    cols: tuple[str, ...]
    make: Callable[[duckdb.DuckDBPyConnection, str, list, str], object]
    temp: str


BY_SYMBOL = Naming(("symbol",), symbol_map, "_symbol_map")
BY_ISSUER = Naming(("issuer_cik", "symbol"), issuer_map, "_issuer_map")
BY_CUSIP = Naming(("issuer_cik", "cusip"), cusip_issuer_map, "_cusip_issuer_map")


def keyed_dirty(ctx, ds, field: str) -> dirty.Dirty:
    """A source's dirty set: the ``<field>=<key>`` values of its changed partitions (:func:`dirty.keyed`)."""
    return dirty.keyed(ctx, ds, field)


def has_table(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(con.execute("SELECT count(*) FROM duckdb_tables() WHERE database_name = current_database() "
                            "AND table_name = ?", (table,)).fetchone()[0])


def _on(naming: Naming, left: str, right: str) -> str:
    return " AND ".join(f'{right}."{c}" IS NOT DISTINCT FROM {left}."{c}"' for c in naming.cols)


def build_keyed(ctx, con: duckdb.DuckDBPyConnection, name: str, table: str, schema: list[tuple[str, str]],
                parts: list[tuple[str, Path]], key_col: str, order: str, naming: Naming | None = None) -> int:
    """``table`` of source ``name`` from its partitions ``parts`` (``[(key, file)]``, ``spine.stage.partitions``),
    each row's ``key_col`` its partition's key; its row count. ``order`` (unprefixed columns) orders a load,
    ``naming`` names the rows' tickers (None: the table carries none).

    Whole (no plan, or no such table in the file): every partition. On the live copy: the rows of the dirty keys
    are replaced by their partitions' rows, then every row is named again (:func:`restate`)."""
    plan = getattr(ctx, "incremental", None)
    if plan is None or not has_table(con, table):
        files = [f for _, f in parts]
        if not files:
            return create_table(con, table, schema)
        src = files_sql(files)
        if naming is None:
            select = f"SELECT p.*, '{name}' AS source FROM {src} p"
        else:
            naming.make(con, f"SELECT {', '.join(naming.cols)} FROM {src}", ctx.notes, name)
            select = (f"SELECT p.*, m.ticker, m.ticker_source, '{name}' AS source FROM {src} p "
                      f"LEFT JOIN {naming.temp} m ON {_on(naming, 'p', 'm')}")
        return create_table(con, table, schema, f"SELECT * FROM ({select}) ORDER BY {order}")
    d = plan.dirty.get(name, dirty.ALL)
    key = f'CAST("{key_col}" AS TEXT)'
    if d.all:
        con.execute(f"DELETE FROM {table}")
        files = [f for _, f in parts]
        keys = None
    else:
        held = {k for k, _ in parts}
        served = {r[0] for r in con.execute(f"SELECT DISTINCT {key} FROM {table} WHERE {key} IS NOT NULL").fetchall()}
        # A key the file serves whose partition is gone: its rows go (a live file built before the manifest
        # cannot show a gone partition, ``dirty.changes``).
        keys = sorted(set(d.keys) | (served - held))
        if keys:
            con.execute(f"DELETE FROM {table} WHERE {key} IN (SELECT unnest(?::TEXT[]))", (keys,))
        files = [f for k, f in parts if k in set(keys)]
    if files:
        src = files_sql(files)
        if keys is not None:
            stray = con.execute(f"SELECT count(*) FROM {src} WHERE {key} IS NULL OR {key} NOT IN "
                                f"(SELECT unnest(?::TEXT[]))", (keys,)).fetchone()[0]
            if stray:
                raise base.SourceError(f"{name}: {stray} rows of a partition carry another {key_col}; a build on the "
                                       f"live copy cannot replace them (build whole: {dirty.WHOLE_ENV}=1)")
        tick = "NULL AS ticker, NULL AS ticker_source, " if naming is not None else ""
        names = ", ".join(f'"{c}"' for c, _ in schema)
        con.execute(f"INSERT INTO {table} SELECT {names} FROM (SELECT p.*, {tick}'{name}' AS source FROM {src} p) "
                    f"ORDER BY {order}")
    if naming is not None:
        restate(con, table, naming, ctx.notes, name)
    return count(con, table)


def restate(con: duckdb.DuckDBPyConnection, table: str, naming: Naming, notes: list[str], label: str) -> int:
    """Name every row of ``table`` from the live spine again: the map over its distinct keys, and the rows whose
    ticker or ticker source differs from it rewritten; how many were."""
    naming.make(con, f"SELECT DISTINCT {', '.join(naming.cols)} FROM {table}", notes, label)
    moved = con.execute(f"""UPDATE {table} t SET ticker = m.ticker, ticker_source = m.ticker_source
        FROM {naming.temp} m WHERE {_on(naming, 't', 'm')}
          AND (t.ticker IS DISTINCT FROM m.ticker OR t.ticker_source IS DISTINCT FROM m.ticker_source)""").fetchone()
    n = int(moved[0]) if moved else 0
    if n:
        notes.append(f"{label}: {n} rows named again from the spine")
    return n
