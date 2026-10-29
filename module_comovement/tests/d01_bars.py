"""Seed daily bars in the Co-movement and Relatedness tests: the prices store since PR 8 (G3).

Both boards read ``rcdata.read.ticker_bars(basis="total_return")`` (``ohlc.price_frame``). The tests build their
markets as ``swing_ohlc``-shaped rows (``ticker``, ``ts``, ``close``, ``cached_at``); ``write_bars`` stores the same
closes as served bars of ``prices.duckdb`` (``rcdata/tests/prices_store``), one per (ticker, session), replacing each
ticker's stored series. The module keeps its D01-era name so the tests that import it did not move.
"""
from __future__ import annotations

from RevenueChaser.rcdata.tests import prices_store


def write_bars(rows: list[dict], *, replace: bool = True) -> int:
    """``rows`` (``ticker``, ``ts`` or ``session_date``, ``close``, optional ``open``/``high``/``low``/``volume`` and
    ``cached_at`` or ``fetched_at``) as served bars, as traded. The first row of a (ticker, session) wins."""
    return prices_store.put_bars(rows, replace=replace)


def write_action(ticker: str, ex_date, action: str, value: float, fetched_at: float | None = None) -> None:
    """One applied ``corporate_action`` (a ``split`` ratio, or a ``dividend`` amount)."""
    prices_store.put_action(ticker, ex_date, action, value, fetched_at=fetched_at)


def delete_bars(ticker: str, since) -> None:
    """Drop ``ticker``'s bars from session ``since`` on (a stale benchmark)."""
    prices_store.delete_bars(ticker, since)


def clear() -> None:
    prices_store.clear()
