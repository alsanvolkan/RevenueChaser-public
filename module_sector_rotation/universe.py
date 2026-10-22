"""Universe for the RRG rotation engine.

- ``ROTATION_ETFS`` — 83 symbols: 72 ETFs plus 11 macro indicators
  (yield curve + volatility/commodities). The 72 ETFs run through the
  SPY-relative rotation engine (Z-score, quadrants, trajectory r/theta,
  money flow); macro indicators are shown in the separate macro panel.
"""

# ── 41 ETFs for the RRG rotation pipeline ──
ROTATION_ETFS = {
    # ── Sectors (11) ──
    # The names are the Morningstar sector names, as stored in ``equity_universe.sector`` (Phase 1, #266):
    # a sector row finds its industry tree (``engine.sector_etf_trees``) by this name.
    "XLC":  {"name": "Communication Services", "group": "sector"},
    "XLE":  {"name": "Energy", "group": "sector"},
    "XLF":  {"name": "Financial Services", "group": "sector"},
    "XLI":  {"name": "Industrials", "group": "sector"},
    "XLB":  {"name": "Basic Materials", "group": "sector"},
    "XLK":  {"name": "Technology", "group": "sector"},
    "XLP":  {"name": "Consumer Defensive", "group": "sector"},
    "XLRE": {"name": "Real Estate", "group": "sector"},
    "XLU":  {"name": "Utilities", "group": "sector"},
    "XLV":  {"name": "Healthcare", "group": "sector"},
    "XLY":  {"name": "Consumer Cyclical", "group": "sector"},

    # ── Thematic & Industry (23) ──
    "AIQ":  {"name": "AI & Technology", "group": "theme"},
    "ARKQ": {"name": "Autonomous Tech", "group": "theme"},
    "IGV":  {"name": "Software", "group": "theme"},
    "ROBO": {"name": "Robotics", "group": "theme"},
    "SKYY": {"name": "Cloud Computing", "group": "theme"},
    "SMH":  {"name": "Semiconductors", "group": "theme"},
    "SOXX": {"name": "Semiconductors", "group": "theme"},
    "WCLD": {"name": "SaaS / Cloud", "group": "theme"},
    "AIRR": {"name": "Industrial Robotics", "group": "theme"},
    "ITA":  {"name": "Aerospace & Defense", "group": "theme"},
    "PAVE": {"name": "Infra / Reshoring", "group": "theme"},
    "GLD":  {"name": "Gold", "group": "theme"},
    "LIT":  {"name": "Lithium / Battery", "group": "theme"},
    "NLR":  {"name": "Uranium / Nuclear", "group": "theme"},
    "SLV":  {"name": "Silver", "group": "theme"},
    "URA":  {"name": "Uranium Miners", "group": "theme"},
    "USO":  {"name": "Oil", "group": "theme"},
    "XME":  {"name": "Metals & Mining", "group": "theme"},
    "XOP":  {"name": "Oil & Gas Exploration", "group": "theme"},
    "IBB":  {"name": "Large-Cap Biotech", "group": "theme"},
    "XBI":  {"name": "Biotechnology", "group": "theme"},
    "JETS": {"name": "Airlines", "group": "theme"},
    "KRE":  {"name": "Regional Banks", "group": "theme"},
    "HACK": {"name": "Cyber Security", "group": "theme"},
    "XHB":  {"name": "Homebuilders", "group": "theme"},
    "XRT":  {"name": "Retail", "group": "theme"},
    "ARKW": {"name": "Next Gen Internet", "group": "theme"},
    "IHI":  {"name": "Medical Devices", "group": "theme"},

    # ── Asset Class, Regional (5) ──
    "EEM":  {"name": "Emerging Markets", "group": "region"},
    "IWM":  {"name": "US Small Caps", "group": "region"},
    "HYG":  {"name": "High Yield Credit", "group": "bond"},
    "LQD":  {"name": "Investment Grade Credit", "group": "bond"},
    "TLT":  {"name": "20+ Year Treasury", "group": "bond"},

    # ── Countries (single-country ETFs) ──
    "EWZ":  {"name": "Brazil", "group": "region"},
    "EWY":  {"name": "South Korea", "group": "region"},
    "FXI":  {"name": "China Large-Cap", "group": "region"},
    "INDA": {"name": "India", "group": "region"},
    "EWT":  {"name": "Taiwan", "group": "region"},
    "EWC":  {"name": "Canada", "group": "region"},
    "EWA":  {"name": "Australia", "group": "region"},
    "EWJ":  {"name": "Japan", "group": "region"},
    "EWU":  {"name": "United Kingdom", "group": "region"},
    "MCHI": {"name": "China", "group": "region"},
    "EWG":  {"name": "Germany", "group": "region"},
    "EWW":  {"name": "Mexico", "group": "region"},
    "VNM":  {"name": "Vietnam", "group": "region"},
    "EWS":  {"name": "Singapore", "group": "region"},
    "EIDO": {"name": "Indonesia (iShares)", "group": "region"},
    "ECH":  {"name": "Chile", "group": "region"},
    "EWP":  {"name": "Spain", "group": "region"},
    "KSA":  {"name": "Saudi Arabia", "group": "region"},
    "EWM":  {"name": "Malaysia", "group": "region"},
    "EPOL": {"name": "Poland", "group": "region"},
    "THD":  {"name": "Thailand", "group": "region"},
    "EEMV": {"name": "Emerging Markets Min Vol", "group": "region"},
    "EZA":  {"name": "South Africa", "group": "region"},
    "TUR":  {"name": "Turkey", "group": "region"},
    "ARGT": {"name": "Argentina", "group": "region"},
    "EPHE": {"name": "Philippines", "group": "region"},
    "IDX":  {"name": "Indonesia (VanEck)", "group": "region"},
    "EPU":  {"name": "Peru", "group": "region"},

    # ── Macro indicators mapped INTO the RRG pipeline ──
    # (weekly closes sourced from FRED/yfinance, not UW OHLC)
    "US02Y":       {"name": "US 2-Year Treasury Yield", "group": "yield"},
    "US05Y":       {"name": "US 5-Year Treasury Yield", "group": "yield"},
    "US10Y":       {"name": "US 10-Year Treasury Yield", "group": "yield"},
    "US20Y":       {"name": "US 20-Year Treasury Yield", "group": "yield"},
    "US30Y":       {"name": "US 30-Year Treasury Yield", "group": "yield"},
    "US10Y-US02Y": {"name": "10Y-2Y Yield Spread", "group": "yield"},
    "USIRYY":      {"name": "US Inflation Breakeven", "group": "yield"},
    "CL1!":        {"name": "Crude Oil Futures (WTI)", "group": "commodity"},
    "MOVE":        {"name": "ICE BofA MOVE Index", "group": "commodity"},
    "VVIX":        {"name": "VIX of VIX", "group": "commodity"},
    "WHEATF":      {"name": "Wheat Futures", "group": "commodity"},
}


