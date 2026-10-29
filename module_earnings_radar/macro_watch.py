"""The FRED request of the nightly macro sweep (``inputs/macro.py``, D27).

Ingestion only. No board reads this module: the Earnings board takes its 10-year yield from the market store
(``store_reads.dgs10_map``) and the Rotation board plots its macro series from the same store
(``module_sector_rotation/engine.macro_points``), so neither calls FRED or Yahoo while it renders.

What was here before: ``fred_daily_map`` (the board's own FRED read for R6 yield beta) and the weekly pair
``_fred_weekly`` / ``_yf_weekly`` behind ``macro_weekly_closes`` (the RRG's old macro plot). The market build
(``rcdata/build/market_series.py``) fetches both the daily and the weekly series now, so all four are gone.
"""
from __future__ import annotations

import logging
import os
import re

from RevenueChaser.core.config import FRED_API_KEY
from RevenueChaser.core.providers.result import ProviderResult

logger = logging.getLogger(__name__)

_API_KEY_QS = re.compile(rb"api_key=[^&\s]+")


def _not_json_diagnostic(r) -> dict:
    """Key-free diagnostic for a 2xx response whose body is not JSON."""
    ctype = str(getattr(r, "headers", {}).get("content-type", "") or "")[:60]
    size = len(getattr(r, "content", b"") or b"")
    return {"error": f"not_json HTTP {getattr(r, 'status_code', '?')} {ctype} len={size}", "attempts": 1}


def _log_body(r) -> None:
    """Debug-only body snippet with any query-string key scrubbed."""
    body = (getattr(r, "content", b"") or b"")[:400]
    if body:
        logger.debug("[fred-body] %s", _API_KEY_QS.sub(b"api_key=<redacted>", body))

FRED_BASE = os.getenv("RC_FRED_BASE") or "https://api.stlouisfed.org/fred/series/observations"




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
        }, timeout=25, follow_redirects=True)
        r.raise_for_status()
        try:
            payload = r.json()
        except ValueError:
            _log_body(r)
            return ProviderResult("failed", [], _not_json_diagnostic(r), series_id)
        obs = payload.get("observations", [])
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
