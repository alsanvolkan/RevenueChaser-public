"""Implied Probability (OIPD) board configuration.

Per-ticker board: read one stored option chain (the options store, SPX and the
option watch list), fit an SVI volatility smile per expiry, and transform the
fitted surface into a risk-neutral price distribution. All knobs live here so
the fit is testable rather than magic.
"""
from __future__ import annotations

from RevenueChaser.core.config import OUTPUT_DIR

# Regenerable artifacts (flat html + namespaced json), written via runners.
OIPD_LATEST = OUTPUT_DIR / "oipd_latest.html"
OIPD_JSON = OUTPUT_DIR / "oipd" / "oipd_latest.json"

# ── Defaults ────────────────────────────────────────────────────────────────
# Default chosen, change if avo prefers: SPX is always on the option list (G9), SPY need not be.
DEFAULT_TICKER = "SPX"
# Risk-free proxy carried from the Financials DCF (10y US Treasury stand-in).
RISK_FREE_RATE = 0.04

# ── Expiry selection ────────────────────────────────────────────────────────
MIN_DTE = 1          # 0DTE is excluded: intraday time value breaks the T model
MAX_DTE = 365        # one-year horizon (matches the OIPD demo's "12m")
MAX_EXPIRIES = 12    # cap the fit cost; the highest-OI expiries win
MIN_CONTRACTS = 6    # an expiry needs at least this many usable strikes to fit

# ── SVI fit ─────────────────────────────────────────────────────────────────
# Butterfly-arbitrage guard: penalise g(k) below this floor during the fit.
G_FLOOR = 0.0
# The board greys its statistics when the density window clips more than this share (percent of mass).
CLIP_FLAG_PCT = 0.01
PENALTY_WEIGHT = 25.0
