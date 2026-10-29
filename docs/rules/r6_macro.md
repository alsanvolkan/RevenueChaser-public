# R6 — Macro (veto only)

**Weight:** 0% of composite direction score (never votes bull/bear)
**Sources:** `DGS10` (source `fred`) and the `FOMC_DECISION` / `CPI_RELEASE` days from the market store's `market_series` (G9; no FRED call from the board) + ticker OHLC already read for the card
**File:** `module_earnings_radar/rules/rule6_macro.py`

## What It Does

Blocks the trade when the name is **extremely rate-sensitive**. A veto sets
`final_action = NO_TRADE` and the `MACRO VETO` badge. The numeric score is
audit-only (same pattern as R4's vol-edge).

## Live input — 10Y yield beta

Once per earnings `run()`, the stored `DGS10` series (the last 150 days, `store_reads.dgs10_map`) is read; it is empty when its newest observation is more than 3 sessions old. Each
ticker's already-fetched ~60d OHLC is paired on calendar date. OLS beta of
stock simple returns vs Δ yield, with FRED percent converted to **decimal**,
so β ≈ equity duration (−10 ≈ 10% price drop per 1pp yield rise).

| |β| | Effect |
|:---:|--------|
| > 4 | **veto** `extreme rate sensitivity` |
| > 2 | reason only — does not block |
| else | measured, no penalty |

Needs ≥ 20 paired returns and nonzero yield variance. Otherwise R6 is
`unavailable` (`score=None`, never a 0 fill).

No stored series, or a stale one → empty map → R6 unavailable; the earnings run
continues. There is no yfinance `^TNX` fallback (`^TNX` is yield×10).

Duration > 4 is common (software, REITs, utilities). This will veto more cards
than the previous dead gate. Thresholds match the original ladder.

## Not wired

FX tailwind / FX exposure, regulatory / geopolitical text, consensus crowding.
Optional kwargs still exist for a later pass; `regulatory_risk="low"` is not a
signal. Pipeline passes **only** `rate_sensitivity` (the 10Y beta).

## Coverage

1.0 when the yield-beta slot is present, else 0. Not divided by empty FX/reg slots.
