"""Test helpers for the forecast ledger's derived writers (``core/outcomes.py``, ``core/baselines.py``), PR 10 (G4).

The builder hands each writer its build file of the ledger kind (``ledger.duckdb``); a test hands it an in-memory
connection with the same tables (``connect``), puts ledger rows in it the way ``ledger/freeze`` would (``freeze``,
``insert_ledger``) and seeds the prices store the writers read their bars from (``seed_bars``, which takes the old
D01-shaped rows and ``price_actions``-shaped actions, so the rules' fixtures stay as they were).
"""
from __future__ import annotations

from datetime import date

import duckdb

from RevenueChaser.rcdata.build import ledger as kind
from RevenueChaser.rcdata.tests import prices_seed


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    kind.create_table(con, kind.LEDGER, kind.LEDGER_SCHEMA, kind.LEDGER_KEY)
    kind.create_table(con, kind.OUTCOMES, kind.OUTCOMES_SCHEMA, kind.OUTCOMES_KEY)
    kind.create_table(con, kind.BASELINES, kind.BASELINES_SCHEMA, kind.BASELINES_KEY)
    return con


def query(con, sql: str, params: list | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def count(con, table: str) -> int:
    return int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


class Bars:
    """The prices store as the old tests wrote D01: a row replaces its day's bar, and a ticker's actions accumulate
    into its factors (a split recorded later is a later call)."""

    def __init__(self):
        self.actions: dict[str, list] = {}

    def seed(self, rows, actions=()) -> None:
        by_ticker: dict[str, list[dict]] = {}
        for r in rows:
            by_ticker.setdefault(r["ticker"], []).append(r)
        for t, got in by_ticker.items():
            prices_seed.upsert_bars(t, got, write=False)
        prices_seed.flush()
        for a in actions:
            self.actions.setdefault(a["ticker"], []).append((a["ex_date"], a["action"], a["value"]))
        for t in {a["ticker"] for a in actions}:
            prices_seed.set_actions(t, self.actions[t])


def ledger_row(subject, rank, *, source: str = "swing", day: date, shown_price=None, **over) -> dict:
    row = {"source": source, "forecast_date": day, "subject": subject, "target_type": "pick", "target_date": day,
           "frozen_at": 1.0, "run_id": "freeze1", "model_version": "v1+abcd", "rank": rank, "score": None,
           "bucket": None, "probs": None, "market_cap": None, "sector": None, "shown_price": shown_price,
           "stop": None, "target": None, "price_session": None}
    row.update(over)
    return row


def insert_ledger(con, rows) -> None:
    cols = [c for c, _ in kind.LEDGER_SCHEMA]
    con.executemany(f"INSERT INTO {kind.LEDGER} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [[r.get(c) for c in cols] for r in rows])


def freeze(con, subjects, *, source: str = "swing", day: date, shown_price=None) -> None:
    """The rows a board's freeze puts in the ledger (``ledger.write_picks`` through ``ledger/freeze``)."""
    insert_ledger(con, [ledger_row(s, i + 1, source=source, day=day, shown_price=shown_price)
                        for i, s in enumerate(subjects)])