# Per-symbol data resolution for RRG-mapped macro symbols: FRED id / yf proxy.
MACRO_SERIES = {
    "US02Y":       {"fred": "DGS2",  "yf": "2YY=F"},
    "US05Y":       {"fred": "DGS5",  "yf": "^FVX"},
    "US10Y":       {"fred": "DGS10", "yf": "^TNX"},
    "US20Y":       {"fred": "DGS20", "yf": None},
    "US30Y":       {"fred": "DGS30", "yf": None},
    "US10Y-US02Y": {"fred": "T10Y2Y", "yf": None},
    "USIRYY":      {"fred": "T10YIE", "yf": None},
    "CL1!":        {"fred": "DCOILWTICO", "yf": "CL=F"},
    "MOVE":        {"fred": None, "yf": "^MOVE"},
    "VVIX":        {"fred": None,     "yf": "^VVIX"},  # FRED VIXCLS is the VIX itself; FRED has no VVIX
    "WHEATF":      {"fred": None, "yf": "ZW=F"},
}


TICKER_ETF_MAP = {
    "NET":  [("IGV", 0.45), ("WCLD", 0.35), ("SKYY", 0.20)],
    "DDOG": [("IGV", 0.45), ("WCLD", 0.40), ("SKYY", 0.15)],
    "CRWD": [("HACK", 0.50), ("IGV", 0.30), ("SKYY", 0.20)],
    "SMCI": [("SMH", 0.70), ("SOXX", 0.20), ("XLK", 0.10)],
    "AAOI": [("SMH", 0.55), ("SOXX", 0.30), ("XLK", 0.15)],
    "RKT":  [("XLF", 0.60), ("KRE", 0.15), ("XHB", 0.25)],
    "ABNB": [("XLY", 0.55), ("XRT", 0.30), ("JETS", 0.15)],
}

# Country flag emoji per single-country ETF (rendered on the RRG map + table).
COUNTRY_FLAGS = {
    "EWZ": "🇧🇷", "EWY": "🇰🇷", "FXI": "🇨🇳", "INDA": "🇮🇳", "EWT": "🇹🇼",
    "EWC": "🇨🇦", "EWA": "🇦🇺", "EWJ": "🇯🇵", "EWU": "🇬🇧", "MCHI": "🇨🇳",
    "EWG": "🇩🇪", "EWW": "🇲🇽", "VNM": "🇻🇳", "EWS": "🇸🇬", "EIDO": "🇮🇩",
    "ECH": "🇨🇱", "EWP": "🇪🇸", "KSA": "🇸🇦", "EWM": "🇲🇾", "EPOL": "🇵🇱",
    "THD": "🇹🇭", "EEMV": "🌐", "EZA": "🇿🇦", "TUR": "🇹🇷", "ARGT": "🇦🇷",
    "EPHE": "🇵🇭", "IDX": "🇮🇩", "EPU": "🇵🇪",
}

GROUP_COLORS = {
    "sector": "#58a6ff",
    "theme": "#d2991d",
    "bond": "#f85149",
    "region": "#a371f7",
    "yield": "#22d3ee",
    "commodity": "#34d399",
}

QUADRANT_COLORS = {
    "LEADING": "#3fb950",
    "WEAKENING": "#d2991d",
    "LAGGING": "#f85149",
    "IMPROVING": "#58a6ff",
}
