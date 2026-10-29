"""Seeds for the Swing and Fundamentals Fetch in the e2e tier (#383 R3 C12).

Since PR 10 (G4) the two boards prefilter on and score from the prices store (``prices.duckdb`` in the market root,
which only the builder writes in production); before it, D04 (``quote_snapshot``) and D01 (``price_bars_daily``). A
test that runs their Fetch seeds both: ``seed_d01`` writes the D01 rows and the same bars into the prices store, and
``seed_quote`` the D04 row the universe pull records. The test process writes the dashboard's files directly:
``point_at`` aims ``rawstore.DB_PATH`` at the dashboard's store and ``config.MARKET_DIR`` at its market root.
"""
from __future__ import annotations

import time
from datetime import date

import pandas as pd


def point_at(dashboard, monkeypatch) -> None:
    """Write the dashboard's store and market root from this process (the dashboard is idle between calls)."""
    from pathlib import Path

    import RevenueChaser.core.rawstore as rawstore
    from RevenueChaser.core import config
    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
    monkeypatch.setattr(config, "MARKET_DIR", Path(dashboard.env["RC_MARKET_DIR"]))


def seed_d01(ticker: str, df: pd.DataFrame, fetched_at: float | None = None) -> None:
    """``df``'s Open/High/Low/Close/Volume bars as D01 rows and in the prices store; ``fetched_at`` defaults to now
    (after any past close)."""
    import RevenueChaser.core.rawstore as rawstore
    from RevenueChaser.rcdata.tests.prices_seed import seed_frame
    at = time.time() if fetched_at is None else fetched_at
    seed_frame(ticker, df, at)
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
