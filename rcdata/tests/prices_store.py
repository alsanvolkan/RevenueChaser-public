"""A served ``prices.duckdb`` written straight from rows, for the tests of the boards that read it (G3: PR 8, 9, 11).

The builder makes the live file from Alpaca's raw answers (``rcdata/build/prices``); a board test only needs the
served tables. These helpers keep the bars and the actions in the live file itself and rebuild the derived tables
(``adj_factor`` with the builder's own ``ADJ_SQL``, ``security_symbol`` with its ``SYMBOL_SQL``) after every change,
so a test can add, replace and drop bars the way the old D01 seed helpers did.

A bar is **as traded** (Alpaca's ``adjustment=raw``): a split divides every bar before its ex-date on read, whenever
the bar was fetched. The security id of a ticker is ``sym:<TICKER>`` unless a row names one.
"""
from __future__ import annotations

import time
from datetime import date, datetime

import duckdb

from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build.prices import serving as prices_serving

TABLES = ("price_bar_daily", "corporate_action", "adj_factor", "shares_class", "security_symbol",
          "market_cap_daily", "price_revision", "wrds_price_diff")


def _day(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _sid(row: dict) -> str:
    return row.get("security_id") or f"sym:{str(row['ticker']).strip().upper()}"


def _connect() -> duckdb.DuckDBPyConnection:
    path = serving.live_path("prices")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    have = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if serving.VERSION_TABLE not in have:
        con.execute(f"CREATE TABLE {serving.VERSION_TABLE} (kind TEXT, version INTEGER, built_at DOUBLE, "
                    "build_id TEXT)")
        con.execute(f"INSERT INTO {serving.VERSION_TABLE} VALUES ('prices', ?, ?, 'test')",
                    [serving.SERVING_SCHEMA_VERSION["prices"], time.time()])
    for table in TABLES:
        if table not in have:
            cols = ", ".join(f'"{c}" {t}' for c, t in prices_serving.SCHEMAS[table])
            con.execute(f"CREATE TABLE {table} ({cols})")
    return con


def put_bars(rows: list[dict], *, replace: bool = True) -> int:
    """``rows`` (``ticker``, ``session_date`` or ``ts``, ``close``, optional ``open``/``high``/``low``/``volume``,
    ``fetched_at``, ``source``, ``security_id``, ``symbol``) as served bars. With ``replace`` each ticker's stored
    bars go first (a re-fetched series); the first row of a (security, session) wins."""
    if not rows:
        return 0
    con = _connect()
    try:
        if replace:
            sids = sorted({_sid(r) for r in rows})
            con.execute(f"DELETE FROM price_bar_daily WHERE security_id IN ({', '.join('?' * len(sids))})", sids)
        seen = {(r[0], r[1]) for r in con.execute("SELECT security_id, session_date FROM price_bar_daily").fetchall()}
        out = []
        for r in rows:
            sid, day = _sid(r), _day(r.get("session_date") or r.get("ts"))
            if (sid, day) in seen:
                continue
            seen.add((sid, day))
            close = r.get("close")
            at = r.get("fetched_at", r.get("cached_at"))
            out.append((sid, day, r.get("symbol") or str(r["ticker"]).strip().upper(), r.get("cik"),
                        r.get("open", close), r.get("high", close), r.get("low", close), close,
                        r.get("volume", 1.0), None, None, None, r.get("source", "alpaca/bars"), day,
                        time.time() if at is None else float(at), "test"))
        con.executemany(f"INSERT INTO price_bar_daily VALUES ({', '.join('?' * 16)})", out)
        _derive(con)
        return len(out)
    finally:
        con.close()


def put_action(ticker: str, ex_date, action: str, value: float, *, fetched_at: float | None = None,
               security_id: str | None = None, applied: bool = True) -> None:
    """One ``corporate_action`` row: ``split`` (``value`` = new over old shares, 2.0 for a 2-for-1) or ``dividend``
    (``value`` = the cash amount a share). A dividend takes the close of the last bar before its ex-date, as the
    builder does, so put the bars first."""
    sid = security_id or f"sym:{str(ticker).strip().upper()}"
    ex = _day(ex_date)
    kind = "cash_dividend" if action in ("dividend", "cash_dividend", "capital_gain") else "split"
    con = _connect()
    try:
        before = con.execute("SELECT close FROM price_bar_daily WHERE security_id = ? AND session_date < ? "
                             "ORDER BY session_date DESC LIMIT 1", [sid, ex]).fetchone()
        con.execute(f"INSERT INTO corporate_action VALUES ({', '.join('?' * 20)})", [
            sid, ex, kind, "cash_dividend" if kind == "cash_dividend" else "forward_split",
            None if kind == "cash_dividend" else float(value), float(value) if kind == "cash_dividend" else None,
            "USD", False, applied, None, str(ticker).upper(), None, None, None, None, "alpaca/corporate_actions",
            f"test-{sid}-{ex}-{kind}", ex, time.time() if fetched_at is None else float(fetched_at), "test"])
        _derive(con)
    finally:
        con.close()


def _derive(con: duckdb.DuckDBPyConnection) -> None:
    """``adj_factor`` and ``security_symbol`` again from the bars and actions the file now holds (a dividend takes
    the close of the last bar before its ex-date, the builder's ``close_before``, which the served table does not keep)."""
    con.execute("CREATE OR REPLACE TEMP TABLE stg_spine (ticker TEXT, cik TEXT, tier TEXT, security_id TEXT)")
    con.execute("""CREATE OR REPLACE TEMP TABLE _act_final AS
        SELECT a.*, p.close AS close_before FROM corporate_action a ASOF LEFT JOIN price_bar_daily p
            ON p.security_id = a.security_id AND a.ex_date > p.session_date""")
    for table, sql in (("adj_factor", prices_serving.ADJ_SQL), ("security_symbol", prices_serving.SYMBOL_SQL)):
        names = ", ".join(f'"{c}"' for c, _ in prices_serving.SCHEMAS[table])
        con.execute(f"DELETE FROM {table}")
        con.execute(f"INSERT INTO {table} ({names}) SELECT {names} FROM ({sql})")
    con.execute(f"UPDATE {serving.VERSION_TABLE} SET built_at = ?", [time.time()])
    con.execute("CHECKPOINT")


def delete_bars(ticker: str, since=None) -> None:
    """Drop ``ticker``'s bars (from session ``since`` on, every one when None)."""
    con = _connect()
    try:
        sql, params = "DELETE FROM price_bar_daily WHERE security_id = ?", [f"sym:{ticker.strip().upper()}"]
        if since is not None:
            sql += " AND session_date >= ?"
            params.append(_day(since))
        con.execute(sql, params)
        _derive(con)
    finally:
        con.close()


def clear() -> None:
    con = _connect()
    try:
        con.execute("DELETE FROM price_bar_daily")
        con.execute("DELETE FROM corporate_action")
        _derive(con)
    finally:
        con.close()
