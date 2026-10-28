"""The tables of ``options.duckdb`` (serving kind ``options``, version 1): their columns, keys, Parquet sources and
windows, the Parquet partitions the datasets write, and the one build of a table from them.

Every table is built from the Parquet partitions of its ``sources`` (``parquet/<source>/<table>/<part>/part-0.parquet``,
``<part>`` being ``date=<session>`` for the UW datasets and ``ticker=<ticker>`` for the IBKR year), deduplicated on its
key (the newest ``fetched_at`` wins), under a primary key. ``option_eod`` serves its last ``EOD_SESSIONS`` sessions
(the Parquet keeps every one) and says so in the build file (``serving.declare_window``), so a session leaving the
window is not taken for a shrink. A table whose Parquet never ran is built empty, never missing.

The long rows of the earnings set (per strike, per expiry, its prints) share the tables of the list names and are
told apart by ``set_name`` (``list`` or ``earnings``), which is part of their key: a name can be in both sets on one
evening (added to the list after its earnings-set fetch) without a key colliding.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from ....core.config import to_float
from ... import serving
from .. import base

LIST_SOURCE = "uw/option_list"
SCREENER_SOURCE = "uw/option_screener"
IBKR_SOURCE = "ibkr/iv_history"
SET_LIST, SET_EARNINGS = "list", "earnings"
# option_eod serves this many sessions (the plan's "last 30 sessions"); its Parquet keeps them all.
EOD_SESSIONS = 30

PROVENANCE = [("source", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT"),
              ("observed", "BOOLEAN")]

# ── The UW fields each table keeps, under UW's own names (docs/openapi.yaml) ──
# /stock/{t}/option-contracts: one row per contract.
CONTRACT_DOUBLES = ("nbbo_bid", "nbbo_ask", "last_price", "high_price", "low_price", "avg_price", "total_premium",
                    "implied_volatility", "delta", "gamma", "theta", "vega", "rho")
CONTRACT_COUNTS = ("open_interest", "prev_oi", "volume", "ask_volume", "bid_volume", "mid_volume", "no_side_volume",
                   "sweep_volume", "multi_leg_volume", "stock_multi_leg_volume", "floor_volume", "cross_volume")
# /stock/{t}/spot-exposures/strike: the four greeks for calls and puts by open interest, volume, bid and ask.
SPOT_FIELDS = tuple(f"{side}_{greek}_{basis}" for greek in ("gamma", "delta", "charm", "vanna")
                    for side in ("call", "put") for basis in ("oi", "vol", "bid", "ask"))
# /stock/{t}/greek-exposure/strike and /expiry: the exposure of each greek, calls and puts.
EXPOSURE_FIELDS = tuple(f"{side}_{greek}" for greek in ("gex", "delta", "charm", "vanna") for side in ("call", "put"))
# /stock/{t}/flow-per-expiry: the day's flow per expiry.
FLOW_EXPIRY_DOUBLES = tuple(f"{side}_{f}" for side in ("call", "put")
                            for f in ("premium", "premium_ask_side", "premium_bid_side", "otm_premium"))
FLOW_EXPIRY_COUNTS = tuple(f"{side}_{f}" for side in ("call", "put")
                           for f in ("trades", "volume", "volume_ask_side", "volume_bid_side", "otm_trades",
                                     "otm_volume"))
# /stock/{t}/volatility/term-structure: one row per expiry.
TERM_FIELDS = ("volatility", "implied_move", "implied_move_perc")
# /stock/{t}/gex-levels: four strike levels.
GEX_LEVEL_FIELDS = ("call_wall", "gamma_flip", "gamma_magnet", "put_wall")
# /stock/{t}/options-volume: the day's option volume, premium and open interest.
VOLUME_DOUBLES = ("call_premium", "put_premium", "bullish_premium", "bearish_premium", "net_call_premium",
                  "net_put_premium", "avg_3_day_call_volume", "avg_3_day_put_volume", "avg_7_day_call_volume",
                  "avg_7_day_put_volume", "avg_30_day_call_volume", "avg_30_day_put_volume")
VOLUME_COUNTS = ("call_volume", "put_volume", "call_open_interest", "put_open_interest", "call_volume_ask_side",
                 "call_volume_bid_side", "put_volume_ask_side", "put_volume_bid_side")
# /stock/{t}/flow-recent: the prints (UW's "Option Trade").
PRINT_DOUBLES = ("price", "premium", "implied_volatility", "delta", "gamma", "theta", "vega", "rho", "theo",
                 "nbbo_bid", "nbbo_ask", "ewma_nbbo_bid", "ewma_nbbo_ask", "underlying_price")
PRINT_COUNTS = ("size", "volume", "open_interest", "multi_vol", "stock_multi_vol", "ask_vol", "bid_vol", "mid_vol",
                "no_side_vol")
# /screener/stocks: the screener row of a reporting name.
SCREENER_DOUBLES = ("call_volume", "put_volume", "call_open_interest", "put_open_interest", "total_open_interest",
                    "call_premium", "put_premium", "net_call_premium", "net_put_premium", "bullish_premium",
                    "bearish_premium", "call_volume_ask_side", "call_volume_bid_side", "put_volume_ask_side",
                    "put_volume_bid_side", "prev_call_oi", "prev_put_oi", "avg_3_day_call_volume",
                    "avg_3_day_put_volume", "avg_7_day_call_volume", "avg_7_day_put_volume",
                    "avg_30_day_call_volume", "avg_30_day_put_volume", "put_call_ratio", "relative_volume", "close",
                    "prev_close", "week_52_high", "week_52_low", "marketcap", "implied_move", "implied_move_perc",
                    "iv30d", "iv30d_1d", "iv30d_1w", "iv30d_1m", "iv_rank", "volatility")
SCREENER_TEXTS = ("er_time", "issue_type", "sector")


def _doubles(names) -> list[tuple[str, str]]:
    return [(n, "DOUBLE") for n in names]


def _counts(names) -> list[tuple[str, str]]:
    return [(n, "BIGINT") for n in names]


@dataclass(frozen=True)
class Table:
    name: str
    schema: list[tuple[str, str]]
    key: tuple[str, ...]
    sources: tuple[str, ...]          # the Parquet folders (under parquet/) whose partitions it unions
    window: int | None = None         # sessions served (newest first); None serves every one

    @property
    def columns(self) -> list[str]:
        return [c for c, _ in self.schema]


OPTION_EOD = Table(
    "option_eod",
    [("session_date", "DATE"), ("ticker", "TEXT"), ("option_symbol", "TEXT"), ("expiry", "DATE"),
     ("strike", "DOUBLE"), ("option_type", "TEXT"), ("days_to_expiry", "INTEGER")]
    + _doubles(CONTRACT_DOUBLES) + _counts(CONTRACT_COUNTS)
    + [("oi_change", "BIGINT"), ("charm", "DOUBLE"), ("vanna", "DOUBLE"), ("greeks_iv", "DOUBLE"),
       ("last_tape_time", "TEXT")] + PROVENANCE,
    ("session_date", "option_symbol"), (LIST_SOURCE,), window=EOD_SESSIONS)

OPTION_STRIKE = Table(
    "option_strike",
    [("session_date", "DATE"), ("ticker", "TEXT"), ("set_name", "TEXT"), ("strike", "DOUBLE"), ("price", "DOUBLE")]
    + _doubles(SPOT_FIELDS) + _doubles(EXPOSURE_FIELDS) + [("snapshot_time", "TEXT")] + PROVENANCE,
    ("session_date", "ticker", "set_name", "strike"), (LIST_SOURCE, SCREENER_SOURCE))

OPTION_EXPIRY = Table(
    "option_expiry",
    [("session_date", "DATE"), ("ticker", "TEXT"), ("set_name", "TEXT"), ("expiry", "DATE"),
     ("days_to_expiry", "INTEGER"), ("chains", "BIGINT"), ("open_interest", "BIGINT"), ("volume", "BIGINT")]
    + _doubles(EXPOSURE_FIELDS) + _doubles(FLOW_EXPIRY_DOUBLES) + _counts(FLOW_EXPIRY_COUNTS)
    + _doubles(TERM_FIELDS) + PROVENANCE,
    ("session_date", "ticker", "set_name", "expiry"), (LIST_SOURCE, SCREENER_SOURCE))

OPTION_DAILY = Table(
    "option_daily",
    [("session_date", "DATE"), ("ticker", "TEXT"), ("expiries", "INTEGER"), ("contracts", "INTEGER")]
    + _doubles(GEX_LEVEL_FIELDS) + _doubles(VOLUME_DOUBLES) + _counts(VOLUME_COUNTS)
    + [("iv_rank_1y", "DOUBLE"), ("iv_rank_volatility", "DOUBLE"), ("close", "DOUBLE")] + PROVENANCE,
    ("session_date", "ticker"), (LIST_SOURCE,))

OPTION_PRINT = Table(
    "option_print",
    [("session_date", "DATE"), ("ticker", "TEXT"), ("set_name", "TEXT"), ("print_id", "TEXT"),
     ("executed_at", "TEXT"), ("option_symbol", "TEXT"), ("expiry", "DATE"), ("strike", "DOUBLE"),
     ("option_type", "TEXT")]
    + _doubles(PRINT_DOUBLES) + _counts(PRINT_COUNTS)
    + [("exchange", "TEXT"), ("tags", "VARCHAR[]"), ("report_flags", "VARCHAR[]"),
       ("upstream_condition_detail", "TEXT"), ("canceled", "BOOLEAN")] + PROVENANCE,
    ("session_date", "ticker", "set_name", "print_id"), (LIST_SOURCE, SCREENER_SOURCE))

# One row per name, session, source and tenor: UW's interpolated IV (days 1 to 365, observed by the daily run) and
# IBKR's 30-day option implied volatility (the year a name gets when it joins the list: backfilled, never observed).
IV_DAILY = Table(
    "iv_daily",
    [("session_date", "DATE"), ("ticker", "TEXT"), ("days", "INTEGER"), ("volatility", "DOUBLE"),
     ("implied_move_perc", "DOUBLE"), ("percentile", "DOUBLE")] + PROVENANCE,
    ("session_date", "ticker", "source", "days"), (LIST_SOURCE, IBKR_SOURCE))

OPTION_SCREENER_DAILY = Table(
    "option_screener_daily",
    [("session_date", "DATE"), ("ticker", "TEXT"), ("report_date", "DATE"), ("report_session", "TEXT"),
     ("screened", "BOOLEAN"), ("passes_gates", "BOOLEAN"), ("option_volume", "BIGINT"),
     ("dollar_option_volume", "DOUBLE")]
    + _doubles(SCREENER_DOUBLES)
    + [(n, "TEXT") for n in SCREENER_TEXTS]
    + [("is_index", "BOOLEAN"), ("next_earnings_date", "DATE"), ("next_dividend_date", "DATE")] + PROVENANCE,
    ("session_date", "ticker"), (SCREENER_SOURCE,))

ER_OPTIONS = Table(
    "er_options",
    [("session_date", "DATE"), ("ticker", "TEXT"), ("report_date", "DATE"), ("report_session", "TEXT"),
     ("option_volume", "BIGINT"), ("total_open_interest", "BIGINT"), ("dollar_option_volume", "DOUBLE"),
     ("strikes", "INTEGER"), ("expiries", "INTEGER"), ("prints", "INTEGER")]
    + _doubles(GEX_LEVEL_FIELDS) + _doubles(VOLUME_DOUBLES) + _counts(VOLUME_COUNTS) + PROVENANCE,
    ("session_date", "ticker"), (SCREENER_SOURCE,))

TABLES: dict[str, Table] = {t.name: t for t in (OPTION_EOD, OPTION_STRIKE, OPTION_EXPIRY, OPTION_DAILY, OPTION_PRINT,
                                                IV_DAILY, OPTION_SCREENER_DAILY, ER_OPTIONS)}

# What makes a row "changed" in a check: its data, not when or by which run it was fetched.
NOT_COMPARED = ("fetched_at", "run_id", "source_as_of")

# Not served: the names a UW dataset has typed for a session, one row each, written after the name's table rows.
# It is the dataset's coverage (names done against names expected), the size of each name's raw file (the plan's
# F5 measure), and the marker of a raw file that is typed: a raw file without its row here (a run killed between
# its fetch and its typing) is typed by the next run.
NAMES = Table(
    "names",
    [("session_date", "DATE"), ("ticker", "TEXT"), ("set_name", "TEXT"), ("raw_bytes", "BIGINT"), ("calls", "INTEGER"),
     ("errors", "TEXT"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")],
    ("session_date", "ticker", "set_name"), ())


# ── Parquet ────────────────────────────────────────────────────────────────────────────────────────────────────

def source_root(source: str) -> Path:
    return base.parquet_root() / source


def partition_path(source: str, table: str, session: date) -> Path:
    return source_root(source) / table / f"date={session.isoformat()}" / "part-0.parquet"


def ticker_partition_path(source: str, table: str, ticker: str) -> Path:
    return source_root(source) / table / f"ticker={ticker}" / "part-0.parquet"


def partitions(table: Table) -> list[Path]:
    return sorted(p for s in table.sources for p in (source_root(s) / table.name).glob("*/part-0.parquet"))


def upsert(source: str, table: Table, rows: list[dict]) -> list[Path]:
    """Merge ``rows`` into their session partitions of ``source`` (one write per session touched)."""
    by_day: dict[date, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["session_date"], []).append(r)
    return [base.upsert_partition(day_rows, table.schema, list(table.key), partition_path(source, table.name, d))
            for d, day_rows in sorted(by_day.items())]


def names_done(source: str, session: date) -> dict[str, dict]:
    """The ``NAMES`` rows of ``source`` for ``session``, by ticker."""
    part = partition_path(source, NAMES.name, session)
    if not part.exists():
        return {}
    con = duckdb.connect()
    try:
        cur = con.execute(f"SELECT * FROM read_parquet({base.sql_path(part)})")
        cols = [d[0] for d in cur.description]
        return {r["ticker"]: r for r in (dict(zip(cols, row)) for row in cur.fetchall())}
    finally:
        con.close()


# ── Building the served tables ─────────────────────────────────────────────────────────────────────────────────

def _windowed(table: Table, parts: list[Path]) -> tuple[list[Path], str | None]:
    """The partitions of the newest ``window`` sessions and the first session kept (None: no window, or no data)."""
    days = sorted({p.parent.name.removeprefix("date=") for p in parts if p.parent.name.startswith("date=")})
    if table.window is None or not days:
        return parts, None
    keep = set(days[-table.window:])
    return [p for p in parts if p.parent.name.removeprefix("date=") in keep], min(keep)


def build_tables(con: duckdb.DuckDBPyConnection, names) -> dict[str, int]:
    """Create each named table in the build file from its Parquet; ``{table: rows}``."""
    counts = {}
    for name in names:
        t = TABLES[name]
        cols = ", ".join(f'"{c}" {typ}' for c, typ in t.schema)
        con.execute(f'CREATE TABLE "{t.name}" ({cols}, PRIMARY KEY ({", ".join(t.key)}))')
        parts, first = _windowed(t, partitions(t))
        if first is not None:
            serving.declare_window(con, t.name, "session_date", first)
        if parts:
            files = ", ".join(base.sql_path(p) for p in parts)
            names_sql = ", ".join(f'"{c}"' for c in t.columns)
            keys = ", ".join(f'"{k}"' for k in t.key)
            con.execute(f'INSERT INTO "{t.name}" SELECT {names_sql} FROM read_parquet([{files}], union_by_name = true) '
                        f"QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY fetched_at DESC) = 1")
        counts[t.name] = int(con.execute(f'SELECT count(*) FROM "{t.name}"').fetchone()[0])
    return counts


def compare_by_day(con: duckdb.DuckDBPyConnection, table: Table, kind: str = "options") -> tuple[int, int]:
    """``(added, removed)`` rows of ``table`` against the live file, on its data columns. A windowed table is
    compared inside the build's window only, so the session that left it is not counted as removed."""
    cols = [c for c in table.columns if c not in NOT_COMPARED]
    first = con.execute(f'SELECT min(session_date) FROM "{table.name}"').fetchone()[0] if table.window else None
    if first is None:
        return base.compare_with_live(con, kind, table.name, cols)
    live = serving.live_path(kind)
    new_rows = con.execute(f'SELECT count(*) FROM "{table.name}"').fetchone()[0]
    if not live.exists():
        return int(new_rows), 0
    names = ", ".join(f'"{c}"' for c in cols)
    con.execute(f"ATTACH {base.sql_path(live)} AS live_cmp (READ_ONLY)")
    try:
        known = {r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'live_cmp'").fetchall()}
        if table.name not in known:
            return int(new_rows), 0
        old = f'SELECT {names} FROM live_cmp."{table.name}" WHERE session_date >= ?'
        added = con.execute(f'SELECT count(*) FROM (SELECT {names} FROM "{table.name}" EXCEPT ALL {old})',
                            [first]).fetchone()[0]
        removed = con.execute(f'SELECT count(*) FROM ({old} EXCEPT ALL SELECT {names} FROM "{table.name}")',
                              [first]).fetchone()[0]
        return int(added), int(removed)
    finally:
        con.execute("DETACH live_cmp")


# ── Typing helpers ─────────────────────────────────────────────────────────────────────────────────────────────

def to_int(value) -> int | None:
    f = to_float(value)
    return int(f) if f is not None and math.isfinite(f) else None


def to_day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value not in (None, "") else None
    except ValueError:
        return None


def to_bool(value) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return True if text in ("true", "t", "1", "yes") else False if text in ("false", "f", "0", "no") else None


def numbers(item: dict, doubles=(), counts=()) -> dict:
    """The named fields of a UW row typed: DOUBLE ones through ``to_float``, BIGINT ones through ``to_int``."""
    out = {f: to_float(item.get(f)) for f in doubles}
    out.update({f: to_int(item.get(f)) for f in counts})
    return out
