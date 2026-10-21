"""VIX family snapshot from yfinance (UW VIX term structure is a paid add-on).

Fetches the last close of the VIX index family used in the report's VIX layer:
^VIX (spot), ^VIX9D, ^VIX3M, ^VIX6M, ^VIX1Y and ^VVIX (vol-of-vol). Derives the
term-structure mode (contango vs backwardation) from the spot vs 3M relationship.
"""
from __future__ import annotations

from datetime import date, timedelta

import yfinance as yf

from .config import to_float

VIX_TICKERS = ["^VIX", "^VIX9D", "^VIX3M", "^VIX6M", "^VIX1Y", "^VVIX"]


def fetch_vix_family() -> dict:
    """Return the latest close for each VIX ticker (best-effort per ticker)."""
    values: dict[str, float | None] = {}
    for t in VIX_TICKERS:
        try:
            df = yf.Ticker(t).history(period="5d", auto_adjust=False)
            if df is not None and not df.empty:
                values[t] = to_float(df["Close"].iloc[-1])
            else:
                values[t] = None
        except Exception:  # noqa: BLE001 — per-ticker degrade
            values[t] = None
    return {"tickers": values}


def vix_term_mode(values: dict) -> str | None:
    """contango (VIX < VIX3M) vs backwardation (VIX > VIX3M); None when missing."""
    vix = values.get("^VIX")
    vix3m = values.get("^VIX3M")
    if vix is None or vix3m is None:
        return None
    if vix < vix3m:
        return "contango"
    if vix > vix3m:
        return "backwardation"
    return "flat"
