"""Macro series resolution (FRED + yfinance).

Weekly closes feed the rotation RRG map. Daily dated observations feed
Earnings Radar R6 (10Y yield beta). The isolated Macro Market Watch panel
was removed; yields/futures/volatility are plotted on the RRG via
``macro_weekly_closes``.
"""
from __future__ import annotations

import os

import logging

from RevenueChaser.core.config import FRED_API_KEY
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.module_sector_rotation.universe import MACRO_SERIES

logger = logging.getLogger(__name__)

FRED_BASE = os.getenv("RC_FRED_BASE") or "https://api.stlouisfed.org/fred/series/observations"


def _fred_weekly(series_id: str, api_key: str, limit: int = 160):
    """Weekly (eop) FRED observations -> ascending list of floats."""
    if not api_key:
        return []
    import httpx
    try:
        r = httpx.get(FRED_BASE, params={
            "series_id": series_id, "api_key": api_key,
            "file_type": "json", "frequency": "w", "aggregation_method": "eop",
            "sort_order": "desc", "limit": limit,
        }, timeout=25, follow_redirects=True)
        r.raise_for_status()
        obs = r.json().get("observations", [])
    except Exception as exc:
        logger.debug(f"[fred-weekly] {series_id}: {exc}")
        return []
    vals = []
    for o in obs:
        v = o.get("value")
        if v in (None, ".", ""):
            continue
        try:
            vals.append(float(v))
        except (ValueError, TypeError):
            continue
    return vals[::-1]


def fred_daily_result(series_id: str, api_key: str = None, limit: int = 80) -> ProviderResult:
    """Daily FRED observations of ONE request, with the outcome (E0 D27, #305).

    ``ok``: ``data`` is the ascending ``(YYYY-MM-DD, value)`` pairs (``.`` values skipped). ``failed``: ``data`` is
    ``[]`` and ``error`` says why: ``missing_key`` (no request sent), ``HTTP <status>`` or the exception's type. The
    error never carries the URL, whose query holds the key.
    """
    key = (api_key if api_key is not None else FRED_API_KEY) or ""
    if not key:
        return ProviderResult("failed", [], {"error": "missing_key", "attempts": 0}, series_id)
    import httpx
    try:
        r = httpx.get(FRED_BASE, params={
            "series_id": series_id, "api_key": key,
            "file_type": "json", "sort_order": "desc", "limit": limit,
        }, timeout=25)
        r.raise_for_status()
        obs = r.json().get("observations", [])
    except httpx.HTTPStatusError as exc:
        return ProviderResult("failed", [], {"error": f"HTTP {exc.response.status_code}", "attempts": 1}, series_id)
    except Exception as exc:  # noqa: BLE001 - transport or body; the type only (its text can hold the URL)
        return ProviderResult("failed", [], {"error": type(exc).__name__, "attempts": 1}, series_id)
    rows = []
    for o in obs:
        v = o.get("value")
        d = str(o.get("date") or "")[:10]
        if not d or v in (None, ".", ""):
            continue
        try:
            rows.append((d, float(v)))
        except (ValueError, TypeError):
            continue
    rows.reverse()
    return ProviderResult("ok", rows, None, series_id)


def _fred_daily(series_id: str, api_key: str = None, limit: int = 80) -> list[tuple[str, float]]:
    """Daily FRED observations -> ascending (YYYY-MM-DD, value) pairs.

    No weekly aggregation — R6 pairs these with ticker OHLC dates. An empty
    key short-circuits with no network call; a failed request gives ``[]``
    (``fred_daily_result`` says why).
    """
    res = fred_daily_result(series_id, api_key=api_key, limit=limit)
    if res.status != "ok" and res.error.get("error") != "missing_key":
        logger.debug(f"[fred-daily] {series_id}: {res.error.get('error')}")
    return res.data


def fred_daily_map(series_id: str = "DGS10", api_key: str = None, limit: int = 80) -> dict[str, float]:
    """Dated daily FRED series as ``{YYYY-MM-DD: value}`` for R6 yield beta."""
    return {d: v for d, v in _fred_daily(series_id, api_key=api_key, limit=limit)}


def _yf_weekly(symbol: str, limit: int = 160):
    """yfinance weekly closes -> ascending list of floats."""
    import math
    import yfinance as yf
    try:
        h = yf.Ticker(symbol).history(period="3y", interval="1wk")
        if h is None or h.empty:
            return []
        closes = [float(x) for x in h["Close"].tolist()
                  if x is not None and not (isinstance(x, float) and math.isnan(x))]
    except Exception as exc:
        logger.debug(f"[yf-weekly] {symbol}: {exc}")
        return []
    return closes[-limit:]


def macro_weekly_closes(symbol: str) -> list[float]:
    """Weekly closing series for a macro indicator (FRED -> yfinance)."""
    cfg = MACRO_SERIES.get(symbol, {})
    fred_id = cfg.get("fred")
    yf_sym = cfg.get("yf")

    if fred_id:
        vals = _fred_weekly(fred_id, FRED_API_KEY)
        if vals:
            return vals
    if yf_sym:
        vals = _yf_weekly(yf_sym)
        if vals:
            return vals
    return []
