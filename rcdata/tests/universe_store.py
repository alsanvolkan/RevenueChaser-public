"""A served ``spine.duckdb`` written straight from the old ``equity_universe`` rows, for the board tests (#642 PR 6).

Since #642 PR 6 the boards' universe is the spine's (``core/universe.py``): its listed names (Tier A and B, no
``my_universe`` list here) with the spine's name, sector, industry and security type. The board tests were written
against ``equity_universe`` rows (``ticker``, ``name``, ``sector``, ``industry``, ``market_cap``, ``price``,
``quote``, ...); ``put`` and ``replace`` write those rows as spine ``security`` rows **and** into the old table,
whose ``quote`` (and, where the prices store has none, cap and price) the spine universe still reads.

A row's ``tier`` defaults to ``A``; ``security_type`` stays NULL unless the row gives one, so a test's
``security_master`` row still types the name (``universe._security_types``). It also books a good
``spine/security`` check, so ``universe.source_state`` gives no reason.
"""
from __future__ import annotations

import duckdb

from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build.spine import security

# Every table the spine readers expect, but not ``my_universe``: an empty list there would keep no name.
TABLES = ("security", "security_history", "gics_history", "index_membership")
_COLS = [c for c, _ in security.SECURITY]


def _connect() -> duckdb.DuckDBPyConnection:
    path = serving.live_path("spine")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    have = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if serving.VERSION_TABLE not in have:
        con.execute(f"CREATE TABLE {serving.VERSION_TABLE} (kind TEXT, version INTEGER, built_at DOUBLE, "
                    "build_id TEXT)")
        con.execute(f"INSERT INTO {serving.VERSION_TABLE} VALUES ('spine', ?, 1, 'test')",
                    [serving.SERVING_SCHEMA_VERSION["spine"]])
    for table in TABLES:
        if table not in have:
            cols = ", ".join(f'"{c}" {t}' for c, t in security.SCHEMAS[table])
            con.execute(f"CREATE TABLE {table} ({cols})")
    return con


def _security_row(r: dict) -> dict:
    ticker = str(r["ticker"]).strip().upper()
    return {"ticker": ticker, "cik": (str(r["cik"]) if r.get("cik") else None), "name": r.get("name") or ticker,
            "exchange": r.get("exchange"), "tier": r.get("tier") or "A", "security_type": r.get("security_type"),
            "sector": r.get("sector"), "industry": r.get("industry"), "source": "sec/company_tickers_exchange"}


def _write(rows: list[dict], *, replace: bool) -> None:
    from RevenueChaser.core import spine_reads
    con = _connect()
    try:
        if replace:
            con.execute("DELETE FROM security")
        out = [_security_row(r) for r in rows]
        for r in out:
            con.execute("DELETE FROM security WHERE ticker = ?", [r["ticker"]])
        if out:
            con.executemany(f"INSERT INTO security ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})",
                            [[r.get(c) for c in _COLS] for r in out])
        con.execute("CHECKPOINT")
    finally:
        con.close()
    spine_reads.clear()
    if ops.latest_unskipped_check("spine/security") is None:
        ops.record_check("spine/security", "changed")


def put(rows: list[dict]) -> None:
    """``equity_universe`` rows added (a ticker already there is replaced), in the spine and the old table."""
    from RevenueChaser.core import rawstore
    rawstore.write_rows("equity_universe", list(rows))
    _write(list(rows), replace=False)


def replace(rows: list[dict]) -> None:
    """``equity_universe`` rows as the whole universe, in the spine and the old table."""
    from RevenueChaser.core import rawstore
    rawstore.replace_all("equity_universe", list(rows))
    _write(list(rows), replace=True)
