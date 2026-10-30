"""Swing Screener configuration.

Thresholds, ranking weights, and the manual-review options-flow flag. The scan
universe is the Asymmetric Growth Hunter's broad US universe; market-cap
filtering is applied client-side (same buckets as the Growth Hunter).
"""
from __future__ import annotations

from pathlib import Path

from RevenueChaser.core.config import OUTPUT_DIR

SWING_LATEST = OUTPUT_DIR / "swing_potential_latest.html"
SWING_JSON = OUTPUT_DIR / "swing_potential" / "swing_potential_latest.json"
OHLC_TTL_DAYS = 1.0                    # reuse cached OHLC within a day (yfinance rate-limit relief)

# ── Phase-1 pre-filter (7-A: price and dollar volume on D04's quotes) ──
# Not a market-cap filter — that is applied client-side like the Growth Hunter. Every name that passes is scored
# and every LONG setup reaches the page (no cap, no top-N; #383 R3 C12, card C).
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 250_000          # avg daily $ volume (price × avg volume)

# ── Gate thresholds ──
RS_MIN = 70                          # RS rating: 0-100 percentile, among the names scanned, of the 12-month
#                                      return vs SPY; ranks only (a quality point), never decides (#65)
ABOVE_52W_LOW = 1.25                 # price ≥ 25% above 52-week low
WITHIN_52W_HIGH = 0.75               # price within 25% of 52-week high
VOL_CONFIRM = 1.5                    # breakout volume ≥ 1.5× 20-day avg
VOL_WINDOW = 5                       # ... on any of the last 5 sessions (Swing review finding 3)
VOL_AVG_DAYS = 20                    # each against the 20 sessions before it, itself excluded
BREAKOUT_HIGH_PCT = 0.98             # price ≥ 98% of 52w high (overhead breakout)
MOVE_MIN = 0.25                      # preceding move ≥ 25% over 1-3 months
ADR_MIN = 0.03                       # average daily range ≥ 3%

# ── Ranking ──
W_12_1 = 0.50                        # JT 12-1 momentum weight
W_PROX = 0.25                        # 52-week proximity weight
W_CONS = 0.25                        # trend consistency weight
CONSISTENCY_LOOKBACK = 126           # ~6 months of trading days

# ── Quality (40/30/30, #65 / #384 SW-S1) ──
# Rank = 40 × Minervini rules passed/8 + 30 × Weinstein/5 + 30 × Qullamaggie/6. The old ``composite``
# (W_12_1 / W_PROX / W_CONS above) is the momentum tie-break.
Q_WEIGHTS = {"minervini": 40, "weinstein": 30, "qullamaggie": 30}
Q_RULE_TOTALS = {"minervini": 8, "weinstein": 5, "qullamaggie": 6}

# ── Execution levels (ATR-based) ──
STOP_ATR = 2.0                       # stop = entry − 2×ATR(14)
TARGET_R = 2.0                       # target = entry + 2×risk  (R:R 2.0)
ATR_PERIOD = 14

# ── Settings bounds (#384 SW-S4) ──
# ``core/board_settings.py`` reads these for the Settings board's editable thresholds: the default is the
# constant above, read at call time, and only a user's saved value lives in the store until Reset deletes it.
# Shares are entered as shares (0.25 = 25%), so the range says what the engine compares against.
MIN_PRICE_LO, MIN_PRICE_HI = 0.5, 100.0                     # $ per share; under $5 is the penny end
MIN_DOLLAR_VOLUME_LO, MIN_DOLLAR_VOLUME_HI = 0, 50_000_000  # $/day; 0 = no liquidity floor
# The board's type filter (PR 2): 1 screens operating companies only (the set it read before the universe became
# Tiers A and B whole), 0 every type.
OPERATING_ONLY = 1
RS_MIN_LO, RS_MIN_HI = 0.0, 100.0                           # RS rating is a 0-100 percentile
VOL_CONFIRM_LO, VOL_CONFIRM_HI = 0.5, 10.0                  # multiple of the 20-day average
BREAKOUT_HIGH_PCT_LO, BREAKOUT_HIGH_PCT_HI = 0.5, 1.0       # share of the 52-week high
MOVE_MIN_LO, MOVE_MIN_HI = 0.0, 3.0                         # share over 1-3 months
ADR_MIN_LO, ADR_MIN_HI = 0.0, 0.25                          # average daily range share
W_PROX_LO, W_PROX_HI = 0.0, 1.0                             # rank weights (W_PROX + W_CONS <= 1, see ranking)
W_CONS_LO, W_CONS_HI = 0.0, 1.0
STOP_ATR_LO, STOP_ATR_HI = 0.5, 10.0                        # stop distance in ATRs
TARGET_R_LO, TARGET_R_HI = 0.5, 20.0                        # target in multiples of the risk

# ── Forecast ledger (#128) ──
# Bump by hand when the scoring logic changes in a way the thresholds above do not show. The
# ledger stamps every frozen row with "<SELECTION_VERSION>+<hash of the thresholds>"; a golden test
# (tests/test_swing_freeze_128.py) fails when a threshold changes without this bump.
SELECTION_VERSION = "5"   # 5: Weinstein volume = best of the last 5 sessions vs the 20 before each (review 3, PR 6);
#                           4: 40/30/30 quality, direction required, RS ranks only, method toggles (#384 SW-S1);
#                           3: the whole universe on D01 bars, returns adjusted, 7-A prefilter, no cap or top-N (#383 R3)
#                           C12; 2: mom_12_1 is the true 12-1 return (#153 U15)

# ── Options flow (constitutional bypass) ──
MANUAL_FLOW_ONLY = True
