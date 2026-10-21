"""Configuration for the SPX Critical Thresholds module (Unusual Whales)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_REPO_ROOT / "RevenueChaser" / ".env")

# NB: no /api suffix here — callers pass paths that already carry it.
UW_API_BASE = os.getenv("RC_UW_BASE_ROOT") or "https://api.unusualwhales.com"
UW_API_TOKEN = os.environ.get("UNUSUAL_WHALES_API_KEY", "") or os.environ.get("UNUSUAL_WHALES_API_TOKEN", "")
UW_CLIENT_API_ID = "100001"
TICKER = "SPX"

# Module-scoped cache/export root (cached data + exported reports live under the
# module's own subdirectory). Overridable via SPX_GAMMA_DATA_ROOT so tests never
# touch the real tree.
DATA_ROOT = Path(
    os.environ.get("SPX_GAMMA_DATA_ROOT")
    or (Path(__file__).resolve().parent / "data")
)
BRIER_FILENAME = "brier.json"
HISTORY_FILENAME = "spx_gamma_gex_history.json"
RAW_ROOT = DATA_ROOT / "raw"
PARSED_ROOT = DATA_ROOT / "parsed"
CHART_ROOT = DATA_ROOT / "charts"
REPORT_ROOT = DATA_ROOT / "reports"

HTTP_TIMEOUT_SECONDS = 20
HTTP_MAX_RETRIES = 3

# SPX Critical Thresholds: 8-week price lookback window
LOOKBACK_WEEKS = 8

# Charm Pin qualification (deterministic)
CHARM_PIN_MAX_DISTANCE_PCT = 0.015   # peak within 1.5% of spot
CHARM_PIN_DOMINANCE_RATIO = 1.50     # top >= 1.5x runner-up |charm|

# Charm y-axis inclusion clamp (keep far-OTM peaks from flattening candles)
CHARM_AXIS_LIMIT_PCT = 0.05

# Spot-exposure strike search window (wide, +/-25% of spot)
SPOT_EXPOSURE_WINDOW_PCT = 0.25

# Tornado chart window around spot (signed Net GEX profile)
TORNADO_WINDOW_PCT = 0.12

# Expected-move IV sanity bounds (annualized decimal)
IV_MIN = 0.001
IV_MAX = 2.50

MAJOR_CONCENTRATION_MULTIPLE = 10.0
EXTREME_CONCENTRATION_MULTIPLE = 25.0

# ── Predictive scenario & directional engine ───────────────────────────────
# Directional vector score S_dir = w_gamma*Sign(NetGEX) + w_charm*(Charm_Term)
# + w_vanna*(Vanna_Term). Each term is normalized to [-1, 1] (see the engine),
# so the weights are interpretable shares of the composite.
S_DIR_W_GAMMA = 0.50   # Gamma Amplification Regime
S_DIR_W_CHARM = 0.30   # Charm Time-Decay Drift
S_DIR_W_VANNA = 0.20   # Vanna Volatility Sensitivity
S_DIR_BULLISH = 0.35   # S_dir >= +0.35 -> BULLISH DRIFT
S_DIR_BEARISH = -0.35  # S_dir <= -0.35 -> BEARISH BREAKDOWN

# Scenario extraction thresholds (dollar-denominated, spot-exposures scale).
PIN_MAGNET_MIN_DOLLARS = 10.0e9    # 0DTE pin >= $10B -> active price magnet
UPSIDE_TRIGGER_MIN_MAGNITUDE = 200.0e6  # |negative GEX| > $200M -> short-gamma squeeze trigger

# Pin/trigger geometry is extracted from an ATM window around spot so a far-away
# 0-1 DTE GEX peak can never masquerade as the active pin or an actionable
# trigger (global-max selection produced scenario bands disconnected from live
# spot, e.g. a "Consolidation 7,700-7,710" band while spot printed 7,631).
PIN_ATM_WINDOW_PTS = 25.0        # accept a 0-1DTE pin only within +/-25 pts of spot
TRIGGER_ATM_WINDOW_PTS = 50.0    # squeeze/support trigger walls within +/-50 pts of spot
CONSOLIDATION_BAND_PTS = 5.0     # neutral band = anchor +/- 5 pts

# ── Iron Butterfly "Pin Harvest" panel ──────────────────────────────────────
# Wing width is derived from the 1-day expected move, rounded to the strike grid
# and clamped to [IB_MIN_W, IB_MAX_W]; fixed candidate widths are priced for
# comparison. All widths are index points. SPX strikes step by 5 pts.
IB_STRIKE_STEP = 5.0
IB_MIN_W = 25.0
IB_MAX_W = 75.0
IB_CANDIDATE_WINGS = (25.0, 50.0)

# Confidence heuristic: how far beyond the S_dir band and how dominant the pin.
CONF_MARGIN_DISTANCE = 0.65   # S_dir distance at which confidence saturates to 100%
CONF_PIN_WEIGHT = 0.25        # share of confidence contributed by the 0DTE pin
CONF_PIN_DOMINANCE_AT_FULL = 3.0  # pin dominance ratio (top/2nd) reaching full pin confidence
CONF_PIN_AT_FULL_DISTANCE = 0.004  # pin distance (pct of spot) reaching full pin confidence


def to_float(v):
    """Coerce an API value to float; missing/NaN/invalid -> None (never 0)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN guard
        return None
    return f


def net_sum(call, put):
    """Net Greek field = call + put; None only when both are unavailable."""
    c = to_float(call)
    p = to_float(put)
    if c is None and p is None:
        return None
    return (c or 0.0) + (p or 0.0)
