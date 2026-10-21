"""Name / sector / type lookup for the SEC 13F universe (one batched UW screener call).

Moved here from the removed TradeWhisperer module (2026-09-30): the 13F meta step
(`app/runners.build_sec13f_meta`) is its only user. Sectors are the Morningstar names
(UW's own list, #266): the screener's sector goes through ``core.gics_map.canonical_sector``,
which also maps an older GICS spelling; the small override tables below only patch the
vendor's blind spots (foreign listings, crypto, indices).
"""
from __future__ import annotations


from RevenueChaser.core.gics_map import canonical_sector

# Manual names for tickers the UW screener cannot resolve.
NAME_ALIASES = {
    "005930": "Samsung Electronics Co Ltd",
    "000660": "SK Hynix Inc",
    "BRK.B": "Berkshire Hathaway Inc",
    "BTC": "Bitcoin",
    "DOGE": "Dogecoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "BNB": "BNB",
    "XRP": "XRP",
    "DXY": "US Dollar Index",
    "VIX": "CBOE Volatility Index",
    "NDX": "Nasdaq 100 Index",
    "OTC:KRKNF": "Kraken Robotics Inc",
    "XFAB": "X-FAB Silicon Foundries SE",
    "DSV": "DSV A/S",
    "FNMA": "Fannie Mae",
    "TIL": "Instil Bio Inc",
    "SIVE": "Sivers Semiconductors AB",
    "RDW": "Redwire Corp",
}

# Crypto / indices render as "Other" even if the screener maps them to a trust.
TICKER_TYPE_OVERRIDES = {
    "BTC": "Other", "DOGE": "Other", "ETH": "Other", "SOL": "Other",
    "BNB": "Other", "XRP": "Other", "DXY": "Other", "VIX": "Other", "NDX": "Other",
}

# Non-equity instruments: an asset class instead of a sector.
TICKER_ASSET_CLASS = {
    "BNB": "Crypto / Digital Assets",
    "DOGE": "Crypto / Digital Assets",
    "SOL": "Crypto / Digital Assets",
    "BTC": "Crypto / Digital Assets",
    "ETH": "Crypto / Digital Assets",
    "XRP": "Crypto / Digital Assets",
    "NDX": "Indices & Benchmarks",
    "VIX": "Indices & Volatility",
    "DXY": "Currencies & Macro",
}

# Morningstar sector for real equities the screener cannot classify (data-gap patch).
TICKER_GICS_OVERRIDES = {
    "000660": "Technology",
    "005930": "Technology",
    "SIVE": "Technology",
    "XFAB": "Technology",
    "BRK.B": "Financial Services",
    "FNMA": "Financial Services",
    "DSV": "Industrials",
    "OTC:KRKNF": "Industrials",
    "RDW": "Industrials",
    "TIL": "Healthcare",
}


def resolve_sector(ticker: str, raw_sector: str) -> str:
    """Asset class for non-equities, else the Morningstar sector (``Unclassified`` when unknown)."""
    if ticker in TICKER_ASSET_CLASS:
        return TICKER_ASSET_CLASS[ticker]
    if ticker in TICKER_GICS_OVERRIDES:
        return canonical_sector(TICKER_GICS_OVERRIDES[ticker])
    return canonical_sector(raw_sector)


def classify_type(issue_type: str) -> str:
    it = (issue_type or "").strip().lower()
    if not it:
        return "Other"
    if "etf" in it or "fund" in it or "etn" in it or "trust" in it or "index" in it:
        return "ETF"
    if "stock" in it or "adr" in it or "common" in it or "equity" in it or "reit" in it or "share" in it:
        return "Stock"
    return "Other"


def fetch_universe_info(uw, tickers: list[str]) -> dict:
    """One batched screener call -> ``{ticker: {name, sector, type}}``.

    Tickers the screener can't resolve fall back to ``NAME_ALIASES``. A screener
    failure returns only the alias rows (callers merge, never blank existing meta).
    """
    info = {}
    try:
        scr = uw.screener_batch(sorted(tickers))
    except Exception:  # noqa: BLE001 - screener outage: aliases only
        scr = {}

    for t, item in scr.items():
        typ = TICKER_TYPE_OVERRIDES.get(t, classify_type(item.get("issue_type", "")))
        name = (item.get("full_name") or item.get("name") or "").strip()
        if not name or name in ("-", "N/A"):
            name = NAME_ALIASES.get(t, "")
        info[t] = {
            "name": name,
            "sector": resolve_sector(t, item.get("sector", "")),
            "type": typ,
        }

    for t in tickers:
        if t not in info and t in NAME_ALIASES:
            info[t] = {
                "name": NAME_ALIASES[t],
                "sector": resolve_sector(t, ""),
                "type": TICKER_TYPE_OVERRIDES.get(t, "Other"),
            }
    return info
