"""Seed D01 (``price_bars_daily``) bars in the Co-movement and Relatedness tests (#379 D01 switch).

Since the D01 switch both boards read ``core.prices.daily_bars(basis="adjusted")`` instead of ``swing_ohlc``. The
tests build their markets as ``swing_ohlc``-shaped rows (``ticker``, ``ts``, ``close``, ``cached_at``); ``write_bars``
stores the same closes as D01 bars, one per (ticker, session), replacing each ticker's stored series the way the old
``swing_ohlc`` writer did (D01 itself is first-write-wins, so a test that "re-fetches" a series replaces it here).
"""
from __future__ import annotations

import time
from datetime import date

from RevenueChaser.core import rawstore

DB = rawstore.MANIFEST["price_bars_daily"].get("db", "raw")


def _session(ts) -> date:
    return date.fromisoformat(str(ts)[:10])


def _has_table(name: str) -> bool:
    rows = rawstore.query("SELECT count(*) AS n FROM information_schema.tables WHERE table_name = ?", [name], db=DB)
    return bool(rows and rows[0]["n"])


def write_bars(rows: list[dict], *, replace: bool = True) -> int:
    """``rows`` (``ticker``, ``ts`` or ``session_date``, ``close``, optional ``open``/``high``/``low``/``volume`` and
    ``cached_at`` or ``fetched_at``) as D01 bars. The first row of a (ticker, session) wins, as in D01."""
    if not rows:
        return 0
    if replace and _has_table("price_bars_daily"):          # a store no call has touched yet has no table
        tickers = sorted({r["ticker"] for r in rows})
        with rawstore.connect(db=DB) as conn:
            conn.execute(f"DELETE FROM price_bars_daily WHERE ticker IN ({', '.join('?' for _ in tickers)})",
                         tickers)
    out = []
    for r in rows:
        at = r.get("fetched_at", r.get("cached_at"))
        at = time.time() if at is None else float(at)
        close = r.get("close")
        out.append({
            "ticker": r["ticker"], "session_date": _session(r.get("session_date") or r["ts"]),
            "open": r.get("open", close), "high": r.get("high", close), "low": r.get("low", close),
            "close": close, "adj_close": None, "volume": r.get("volume", 1.0), "split_ratio": None,
            "adj_source": None, "source": "uw", "fetched_at": at, "run_id": "test", "run_at": at})
    return rawstore.write_rows("price_bars_daily", out)


def write_action(ticker: str, ex_date, action: str, value: float, fetched_at: float | None = None) -> None:
    """One ``price_actions`` row (a split ratio, or a dividend / capital-gain amount)."""
    at = time.time() if fetched_at is None else float(fetched_at)
    rawstore.write_rows("price_actions", [{
        "ticker": ticker, "ex_date": _session(ex_date), "action": action, "value": float(value),
        "session_date": _session(ex_date), "source": "yfinance", "fetched_at": at, "run_id": "test", "run_at": at}])


def delete_bars(ticker: str, since) -> None:
    """Drop ``ticker``'s bars from session ``since`` on (a stale benchmark)."""
    with rawstore.connect(db=DB) as conn:
        conn.execute("DELETE FROM price_bars_daily WHERE ticker = ? AND session_date >= ?", [ticker, _session(since)])


def clear() -> None:
    with rawstore.connect(db=DB) as conn:
        conn.execute("DELETE FROM price_bars_daily")
        conn.execute("DELETE FROM price_actions")
