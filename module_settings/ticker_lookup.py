"""The Database tab's ticker lookup: what the served files hold for one name, and since when.

avo, 11 Nov 2026: "Eskiden bir tane sorgulayici vardi orada. Ticker'in ismini giriyordun ... o ticker sana hangi
verileri oldugunu, ne zamandan kaldigini vesaire soyluyordu."

The old drill (``inventory.ticker_detail``) read ``.store/raw.duckdb``, the old store, and only ever showed the
newest date. This one reads **only** the six served DuckDB files through :mod:`RevenueChaser.rcdata.serving`, and
every row carries both ends of the range: since when, and up to when.

Nothing here is hand-maintained per table. Each live file is asked for its own tables and columns
(``duckdb_columns()``), so a table a later build adds shows up by itself. A table is counted for the name when it
carries one of the key columns below; a table that carries none (a reference list, a calendar) is left out rather
than guessed at.
"""
from __future__ import annotations

import logging

from ..rcdata import read, serving

logger = logging.getLogger(__name__)

#: Never data about a name: the build's own bookkeeping.
META_TABLES = frozenset(serving.META_TABLES) | {serving.VERSION_TABLE, serving.WINDOW_TABLE}

#: How a row is tied to the name, best first. ``security_id`` and ``cik`` are matched against the spine's answer
#: for the ticker, so a table that never carries a symbol still reports.
KEY_COLUMNS = ("ticker", "symbol", "security_id", "cik")

#: The column whose range is the "since when", best first. A table with none of them reports rows and no range.
DATE_COLUMNS = ("session_date", "period_end", "report_date", "event_date", "filed", "snapshot_date",
                "as_of", "valid_from", "date", "day", "expiry")

#: What each served file is called on the page, in the build order the Data flow draws.
FILE_TITLES = {"spine": "Identity and the universe", "prices": "Prices and shares",
               "fundamentals": "Statements, filings and earnings", "ownership": "Who owns it, who is short",
               "options": "Options", "market": "Market-wide series"}


def _columns(con, table: str) -> list[str]:
    rows = con.execute("SELECT column_name FROM duckdb_columns() WHERE table_name = ? ORDER BY column_index",
                       [table]).fetchall()
    return [r[0] for r in rows]


def _tables(con) -> list[str]:
    rows = con.execute("SELECT table_name FROM duckdb_tables() ORDER BY table_name").fetchall()
    return [r[0] for r in rows if r[0] not in META_TABLES]


def _pick(columns: list[str], wanted: tuple[str, ...]) -> str | None:
    have = set(columns)
    return next((c for c in wanted if c in have), None)


def _value(key: str, ticker: str, security_id: str | None, cik: str | None) -> str | None:
    if key in ("ticker", "symbol"):
        return ticker
    if key == "security_id":
        return security_id
    return cik


def _one(con, table: str, key: str, value: str, date_column: str | None) -> dict:
    """One table's answer for the name: how many rows, and the first and last day they cover."""
    what = (f'count(*), min("{date_column}"), max("{date_column}")' if date_column else "count(*), NULL, NULL")
    rows, first, last = con.execute(f'SELECT {what} FROM "{table}" WHERE "{key}" = ?', [value]).fetchone()
    return {"table": table, "rows": int(rows or 0), "matched_on": key, "date_column": date_column,
            "first": None if first is None else str(first)[:10], "last": None if last is None else str(last)[:10]}


def _scan(kind: str, ticker: str, security_id: str | None,
          cik: str | None) -> tuple[list[dict], str | None, bool]:
    """Every table of one served file that can speak about the name, its note, and whether the read **failed**.

    A file that is not built yet is not a failure: it is a line on its own group and nothing more, or an unknown
    name would read as "could not tell" on a tree where half the files have never been built.
    """
    try:
        con = serving.open_read(kind)
    except serving.ServingFileMissing:
        return [], f"no {kind}.duckdb yet", False
    except Exception as exc:  # noqa: BLE001 - one unreadable file must not take the lookup down
        logger.warning("ticker lookup could not open %s: %s", kind, exc)
        return [], f"{type(exc).__name__}: {exc}"[:200], True
    out: list[dict] = []
    error, hard = None, False
    try:
        con.execute("SET python_enable_replacements = false")
        for table in _tables(con):
            columns = _columns(con, table)
            key = _pick(columns, KEY_COLUMNS)
            if not key:
                continue
            value = _value(key, ticker, security_id, cik)
            if not value:
                continue
            try:
                out.append(_one(con, table, key, value, _pick(columns, DATE_COLUMNS)))
            except Exception as exc:  # noqa: BLE001 - a table that cannot be read is said, by name
                logger.warning("ticker lookup could not read %s.%s: %s", kind, table, exc)
                out.append({"table": table, "rows": None, "matched_on": key, "date_column": None,
                            "first": None, "last": None, "error": f"{type(exc).__name__}: {exc}"[:200]})
    except Exception as exc:  # noqa: BLE001
        logger.warning("ticker lookup failed inside %s: %s", kind, exc)
        error, hard = f"{type(exc).__name__}: {exc}"[:200], True
    finally:
        con.close()
    return out, error, hard


def _identity(ticker: str) -> tuple[dict, str | None]:
    try:
        rows = read.identity([ticker])
    except Exception as exc:  # noqa: BLE001 - no spine is "nothing known", not a 500
        logger.warning("ticker lookup could not read the spine: %s", exc)
        return {}, f"{type(exc).__name__}: {exc}"[:200]
    return (rows[0] if rows else {}), None


def _security_id(ticker: str) -> str | None:
    try:
        return read.security_ids([ticker]).get(ticker)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ticker lookup could not resolve the security id: %s", exc)
        return None


def lookup(ticker: str) -> dict | None:
    """What the served files hold for ``ticker``, by file, each table with its row count and date range.

    ``None`` when no served file holds a row for the name and the spine does not know it, so the route answers 404.
    A file that is missing or unreadable is a named line in ``errors``; it never raises and never reads the old store.
    """
    t = (ticker or "").strip().upper()
    if not t:
        return None
    row, spine_error = _identity(t)
    cik = str(row.get("cik")) if row.get("cik") else None
    security_id = _security_id(t)

    files, errors = [], ([spine_error] if spine_error else [])
    for kind in serving.KINDS:
        tables, error, hard = _scan(kind, t, security_id, cik)
        if hard:
            errors.append(f"{kind}: {error}")
        held = [x for x in tables if x.get("rows") or x.get("error")]
        files.append({"kind": kind, "name": f"{kind}.duckdb", "title": FILE_TITLES.get(kind, kind),
                      "tables": sorted(held, key=lambda x: x["table"]),
                      "looked_at": len(tables), "error": error})
    if not row and not any(f["tables"] for f in files) and not errors:
        return None
    return {"ticker": t, "name": row.get("name") or t, "security_id": security_id, "cik": cik,
            "figi": row.get("figi"), "cusip": row.get("cusip"), "isin": row.get("isin"),
            "sector": row.get("sector"), "industry": row.get("industry"), "sic": row.get("sic"),
            "files": files, "errors": errors}
