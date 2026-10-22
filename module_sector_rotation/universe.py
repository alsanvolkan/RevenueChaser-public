"""Universe for the RRG rotation engine.

- ``ROTATION_ETFS`` — 78 symbols run through the SPY-relative rotation engine
  (Z-score, quadrants, trajectory r/theta, money flow). Includes 67 ETFs plus
  11 macro indicators (yield curve + volatility/commodities) sourced weekly
  from FRED/yfinance via ``MACRO_SERIES``.

Both are reference data, not a board's own, so they live in ``core/rotation_reference.py``
(since #12) and are re-exported here: ``core/prices.py``, ``module_relatedness`` and
``module_earnings_radar`` read them, and a board module must not be their owner. The
re-export is deliberate, not tidiness — these names are the objects the reference module
holds (a test that patches one in place patches what every reader sees), and this module
still binds both (a test that rebinds ``engine.ROTATION_ETFS`` or ``store.MACRO_SERIES``
keeps working).
"""
from RevenueChaser.core.rotation_reference import MACRO_SERIES, ROTATION_ETFS  # noqa: F401

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
