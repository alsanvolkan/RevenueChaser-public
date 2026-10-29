"""Fixture serving files for the options boards (G9): ``options.duckdb`` written straight into the test's
``MARKET_DIR`` (``conftest.isolate_ops`` gives every test its own), with the builder's own schemas
(``rcdata/build/options/tables.py``). A column a row leaves out is NULL."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb

from RevenueChaser.core import config
from RevenueChaser.core.options import parse_option_symbol
from RevenueChaser.rcdata.build.options.tables import TABLES


def write_options(market=None, **tables: list[dict]) -> None:
    """Create each named table in ``options.duckdb`` of ``market`` (the test's ``MARKET_DIR`` by default; an e2e test
    passes its dashboard's) once, and insert its rows."""
    market = Path(market) if market is not None else config.MARKET_DIR
    market.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(market / "options.duckdb"))
    try:
        for name, rows in tables.items():
            schema = TABLES[name].schema
            con.execute(f"CREATE TABLE IF NOT EXISTS {name} ({', '.join(f'{c} {t}' for c, t in schema)})")
            columns = [c for c, _ in schema]
            for row in rows:
                con.execute(f"INSERT INTO {name} VALUES ({', '.join('?' * len(columns))})",
                            [row.get(c) for c in columns])
    finally:
        con.close()


def eod_rows(ticker: str, session: date, uw_rows: list[dict]) -> list[dict]:
    """``option_eod`` rows of one session from UW ``/option-contracts`` shaped rows (the old OIPD fixtures)."""
    out = []
    for r in uw_rows:
        sym = parse_option_symbol(r["option_symbol"])
        expiry = date.fromisoformat(sym["expiry"])
        out.append({"session_date": session, "ticker": ticker, "option_symbol": r["option_symbol"],
                    "expiry": expiry, "strike": sym["strike"], "option_type": "call" if sym["type"] == "C" else "put",
                    "days_to_expiry": (expiry - session).days, "nbbo_bid": r.get("nbbo_bid"),
                    "nbbo_ask": r.get("nbbo_ask"), "last_price": r.get("last_price"),
                    "implied_volatility": r.get("implied_volatility"), "delta": r.get("delta"),
                    "open_interest": r.get("open_interest"), "volume": r.get("volume"),
                    "last_tape_time": r.get("last_tape_time"), "source": "uw/option_list",
                    "source_as_of": session, "fetched_at": 1.0, "run_id": "fixture", "observed": True})
    return out


def daily_row(ticker: str, session: date, close: float | None, **kw) -> dict:
    return {"session_date": session, "ticker": ticker, "close": close, "source": "uw/option_list",
            "source_as_of": session, "fetched_at": 1.0, "run_id": "fixture", "observed": True, **kw}
