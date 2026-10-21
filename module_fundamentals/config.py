"""Fundamental Analysis configuration — thresholds, universe, ranking.

Quality-growth scan (3 methods: Rule of 40 / Quality / Momentum). Reuses the
Asymmetric Growth Hunter's broad US universe; market-cap filtering is applied
client-side via the universal filters.
"""
from __future__ import annotations

from RevenueChaser.core.config import OUTPUT_DIR

FUNDAMENTALS_DIR = OUTPUT_DIR
FUNDAMENTALS_LATEST = FUNDAMENTALS_DIR / "fundamentals_latest.html"
FUNDAMENTALS_JSON = OUTPUT_DIR / "fundamentals" / "fundamentals_latest.json"

# ── Phase-1 pre-filter (7-A: price and dollar volume on D04's quotes, before EDGAR) ──
# Every name that passes is scored and every candidate reaches the page (no cap, no top-N; #383 R3 C12, card C).
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 250_000

# ── Rule of 40 ──
RO40_MIN = 40.0

# ── Quality (x/3) ──
GROWTH_MIN = 0.20                    # TTM YoY revenue growth
ROIC_MIN = 0.15
GM_MIN = 0.30                        # gross margin
ND_EBITDA_MAX = 3.0                  # net debt / EBITDA

# ── Momentum (x/3) ──
DIST_52W_MAX = 0.25                  # within 25% of 52-week high
RS_6M_MIN = 0.0                      # 6-month RS vs SPY > 0 (pp)
ABOVE_200D = True                    # price > 200-day MA

# ── Data-health guardrail ([DISTORTED] -> manual review) ──
DISTORTED_GROWTH = 2.0               # abs(TTM YoY growth) > 200%
DISTORTED_REV_MIN = 10_000_000       # TTM revenue base < $10M

# ── Data-health gates (#386 F1) ──
# A spine sector that is not comparable on a quality-growth scan (card 4 (b), Common C17): the Financials and
# Real Estate business models (balance-sheet driven) leave the universe before any EDGAR read.
EXCLUDED_SECTORS = ["Financial Services", "Real Estate"]
# A Rule of 40 above this is a data problem, not a pick (#386 F1).
RO40_BARRIER = 150.0

# ── ROIC ──
TAX_RATE = 0.21

# ── Forecast ledger (#128) ──
# Bump by hand when the scoring logic changes in a way the thresholds above do not show. The
# ledger stamps every frozen row with "<SELECTION_VERSION>+<hash of the thresholds>"; a golden test
# (tests/test_fundamentals_freeze_128.py) fails when a threshold changes without this bump.
# 6: data-health gates (edgar_fetch_failed, stale_annual, sector_excluded, ro40_barrier) and the F2 scoring basis
#    (#386)
SELECTION_VERSION = "6"

# ── Options flow (constitutional bypass) ──
MANUAL_FLOW_ONLY = True
