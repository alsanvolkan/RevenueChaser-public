"""SPX daily candles: yfinance ^GSPC (UW OHLC is permission-denied for the index)."""
from __future__ import annotations

from datetime import date, timedelta

import yfinance as yf

from .config import LOOKBACK_WEEKS, to_float


def week_window_start(asof: date | str) -> date:
    """Monday of the week ``LOOKBACK_WEEKS`` weeks before the as-of week.

    The as-of week is floored to its Monday, so any day within week N maps to
    the same start (week N - LOOKBACK_WEEKS).
    """
    asof_date = asof if isinstance(asof, date) else date.fromisoformat(asof)
    monday = asof_date - timedelta(days=asof_date.weekday())
    return monday - timedelta(weeks=LOOKBACK_WEEKS)


def fetch_spx_candles(asof: date | str, ticker: str = "^GSPC") -> list[dict]:
    """Daily OHLC for a symbol from the 8-week window start through ``asof``.

    Defaults to ^GSPC (S&P 500 index); pass a yfinance symbol to generalise.
    """
    asof_date = asof if isinstance(asof, date) else date.fromisoformat(asof)
    start = week_window_start(asof_date) - timedelta(days=3)  # holiday buffer
    end = asof_date + timedelta(days=1)
    df = yf.Ticker(ticker).history(
        start=start.isoformat(),
        end=end.isoformat(),
        auto_adjust=False,
    )
    rows = []
    if df is not None and not df.empty:
        for idx, row in df.iterrows():
            d = idx.date() if hasattr(idx, "date") else date.fromisoformat(str(idx)[:10])
            rows.append({
                "date": d.isoformat(),
                "open": to_float(row.get("Open")),
                "high": to_float(row.get("High")),
                "low": to_float(row.get("Low")),
                "close": to_float(row.get("Close")),
                "volume": to_float(row.get("Volume")),
            })
    rows.sort(key=lambda r: r["date"])
    return rows


def price_window_sessions(rows: list[dict], asof: date | str) -> list[dict]:
    """Completed sessions within the 8-week window ending on (or before) ``asof``."""
    asof_date = asof if isinstance(asof, date) else date.fromisoformat(asof)
    start = week_window_start(asof_date)
    return [
        r for r in rows
        if r.get("close") is not None
        and start.isoformat() <= r["date"] <= asof_date.isoformat()
    ]
