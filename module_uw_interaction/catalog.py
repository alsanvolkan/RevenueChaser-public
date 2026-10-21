"""UW Interaction catalog — every tool a user can run against the Unusual Whales API.

Each entry describes one REST endpoint (path + query params), the *family* it
belongs to (mirroring the Unusual Whales MCP grouping), and how to render rows.
Entries are data only; ``module_uw_interaction.engine`` turns them into calls
against ``UWClient``. Paths were verified against the published OpenAPI spec
(``https://api.unusualwhales.com/api/openapi``) and are relative to the client
base (no leading ``/api``), matching every existing ``UWClient`` call site.

A tool is added by appending one dict — no engine change. That is the point of a
lab: experiment with an endpoint first, promote it to a curated board later.
"""

# Param kinds: text | int | float | bool | select. ``in_path`` marks a param
# that fills a ``{name}`` placeholder in the path instead of the query string.
# ``required`` params are validated before any network call.

TOOLS = [
    # ── Options flow ──
    {
        "key": "flow_alerts", "family": "Options flow", "label": "Flow Alerts",
        "path": "/option-trades/flow-alerts", "rows": "data",
        "help": "Rule-based alerts aggregated from the full option tape.",
        "params": [
            {"name": "ticker_symbol", "label": "Ticker", "kind": "text", "required": False, "help": "Optional single ticker filter"},
            {"name": "min_premium", "label": "Min premium ($)", "kind": "float", "default": "10000", "help": "Only prints at/above this premium"},
            {"name": "is_sweep", "label": "Sweeps only", "kind": "bool", "default": "false"},
            {"name": "unusual", "label": "Unusual only", "kind": "bool", "default": "false"},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
        ],
    },
    {
        "key": "option_trades", "family": "Options flow", "label": "Option Trades (tape)",
        "path": "/option-trades", "rows": "data",
        "help": "Raw option tape prints with DTE/premium filters.",
        "params": [
            {"name": "ticker_symbol", "label": "Ticker", "kind": "text", "required": False},
            {"name": "min_premium", "label": "Min premium ($)", "kind": "float", "default": "10000"},
            {"name": "min_dte", "label": "Min DTE", "kind": "int"},
            {"name": "max_dte", "label": "Max DTE", "kind": "int"},
            {"name": "size_greater_oi", "label": "Size > OI", "kind": "bool", "default": "false"},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
        ],
    },
    {
        "key": "multi_leg", "family": "Options flow", "label": "Multi-Leg Strategies",
        "path": "/option-trades/multi-leg", "rows": "data",
        "help": "Detected multi-leg strategies (spreads, condors, butterflies…).",
        "params": [
            {"name": "ticker_symbol", "label": "Ticker", "kind": "text", "required": False},
            {"name": "strategy", "label": "Strategy", "kind": "text"},
            {"name": "net_side", "label": "Net side", "kind": "text"},
            {"name": "min_premium", "label": "Min premium ($)", "kind": "float", "default": "10000"},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
        ],
    },
    {
        "key": "unusual_activity", "family": "Options flow", "label": "Unusual Options Activity",
        "path": "/option-activity/unusual", "rows": "data",
        "help": "API equivalent of the live options-flow 'unusual' view.",
        "params": [
            {"name": "ticker_symbol", "label": "Ticker", "kind": "text", "required": False},
            {"name": "unusual", "label": "Unusual only", "kind": "bool", "default": "true"},
            {"name": "min_premium", "label": "Min premium ($)", "kind": "float", "default": "10000"},
            {"name": "max_dte", "label": "Max DTE", "kind": "int"},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
        ],
    },
    {
        "key": "net_flow_expiry", "family": "Options flow", "label": "Net Flow by Expiry",
        "path": "/net-flow/expiry", "rows": "data",
        "help": "Net premium flow by tide/moneyness/expiration buckets (ZDTE view).",
        "params": [
            {"name": "date", "label": "Date (YYYY-MM-DD)", "kind": "text", "required": False},
            {"name": "moneyness", "label": "Moneyness", "kind": "text"},
            {"name": "tide_type", "label": "Tide type", "kind": "text"},
            {"name": "expiration", "label": "Expiration bucket", "kind": "text"},
        ],
    },
    # ── Dark pool ──
    {
        "key": "darkpool_recent", "family": "Dark pool", "label": "Dark Pool — Recent (market-wide)",
        "path": "/darkpool/recent", "rows": "data",
        "help": "Latest dark-pool trades across the whole tape.",
        "params": [
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
            {"name": "date", "label": "Date (YYYY-MM-DD)", "kind": "text", "required": False},
            {"name": "min_premium", "label": "Min premium ($)", "kind": "float"},
        ],
    },
    {
        "key": "darkpool_ticker", "family": "Dark pool", "label": "Dark Pool — Ticker",
        "path": "/darkpool/{ticker}", "rows": "data",
        "help": "Dark-pool trades for one ticker on a day.",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": True, "in_path": True, "default": "AAPL"},
            {"name": "date", "label": "Date (YYYY-MM-DD)", "kind": "text", "required": False},
            {"name": "min_premium", "label": "Min premium ($)", "kind": "float"},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
        ],
    },
    {
        "key": "darkpool_price_levels", "family": "Dark pool", "label": "Dark Pool — Price Levels",
        "path": "/darkpool/{ticker}/price-levels", "rows": "data",
        "help": "Rounded dark-pool volume concentration by price level.",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": True, "in_path": True, "default": "AAPL"},
            {"name": "date", "label": "Date (YYYY-MM-DD)", "kind": "text", "required": False},
        ],
    },
    # ── Greeks & volatility ──
    {
        "key": "gex_levels", "family": "Greeks & volatility", "label": "GEX Levels",
        "path": "/stock/{ticker}/gex-levels", "rows": "object",
        "help": "Gamma-exposure price levels (flip, walls) for a ticker.",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": True, "in_path": True, "default": "SPY"},
            {"name": "date", "label": "Date (YYYY-MM-DD)", "kind": "text", "required": False},
        ],
    },
    {
        "key": "greek_exposure", "family": "Greeks & volatility", "label": "Greek Exposure",
        "path": "/stock/{ticker}/greek-exposure", "rows": "data",
        "help": "Assumed dealer greek exposure per strike for a ticker.",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": True, "in_path": True, "default": "SPY"},
            {"name": "date", "label": "Date (YYYY-MM-DD)", "kind": "text", "required": False},
            {"name": "timeframe", "label": "Timeframe", "kind": "text", "default": "1Y"},
        ],
    },
    {
        "key": "greek_exposure_expiry", "family": "Greeks & volatility", "label": "Greek Exposure by Expiry",
        "path": "/stock/{ticker}/greek-exposure/expiry", "rows": "data",
        "help": "Dealer greek exposure grouped by expiry.",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": True, "in_path": True, "default": "SPY"},
            {"name": "date", "label": "Date (YYYY-MM-DD)", "kind": "text", "required": False},
        ],
    },
    {
        "key": "max_pain", "family": "Greeks & volatility", "label": "Max Pain",
        "path": "/stock/{ticker}/max-pain", "rows": "object",
        "help": "Max-pain levels for all expiries (last 120 days).",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": True, "in_path": True, "default": "AAPL"},
            {"name": "date", "label": "Date (YYYY-MM-DD)", "kind": "text", "required": False},
        ],
    },
    {
        "key": "iv_rank", "family": "Greeks & volatility", "label": "IV Rank",
        "path": "/stock/{ticker}/iv-rank", "rows": "data",
        "help": "Implied-volatility rank history for a ticker.",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": True, "in_path": True, "default": "SPY"},
            {"name": "timespan", "label": "Timespan", "kind": "text", "default": "1y"},
        ],
    },
    {
        "key": "vol_term_structure", "family": "Greeks & volatility", "label": "IV Term Structure",
        "path": "/stock/{ticker}/volatility/term-structure", "rows": "data",
        "help": "Average ATM IV per expiry (term structure).",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": True, "in_path": True, "default": "SPY"},
            {"name": "date", "label": "Date (YYYY-MM-DD)", "kind": "text", "required": False},
        ],
    },
    # ── Short interest ──
    {
        "key": "short_screener", "family": "Short interest", "label": "Short Screener",
        "path": "/short_screener", "rows": "data",
        "help": "Short interest / float metrics across many tickers.",
        "params": [
            {"name": "tickers", "label": "Tickers (comma)", "kind": "text", "required": False, "help": "Optional comma-separated list"},
            {"name": "min_si_float", "label": "Min SI % of float", "kind": "float"},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
        ],
    },
    {
        "key": "short_data", "family": "Short interest", "label": "Short Data (ticker)",
        "path": "/shorts/{ticker}/data", "rows": "data",
        "help": "Short shares available + rebate for a ticker.",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": True, "in_path": True, "default": "GME"},
        ],
    },
    {
        "key": "short_volume_ratio", "family": "Short interest", "label": "Short Volume & Ratio",
        "path": "/shorts/{ticker}/volume-and-ratio", "rows": "data",
        "help": "Short volume and ratio series for a ticker.",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": True, "in_path": True, "default": "GME"},
        ],
    },
    {
        "key": "ftds", "family": "Short interest", "label": "Failures to Deliver",
        "path": "/shorts/{ticker}/ftds", "rows": "data",
        "help": "Daily failures-to-deliver for a ticker.",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": True, "in_path": True, "default": "GME"},
        ],
    },
    # ── Congress & insiders ──
    {
        "key": "congress_recent_trades", "family": "Congress & insiders", "label": "Congress — Recent Trades",
        "path": "/congress/recent-trades", "rows": "data",
        "help": "Latest transacted congressional trades.",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": False},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
        ],
    },
    {
        "key": "congress_unusual", "family": "Congress & insiders", "label": "Congress — Unusual Trades",
        "path": "/congress/unusual-trades", "rows": "data",
        "help": "Congressional trades flagged as unusual.",
        "params": [
            {"name": "types", "label": "Reason types", "kind": "text", "required": False},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
            {"name": "page", "label": "Page", "kind": "int", "default": "1"},
        ],
    },
    {
        "key": "politician_trades", "family": "Congress & insiders", "label": "Politician Trades (politics)",
        "path": "/politics/politician/trades", "rows": "data",
        "help": "Congress-member trades with politician linkage.",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": False},
            {"name": "politician_id", "label": "Politician ID", "kind": "text", "required": False},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
        ],
    },
    {
        "key": "insider_ticker_flow", "family": "Congress & insiders", "label": "Insider — Ticker Flow",
        "path": "/insider/{ticker}/ticker-flow", "rows": "data",
        "help": "Aggregated insider flow for a ticker.",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": True, "in_path": True, "default": "AAPL"},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
        ],
    },
    {
        "key": "insider_transactions", "family": "Congress & insiders", "label": "Insider — Transactions",
        "path": "/insider/transactions", "rows": "data",
        "help": "Latest insider transactions, filterable by ticker.",
        "params": [
            {"name": "ticker_symbol", "label": "Ticker", "kind": "text", "required": False},
            {"name": "min_value", "label": "Min value ($)", "kind": "float"},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
            {"name": "page", "label": "Page", "kind": "int", "default": "1"},
        ],
    },
    {
        "key": "insider_buy_sells", "family": "Congress & insiders", "label": "Insider — Buy & Sells",
        "path": "/market/insider-buy-sells", "rows": "data",
        "help": "Total insider buys/sells + notional across the market.",
        "params": [{"name": "limit", "label": "Limit", "kind": "int", "default": "100"}],
    },
    # ── Institutions ──
    {
        "key": "institutions_list", "family": "Institutions", "label": "Institutions — List",
        "path": "/institutions", "rows": "data",
        "help": "List of institutions with value/share ranges.",
        "params": [
            {"name": "name", "label": "Name filter", "kind": "text", "required": False},
            {"name": "min_total_value", "label": "Min total value ($)", "kind": "float"},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
        ],
    },
    {
        "key": "institution_holdings", "family": "Institutions", "label": "Institution — Holdings",
        "path": "/institution/{name}/holdings", "rows": "data",
        "help": "Holdings of one institution (name or CIK).",
        "params": [
            {"name": "name", "label": "Institution", "kind": "text", "required": True, "in_path": True, "default": "VANGUARD GROUP INC"},
            {"name": "ticker_symbol", "label": "Ticker", "kind": "text", "required": False},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
        ],
    },
    {
        "key": "institution_ownership", "family": "Institutions", "label": "Institution — Ownership (ticker)",
        "path": "/institution/{ticker}/ownership", "rows": "data",
        "help": "All 13F holders of a ticker (paginated list).",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": True, "in_path": True, "default": "AAPL"},
            {"name": "date", "label": "Report date", "kind": "text", "required": False},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
        ],
    },
    # ── Prediction markets ──
    {
        "key": "prediction_unusual", "family": "Prediction markets", "label": "Predictions — Unusual",
        "path": "/predictions/unusual", "rows": "data",
        "help": "Prediction markets with unusual activity.",
        "params": [
            {"name": "categories", "label": "Category", "kind": "select", "required": False,
             "options": ["", "Crypto", "Culture", "Finance", "Politics", "Science", "Sports", "Technology"]},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "50"},
        ],
    },
    {
        "key": "prediction_smart_money", "family": "Prediction markets", "label": "Predictions — Smart Money",
        "path": "/predictions/smart-money", "rows": "data",
        "help": "Profitable prediction-market traders.",
        "params": [
            {"name": "categories", "label": "Category", "kind": "select", "required": False,
             "options": ["", "Crypto", "Culture", "Finance", "Politics", "Science", "Sports", "Technology"]},
        ],
    },
    {
        "key": "prediction_whales", "family": "Prediction markets", "label": "Predictions — Whales",
        "path": "/predictions/whales", "rows": "data",
        "help": "Large prediction-market traders.",
        "params": [],
    },
    # ── Crypto ──
    {
        "key": "crypto_whales_recent", "family": "Crypto", "label": "Crypto — Recent Whale Trades",
        "path": "/crypto/whales/recent", "rows": "data",
        "help": "Recent large crypto trades across all pairs.",
        "params": [{"name": "limit", "label": "Limit", "kind": "int", "default": "50"}],
    },
    {
        "key": "crypto_pair_state", "family": "Crypto", "label": "Crypto — Pair State",
        "path": "/crypto/{pair}/state", "rows": "object",
        "help": "Current state for a crypto pair incl. 24h OHLCV.",
        "params": [
            {"name": "pair", "label": "Pair", "kind": "text", "required": True, "in_path": True},
        ],
    },
    # ── Market & macro ──
    {
        "key": "market_tide", "family": "Market & macro", "label": "Market Tide",
        "path": "/market/market-tide", "rows": "data",
        "help": "Proprietary market-wide options-activity tide.",
        "params": [
            {"name": "date", "label": "Date (YYYY-MM-DD)", "kind": "text", "required": False},
            {"name": "otm_only", "label": "OTM only", "kind": "bool", "default": "false"},
        ],
    },
    {
        "key": "market_oi_change", "family": "Market & macro", "label": "Market — OI Change",
        "path": "/market/oi-change", "rows": "data",
        "help": "Non-index/non-ETF contracts with the largest OI change.",
        "params": [
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
            {"name": "order", "label": "Order", "kind": "text", "default": "desc"},
        ],
    },
    {
        "key": "sector_tide", "family": "Market & macro", "label": "Sector Tide",
        "path": "/market/{sector}/sector-tide", "rows": "data",
        "help": "Options-activity tide for one sector.",
        "params": [
            {"name": "sector", "label": "Sector", "kind": "text", "required": True, "in_path": True, "default": "Technology"},
            {"name": "date", "label": "Date (YYYY-MM-DD)", "kind": "text", "required": False},
        ],
    },
    {
        "key": "top_net_impact", "family": "Market & macro", "label": "Top Net Impact",
        "path": "/market/top-net-impact", "rows": "data",
        "help": "Top tickers by net premium (half bullish, half bearish).",
        "params": [
            {"name": "limit", "label": "Limit", "kind": "int", "default": "50"},
        ],
    },
    {
        "key": "news_headlines", "family": "Market & macro", "label": "News Headlines",
        "path": "/news/headlines", "rows": "data",
        "help": "Latest financial-market news headlines.",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": False},
            {"name": "search_term", "label": "Search term", "kind": "text", "required": False},
            {"name": "major_only", "label": "Major only", "kind": "bool", "default": "false"},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "50"},
        ],
    },
    {
        "key": "daily_report", "family": "Market & macro", "label": "Daily Market Report",
        "path": "/market/daily-report", "rows": "data",
        "help": "Curated daily vol/skew/premium report.",
        "params": [
            {"name": "date", "label": "Date (YYYY-MM-DD)", "kind": "text", "required": False},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "50"},
        ],
    },
    # ── Screeners ──
    {
        "key": "stock_screener", "family": "Screeners", "label": "Stock Screener",
        "path": "/screener/stocks", "rows": "data",
        "help": "Stock screener (options volume, IV, OI-change filters).",
        "params": [
            {"name": "ticker", "label": "Ticker", "kind": "text", "required": False},
            {"name": "sectors", "label": "Sector", "kind": "text", "required": False},
            {"name": "min_marketcap", "label": "Min market cap", "kind": "float"},
            {"name": "max_marketcap", "label": "Max market cap", "kind": "float"},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
        ],
    },
    {
        "key": "option_screener", "family": "Screeners", "label": "Contract Screener (options)",
        "path": "/screener/option-contracts", "rows": "data",
        "help": "Screener for option contracts by a wide filter set.",
        "params": [
            {"name": "ticker_symbol", "label": "Ticker", "kind": "text", "required": False},
            {"name": "type", "label": "Type (call/put)", "kind": "text", "required": False},
            {"name": "min_dte", "label": "Min DTE", "kind": "int"},
            {"name": "max_dte", "label": "Max DTE", "kind": "int"},
            {"name": "min_premium", "label": "Min premium ($)", "kind": "float"},
            {"name": "limit", "label": "Limit", "kind": "int", "default": "100"},
        ],
    },
]


