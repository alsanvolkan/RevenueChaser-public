"""Derived ``company_profile`` VIEW — one row per ``equity_universe`` ticker.

Scalar joins only (AG scores + fundamentals + swing); no N-valued joins and no
scalar theme (themes live in ``ticker_themes``). The view is created by
``rawstore._VIEWS`` on ``ensure_all_tables()``. Boards iterate
``core/universe.py::classification_universe`` and join this view by ticker.
"""
from __future__ import annotations

from . import rawstore


def ensure() -> None:
    """Make sure the view (and its base tables) exist."""
    rawstore.ensure_all_tables()


def query(tickers: list[str] | None = None) -> list[dict]:
    if tickers:
        ph = ", ".join(["?"] * len(tickers))
        return rawstore.query(
            f"SELECT * FROM company_profile WHERE ticker IN ({ph})",
            [t.upper() for t in tickers])
    return rawstore.query(
        "SELECT * FROM company_profile ORDER BY market_cap DESC NULLS LAST")


def get(ticker: str) -> dict | None:
    rows = rawstore.query(
        "SELECT * FROM company_profile WHERE ticker = ?", [ticker.upper()])
    return rows[0] if rows else None


def count() -> int:
    return rawstore.query("SELECT count(*) n FROM company_profile")[0]["n"]
