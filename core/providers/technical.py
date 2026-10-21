"""Technical data (yfinance) — 52-week high, 10-day/90-day volume, 50-day SMA.

``fetch_technical`` is shared by the Quick Remarks and Small/Micro Cap engines.
"""
from __future__ import annotations

import logging

from ..config import to_float

logger = logging.getLogger(__name__)


def fetch_technical(ticker: str) -> dict:
    """yfinance info + 1y günlük tarihçe -> teknik alt kriter değerleri."""
    out = {
        "price": None, "high_52w": None, "dist_52w_pct": None,
        "sma50": None, "above_sma50": None,
        "vol_10d": None, "vol_90d": None, "vol_ratio": None,
    }
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info or {}
        hist = t.history(period="1y")
    except Exception as exc:  # pragma: no cover - network
        logger.debug("technical %s: %s", ticker, exc)
        return out

    price = to_float(info.get("currentPrice")) or to_float(info.get("regularMarketPrice"))
    high_52w = to_float(info.get("fiftyTwoWeekHigh"))

    if hist is not None and not hist.empty:
        closes = hist["Close"]
        if price is None:
            price = to_float(closes.iloc[-1])
        if high_52w is None:
            high_52w = to_float(hist["High"].max())
        sma50 = to_float(closes.rolling(50).mean().iloc[-1]) if len(closes) >= 50 else None
        vol = hist["Volume"]
        vol_10d = to_float(vol.tail(10).mean()) if len(vol) >= 10 else None
        vol_90d = to_float(vol.tail(90).mean()) if len(vol) >= 90 else None
        out["sma50"] = sma50
        out["above_sma50"] = (price > sma50) if (price is not None and sma50 is not None) else None
        out["vol_10d"] = vol_10d
        out["vol_90d"] = vol_90d
        out["vol_ratio"] = vol_10d / vol_90d if (vol_10d is not None and vol_90d) else None

    out["price"] = price
    out["high_52w"] = high_52w
    if price is not None and high_52w:
        out["dist_52w_pct"] = (high_52w - price) / high_52w

    return out