FAMILY_ORDER = [
    "Options flow", "Dark pool", "Greeks & volatility", "Short interest",
    "Congress & insiders", "Institutions", "Prediction markets", "Crypto",
    "Market & macro", "Screeners",
]

import re as _re

_BY_KEY = {t["key"]: t for t in TOOLS}
_KEYS = set(_BY_KEY)


def path_params(path: str) -> set[str]:
    """Placeholder names in a path, e.g. '/x/{ticker}/y' -> {'ticker'}."""
    return set(_re.findall(r"\{(\w+)\}", path))


def validate_catalog() -> list[str]:
    """Structural checks; returns a list of problem strings (empty = valid).

    Called by unit tests so a malformed catalog entry fails loudly instead of
    surfacing as a confusing empty board.
    """
    problems: list[str] = []
    seen: set[str] = set()
    for t in TOOLS:
        key = t.get("key")
        if not key or key in seen:
            problems.append(f"duplicate/missing key: {key!r}")
        seen.add(key)
        if t.get("family") not in FAMILY_ORDER:
            problems.append(f"{key}: unknown family {t.get('family')!r}")
        if not str(t.get("path", "")).startswith("/") or "/api" in str(t.get("path", "")):
            problems.append(f"{key}: path must be /api-free and start with '/': {t.get('path')!r}")
        if t.get("rows") not in ("data", "list", "object"):
            problems.append(f"{key}: rows must be data|list|object")
        path_placeholders = path_params(str(t.get("path", "")))
        names: set[str] = set()
        for p in t.get("params", []):
            n = p.get("name")
            if not n or n in names:
                problems.append(f"{key}: duplicate/missing param name {n!r}")
            names.add(n)
            if p.get("in_path") and n not in path_placeholders:
                problems.append(f"{key}: {n} marked in_path but not in path {t.get('path')!r}")
        for pp in path_placeholders:
            pnames = {p.get("name") for p in t.get("params", [])}
            if pp not in pnames:
                problems.append(f"{key}: path placeholder {{{pp}}} has no param entry")
    return problems


def tools_for_family(family: str) -> list[dict]:
    return [t for t in TOOLS if t.get("family") == family]


def get_tool(key: str) -> dict | None:
    return _BY_KEY.get(key)


def catalog_payload() -> dict:
    """{family: [tool…]} grouped, ready to embed in the board for the tool form."""
    return {f: tools_for_family(f) for f in FAMILY_ORDER}
