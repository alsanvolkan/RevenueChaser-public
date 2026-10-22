"""Reference data for the RRG rotation board, in a neutral home (#12).

``ROTATION_ETFS`` and ``MACRO_SERIES`` describe *what* the rotation engine scans, not *how*
the Sector Rotation board renders it, so they live here rather than in
``module_sector_rotation/universe.py``. That module re-exports both names, so the board and
its tests keep reading them from where they always did, and a test that patches the dict in
place still patches the one object every reader holds.

Nothing here imports another module: a reference table that the price writer, the taxonomy
and the earnings radar all read must not drag a board in behind it.
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
    "US20Y":       {"fred": "DGS20", "yf": "^TYX"},
    "US30Y":       {"fred": "DGS30", "yf": "^TYX"},
    "US10Y-US02Y": {"fred": "T10Y2Y", "yf": None},
    "USIRYY":      {"fred": "T10YIE", "yf": "TIP"},
    "CL1!":        {"fred": "DCOILWTICO", "yf": "CL=F"},
    "MOVE":        {"fred": None, "yf": "^MOVE"},
    "VVIX":        {"fred": None,     "yf": "^VVIX"},  # FRED VIXCLS is the VIX itself; FRED has no VVIX
    "WHEATF":      {"fred": None, "yf": "ZW=F"},
}
