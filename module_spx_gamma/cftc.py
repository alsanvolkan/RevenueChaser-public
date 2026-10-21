"""CFTC Commitments of Traders — Traders in Financial Futures (TFF).

Pulls the latest E-mini S&P 500 (CME) TFF row from CFTC's public Socrata API.
Maps directly to the report's COT section: asset manager / leveraged funds /
dealer net positions and their weekly change.
"""
from __future__ import annotations

import os

import httpx

from .config import HTTP_MAX_RETRIES, HTTP_TIMEOUT_SECONDS, to_float

CFTC_TFF_BASE = os.getenv("RC_CFTC_BASE") or "https://publicreporting.cftc.gov/resource/udgc-27he.json"
E_MINI_MARKET = "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE"


def _market_where() -> str:
    # SoQL: match the E-mini market & exchange name (URL-encoded later by httpx)
    return f"market_and_exchange_names like '{E_MINI_MARKET}'"


def fetch_cftc_tff() -> dict | None:
    """Latest E-mini S&P 500 TFF row: dealer/asset-mgr/leveraged-funds net + Δ."""
    params = {
        "$where": _market_where(),
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": 1,
    }
    last_err = None
    for attempt in range(HTTP_MAX_RETRIES + 1):
        try:
            resp = httpx.get(CFTC_TFF_BASE, params=params, timeout=HTTP_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            last_err = str(exc)
            continue
        if resp.status_code != 200:
            last_err = f"{resp.status_code}: {resp.text[:200]}"
            continue
        data = resp.json()
        if not isinstance(data, list) or not data:
            return None
        return _map_row(data[0])
    return None


def _map_row(row: dict) -> dict | None:
    def g(*keys):
        for k in keys:
            v = to_float(row.get(k))
            if v is not None:
                return v
        return None

    am_long = g("asset_mgr_positions_long", "asset_mgr_positions_long_all")
    am_short = g("asset_mgr_positions_short", "asset_mgr_positions_short_all")
    lf_long = g("lev_money_positions_long", "lev_money_positions_long_all")
    lf_short = g("lev_money_positions_short", "lev_money_positions_short_all")
    d_long = g("dealer_positions_long_all")
    d_short = g("dealer_positions_short_all")

    if am_long is None and lf_long is None and d_long is None:
        return None

    return {
        "report_date": row.get("report_date_as_yyyy_mm_dd"),
        "open_interest": g("open_interest_all"),
        "asset_manager": {
            "long": am_long,
            "short": am_short,
            "net": (am_long - am_short) if (am_long is not None and am_short is not None) else None,
            "change": g("change_in_asset_mgr_long"),
        },
        "leveraged_funds": {
            "long": lf_long,
            "short": lf_short,
            "net": (lf_long - lf_short) if (lf_long is not None and lf_short is not None) else None,
            "change": g("change_in_lev_money_long"),
        },
        "dealer": {
            "long": d_long,
            "short": d_short,
            "net": (d_long - d_short) if (d_long is not None and d_short is not None) else None,
            "change": g("change_in_dealer_long_all"),
        },
    }
