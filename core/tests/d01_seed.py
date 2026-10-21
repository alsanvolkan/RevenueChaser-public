"""Seed D01 (``price_bars_daily``) bars in unit tests (#383 R3).

Since C12 and K8b, Swing and Fundamentals score their picks from D01 and the freeze and the history snapshot check
the picks' D01 bars (``ledger.ohlc_gaps``). D01 is first-write-wins (``rawstore`` append), so a test that "rewrites"
a series (a later run with fresher bars) replaces the ticker's rows here; the real writer never stores a bar before
its session's close + 30 min, so the tests that stamp one earlier stage what the check must refuse.
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.core import rawstore


def seed_d01(ticker: str, days, fetched_at: float, *, close: float = 1.5, replace: bool = True) -> None:
    """One flat bar per day in ``days`` (dates or ISO texts) for ``ticker``, stamped ``fetched_at``."""
    if replace:
        with rawstore.connect(db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw")) as conn:
            conn.execute("DELETE FROM price_bars_daily WHERE ticker = ?", [ticker])
    rawstore.write_rows("price_bars_daily", [{
        "ticker": ticker, "session_date": d if isinstance(d, date) else date.fromisoformat(str(d)[:10]),
        "open": 1.0, "high": 2.0, "low": 0.5, "close": close, "adj_close": None, "volume": 100.0,
        "split_ratio": None, "adj_source": None, "source": "uw", "fetched_at": fetched_at, "run_id": "seed",
        "run_at": fetched_at} for d in days])
