"""Seed D01 (``price_bars_daily``) bars in unit tests (#383 R3).

Since C12 and K8b, Swing and Fundamentals score their picks from D01 and the freeze and the history snapshot check
the picks' D01 bars (``ledger.ohlc_gaps``). D01 is first-write-wins (``rawstore`` append), so a test that "rewrites"
a series (a later run with fresher bars) replaces the ticker's rows here; the real writer never stores a bar before
its session's close + 30 min, so the tests that stamp one earlier stage what the check must refuse.

Since #642 PR 6 the readers read only the builder's served ``prices.duckdb`` (``rcdata/tests/prices_store``), whose
``price_bars_daily`` view stamps every bar with the file's build time: a seed writes the bars there too, built at
``fetched_at`` (the store copy stays for the writer's own skip keys).
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.core import rawstore
from RevenueChaser.rcdata.tests import prices_store


def seed_d01(ticker: str, days, fetched_at: float, *, close: float = 1.5, replace: bool = True) -> None:
    """One flat bar per day in ``days`` (dates or ISO texts) for ``ticker``, stamped ``fetched_at``."""
    if replace:
        with rawstore.connect(db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw")) as conn:
            conn.execute("DELETE FROM price_bars_daily WHERE ticker = ?", [ticker])
    rows = [{
        "ticker": ticker, "session_date": d if isinstance(d, date) else date.fromisoformat(str(d)[:10]),
        "open": 1.0, "high": 2.0, "low": 0.5, "close": close, "adj_close": None, "volume": 100.0,
        "split_ratio": None, "adj_source": None, "source": "uw", "fetched_at": fetched_at, "run_id": "seed",
        "run_at": fetched_at} for d in days]
    rawstore.write_rows("price_bars_daily", rows)
    if rows:
        prices_store.put_bars(rows, replace=replace, built_at=fetched_at)
    elif replace:
        prices_store.delete_bars(ticker)


def drop_d01(ticker: str | None = None, session=None) -> None:
    """Drop D01 bars, from the store and the served file: ``ticker``'s (every ticker's when None), only ``session``'s
    when given."""
    where, params = ["true"], []
    if ticker is not None:
        where.append("ticker = ?")
        params.append(ticker)
    if session is not None:
        where.append("session_date = ?")
        params.append(session if isinstance(session, date) else date.fromisoformat(str(session)[:10]))
    with rawstore.connect(db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw")) as conn:
        conn.execute(f"DELETE FROM price_bars_daily WHERE {' AND '.join(where)}", params)
    prices_store.delete_bars(ticker, session=session)


def write_d01(rows, actions=(), *, built_at: float | None = None) -> None:
    """Store-shaped ``price_bars_daily`` (and ``price_actions``) rows, in the store and the served file; the file is
    stamped built at ``built_at``, else at the newest row's ``fetched_at`` (now when none has one)."""
    rows, actions = list(rows), list(actions)
    rawstore.write_rows("price_bars_daily", rows)
    if actions:
        rawstore.write_rows("price_actions", actions)
    if built_at is None:
        built_at = max((r["fetched_at"] for r in rows if r.get("fetched_at") is not None), default=None)
    prices_store.put_store_rows(rows, actions, built_at=built_at)
