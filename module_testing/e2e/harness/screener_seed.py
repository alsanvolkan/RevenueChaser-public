"""Seeds for the Swing and Fundamentals Fetch in the e2e tier (#383 R3 C12).

Since C12 the two boards prefilter on D04 (``quote_snapshot``) and score from D01 (``price_bars_daily``), so a test
that runs their Fetch writes those two tables, the way the universe pull and RC-Daily-Bars leave them. The test
process writes the dashboard's store directly (``rawstore.DB_PATH`` patched to the dashboard's store folder; D01 lives
beside it in ``prices.duckdb``).
"""
from __future__ import annotations

import time
from datetime import date

import pandas as pd


def seed_d01(ticker: str, df: pd.DataFrame, fetched_at: float | None = None) -> None:
    """``df``'s Open/High/Low/Close/Volume bars as D01 rows; ``fetched_at`` defaults to now (after any past close)."""
    import RevenueChaser.core.rawstore as rawstore
    at = time.time() if fetched_at is None else fetched_at
    rawstore.write_rows("price_bars_daily", [{
        "ticker": ticker, "session_date": pd.Timestamp(ts).date(), "open": float(r["Open"]),
        "high": float(r["High"]), "low": float(r["Low"]), "close": float(r["Close"]), "adj_close": None,
        "volume": float(r["Volume"]), "split_ratio": None, "adj_source": None, "source": "uw", "fetched_at": at,
        "run_id": "e2e", "run_at": at} for ts, r in df.iterrows()])


def seed_quote(ticker: str, session: date, quote: dict, market_cap: float) -> None:
    """One D04 Yahoo row: the screener quote ``quote`` the universe pull recorded for ``session``."""
    import RevenueChaser.core.rawstore as rawstore
    at = time.time()
    rawstore.write_rows("quote_snapshot", [{
        "ticker": ticker, "session_date": session, "source": "yfinance", "market_cap": market_cap,
        "price": quote.get("regularMarketPrice"), "shares_outstanding": None,
        "week_52_high": quote.get("fiftyTwoWeekHigh"), "week_52_low": None, "payload": quote, "fetched_at": at,
        "run_id": "e2e", "run_at": at}])


def bars_to(end: date, periods: int = 300) -> pd.DataFrame:
    """``periods`` rising business-day bars ending on ``end`` (the shape of ``test_ohlc._df``)."""
    idx = pd.bdate_range(end=end, periods=periods)
    close = pd.Series(100 + pd.Series(range(periods), index=idx) * 0.1, index=idx)
    return pd.DataFrame({"Open": close.shift(1).fillna(close.iloc[0]), "High": close * 1.01,
                         "Low": close * 0.99, "Close": close, "Volume": 1_000_000})
