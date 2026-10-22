"""FinancialModelEngine konfigürasyonu — API key, WACC/büyüme varsayımları, sektör bantları."""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

from RevenueChaser.core.config import OUTPUT_DIR, to_float

BASE_DIR = Path(__file__).resolve().parent
REVENUE_CHASER_ROOT = BASE_DIR.parent
load_dotenv(REVENUE_CHASER_ROOT / ".env")

UW_API_KEY = os.getenv("UNUSUAL_WHALES_API_KEY", "")

# ── Unusual Whales rate limit ──
UW_RATE_LIMIT_PER_MIN = 120
UW_RATE_LIMIT_PER_DAY = 15_000

# ── DCF / WACC varsayımları ──
RISK_FREE_RATE = 0.04        # 10y ABD tahvili vekili
EQUITY_RISK_PREMIUM = 0.05   # CAPM hisse risk primi
DEFAULT_BETA = 1.0
DEFAULT_TAX_RATE = 0.21
DEFAULT_COST_OF_DEBT = 0.05
TERMINAL_GROWTH = 0.025      # Gordon Growth g
DEFAULT_EXIT_MULTIPLE = 12.0  # EV/EBITDA exit
PROJECTION_YEARS = 5

# Projeksiyonda kullanılan varsayılan bantlar (override edilebilir)
DEFAULT_REVENUE_GROWTH = [0.08, 0.07, 0.06, 0.05, 0.04]
DEFAULT_EBITDA_MARGIN = 0.30
DEFAULT_CAPEX_PCT_REV = 0.05
DEFAULT_DNWC_PCT_REV = 0.00

# ── Comps peers (FIN-3): k comes from a measurement, never a guess ──
# A leave-one-out run over 3,365 names: k = 8 was the best of 3/5/8/12 on EV/EBITDA, EV/Revenue and P/E.
# The peers themselves are the spine universe's (module_financials/peers.py); there is no static list.
PEER_K = 8


# ── Sektör bazlı default bantlar (WACC / büyüme / marj) ──
# Keys are the Morningstar sector names, the same keys and numbers as config/sector_defaults.json (#266).
SECTOR_DEFAULTS_FILE = BASE_DIR / "config" / "sector_defaults.json"

SECTOR_DEFAULTS = {
    "default": {
        "wacc_range": (0.07, 0.12),
        "growth_range": (0.02, 0.10),
        "ebitda_margin_range": (0.15, 0.40),
        "exit_multiple_range": (8.0, 16.0),
    },
    "Technology": {
        "wacc_range": (0.08, 0.12),
        "growth_range": (0.05, 0.18),
        "ebitda_margin_range": (0.20, 0.45),
        "exit_multiple_range": (12.0, 22.0),
    },
    "Consumer Defensive": {
        "wacc_range": (0.06, 0.09),
        "growth_range": (0.01, 0.05),
        "ebitda_margin_range": (0.12, 0.30),
        "exit_multiple_range": (10.0, 18.0),
    },
    "Financial Services": {
        "wacc_range": (0.07, 0.11),
        "growth_range": (0.02, 0.08),
        "ebitda_margin_range": (0.25, 0.50),
        "exit_multiple_range": (8.0, 14.0),
    },
    "Energy": {
        "wacc_range": (0.07, 0.11),
        "growth_range": (0.00, 0.06),
        "ebitda_margin_range": (0.15, 0.40),
        "exit_multiple_range": (5.0, 10.0),
    },
    "Healthcare": {
        "wacc_range": (0.06, 0.10),
        "growth_range": (0.02, 0.10),
        "ebitda_margin_range": (0.20, 0.40),
        "exit_multiple_range": (10.0, 18.0),
    },
}


def load_sector_defaults() -> dict:
    """JSON dosyası varsa onu, yoksa gömülü SECTOR_DEFAULTS'u döndürür."""
    if SECTOR_DEFAULTS_FILE.exists():
        try:
            data = json.loads(SECTOR_DEFAULTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return SECTOR_DEFAULTS


def sector_defaults(sector: str | None) -> dict:
    """Sektör için default bant; bilinmeyen sektör 'default'a düşer."""
    defaults = load_sector_defaults()
    key = sector if sector in defaults else "default"
    return defaults[key]
