"""The one writer of ``swing_ohlc``, the daily OHLC cache (C8, #383 R3; card D option 2).

Two boards wrote the cache: Swing's per-ticker refetch (``module_swing_potential/indicators.py``, now only the SPY
fallback of Swing and Fundamentals, which score from D01 since C12) and Co-movement's bulk download
(``module_comovement/ohlc.py``). Both now write through :func:`write`, in Co-movement's row format (moved here
unchanged: ``ts`` is ``YYYY-MM-DDT00:00:00`` with no zone, and a bar with no close is not a bar). The table's write
mode replaces a ticker's whole stored series, so the writer guards that replacement once, for both:

- **#261:** a short series (fewer than ``SHORT_BARS`` bars and fewer than the stored ones) never replaces a longer
  stored history;
- **except a new listing** under a reused symbol, which shares no session with the stored bars **and** starts more
  than ``NEW_LISTING_GAP_DAYS`` calendar days after the stored last bar (F2b, avo "30 gün şartı", 2026-10-15 20:13
  Amsterdam). Without the gap a truncated download (one bar right after the stored last bar shares no session
  either) replaced a 500-bar history.

Readers stay where they are (``indicators.load_ohlc``, ``ohlc.price_frame``, ``ledger.last_bars``). No network.
"""
from __future__ import annotations

import logging
import time
from datetime import date

import pandas as pd

from RevenueChaser.core import rawstore

logger = logging.getLogger(__name__)

TABLE = "swing_ohlc"
SHORT_BARS = 252               # a series with fewer bars is short (#261); the boards need 252 to score
NEW_LISTING_GAP_DAYS = 30      # F2b: a new listing starts more than this many days after the stored last bar


def _f(v):
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def canon_ts(ts) -> str:
    """One stored format for a daily bar: ``YYYY-MM-DDT00:00:00`` (no tz)."""
    try:
        return pd.Timestamp(ts).strftime("%Y-%m-%dT00:00:00")
    except (TypeError, ValueError):
        return str(ts)[:10] + "T00:00:00"


def rows_for(ticker: str, frame: pd.DataFrame, cached_at: float) -> list[dict]:
    """``frame``'s bars (Open/High/Low/Close/Volume, one row per session) as ``swing_ohlc`` rows."""
    out = []
    for ts, r in frame.iterrows():
        close = _f(r.get("Close"))
        if close is None:
            continue
        out.append({
            "ticker": ticker,
            "ts": canon_ts(ts),
            "open": _f(r.get("Open")), "high": _f(r.get("High")),
            "low": _f(r.get("Low")), "close": close,
            "volume": _f(r.get("Volume")), "cached_at": cached_at,
        })
    return out


def rows_from_download(df, tickers: list[str], cached_at: float | None = None) -> list[dict]:
    """Flatten a ``yf.download`` frame into ``swing_ohlc`` rows.

    Handles the two shapes yfinance returns: a ``(ticker, field)`` MultiIndex
    for a multi-ticker chunk, and a flat frame for a single ticker.
    """
    if df is None or len(df) == 0:
        return []
    cached_at = cached_at if cached_at is not None else time.time()
    out: list[dict] = []
    if isinstance(df.columns, pd.MultiIndex):
        level0 = set(df.columns.get_level_values(0))
        for t in tickers:
            if t in level0:
                out.extend(rows_for(t, df[t], cached_at))
    elif len(tickers) == 1:
        out.extend(rows_for(tickers[0], df, cached_at))
    return out


def short_series_refused(stored_days, fresh_days) -> bool:
    """True when a fresh series (its sessions, ``YYYY-MM-DD``) must not replace the stored one (#261, F2b)."""
    stored = sorted({str(d)[:10] for d in stored_days})
    fresh = sorted({str(d)[:10] for d in fresh_days})
    if not stored or not fresh or len(fresh) >= SHORT_BARS or len(fresh) >= len(stored):
        return False
    if set(fresh) & set(stored):
        return True                    # a short slice of the same listing
    gap = (date.fromisoformat(fresh[0]) - date.fromisoformat(stored[-1])).days
    return gap <= NEW_LISTING_GAP_DAYS


def _stored_days(tickers: list[str]) -> dict[str, list[str]]:
    rows = rawstore.query(f"SELECT ticker, substr(ts, 1, 10) AS d FROM {TABLE} WHERE ticker IN (SELECT unnest(?))",
                          [tickers])
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["ticker"], []).append(r["d"])
    return out


def write(rows: list[dict]) -> dict[str, str]:
    """Write ``rows`` (each ticker's whole fresh series) to ``swing_ohlc``: ``{ticker: "written" | "refused"}``.

    Each ticker is decided on its own (:func:`short_series_refused` against its stored sessions); the written
    tickers' series replace their stored ones in one write. A refused ticker keeps its stored rows untouched.
    """
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)
    if not by_ticker:
        return {}
    stored = _stored_days(sorted(by_ticker))
    outcome: dict[str, str] = {}
    keep: list[dict] = []
    for ticker, fresh in by_ticker.items():
        old = stored.get(ticker, [])
        if short_series_refused(old, [r["ts"] for r in fresh]):
            logger.warning("ohlc %s: refusing a short series (%d bars) over the stored %d", ticker, len(fresh),
                           len(old))
            outcome[ticker] = "refused"
        else:
            outcome[ticker] = "written"
            keep.extend(fresh)
    if keep:
        rawstore.write_rows(TABLE, keep)
    return outcome
