# R4 — Options Volatility Edge (Direction-Free)

**Weight:** 0% of composite direction score (edge/gate only)
**Source:** UW calendar expected move, screener `iv30d` / `iv_rank`, Yang-Zhang RV30 (offline from the OHLC the pipeline already fetches), `/stock/{ticker}/volatility/term-structure`, `gex-levels`, `/earnings/{ticker}` history (long-straddle payoffs)
**File:** `module_earnings_radar/rules/rule4_volatility.py` (+ `core/vol_metrics.py`)

## What It Measures

R4 never votes bull or bear. It answers the vehicle question for a card that is
already directional: **should the trade be expressed with options, or with
shares / defined risk?** A high vol-edge means "options are a favourable
instrument at this expected move", which is the R4 input to `select_action`.

It is **not** the "Edge" column — that display value is median realized 1D move
÷ current implied move, computed separately.

## Inputs

| Input | Source | Role |
|---|---|---|
| Expected move (%) | earnings calendar / history | EM buckets |
| Implied vol | screener row `iv30d` | IV/RV numerator |
| Realized vol | **Yang-Zhang RV30** offline from the fetched OHLC bars (fallback: screener `realized_volatility`) | IV/RV denominator |
| Term-structure curve | `/stock/{ticker}/volatility/term-structure` | IV/RV context + slope term |
| Volume-book GEX profile | `/stock/{ticker}/spot-exposures/strike` (Directionalized Volume) | regime, magnet, charm, vanna |
| `gamma_flip` + spot | `gex-levels` + screener close | GEX regime fallback |
| Long-straddle 1D payoffs | `/earnings/{ticker}` history (last 4 completed) | history term |
| (fallback) realized 1D moves | same history | realized-vs-implied ratio |
| `iv_rank` | screener row | reason only |

## Scoring (0–100)

| Signal | Condition | Points |
|---|---|---|
| Expected move | > 12% | +25 |
| | > 7% | +15 |
| | > 4% | +5 |
| | < 2% | −10 |
| IV/RV (both real) | < 1.0 (cheap) | +10 |
| | ≥ 1.25 (rich) | −5 |
| Term-structure slope | ≤ −0.00406/day (backwardated) | −8 |
| | (−0.00406, −0.002]/day (mild backwardation) | −3 |
| | > 0/day (contango) | +5 |
| GEX block (clamped **+20 / −15**) | | |
| · regime (volume book) | EM-band net gamma red (short) | +15 |
| | EM-band net gamma green (long) | −10 |
| · magnet near spot (≤5%) | magnet below spot | +8 |
| | magnet above spot | −8 |
| · charm / vanna / far walls | | reason only, no points |
| History | straddles paid (median > 0) | +10 |
| | straddles lost (median < 0) | −10 |
| | fallback realized/implied > 1.2× | +10 |
| | fallback realized/implied < 0.8× | −5 |

The GEX **regime** is read from the directionalized-volume strike profile first:
the sign of net gamma inside the expected-move band around spot (heatmap
semantics — red band = dealers amplify a gap, green = they damp it). A stale
`gex-levels` flip that sits on the wrong book (e.g. an OI flip far below spot)
must not relabel the regime, so the profile wins when present; the `gex-levels`
flip is the fallback. Near-spot magnet proximity adds a small directional
nudge; walls, charm (Pin vs Peak) and vanna lobes are surfaced as reasons and
feed the structure target band, never extra points.

Richness is handled once: high IV/RV is a **penalty** for a premium buyer (the
market has already marked vol up), and high `iv_rank` is informational only —
never stacked as extra "+edge".

### Yang-Zhang RV30 + term-structure slope (carried from uw-earnings-vol-scan)

The IV/RV richness read uses the exact construction the skill's thresholds were
calibrated against: today's ATM IV (`iv30d`) over **Yang-Zhang RV30** — realized
vol over the trailing 30 trading days, annualised by `sqrt(251)` (Juneteenth
made the US trading year 251 days), computed offline from the OHLC bars the
pipeline already fetches (`core/vol_metrics.yang_zhang`). Any bar dated
today-or-later is dropped so a still-forming bar never feeds RV (the skill's
`bars_through` rule). If too few clean bars exist R4 falls back to the screener's
`realized_volatility` exactly as before.

The term-structure slope is the per-day ATM-IV change from the nearest
expiration out to ~45 DTE (`core/vol_metrics.term_structure_slope`, one curve
from `/stock/{ticker}/volatility/term-structure`; mirror the skill — never
interpolate a fake far end). For a **premium buyer** the polarity is inverted
from the skill's calendar-seller filter: backwardation (front IV rich vs the
back) is a −8 drag, mild backwardation −3, contango (front-dated vol cheap)
+5. Slope points are scored but **do not enter the coverage denominator** —
coverage stays the shipped four-part set below.

### Honesty rules

- **No fake spot.** If no real price is known the GEX term is skipped; there is
  no invented `$100` regime.
- **No fake zeros.** Missing IV/RV/straddle fields are `None`; a ratio is only
  computed when both sides are real and RV > 0.
- **Profile is additive.** If the strike-exposure call is disabled or fails, R4
  falls back to `gex-levels` exactly as before — the volume profile never
  fabricates strikes and never blocks a score.
- **RV source is additive.** Yang-Zhang RV30 is computed offline from bars the
  pipeline already fetched; no extra call. It is used only when it exists;
  otherwise the screener RV stands.
- **Slope is additive.** The term-structure fetch is one extra call per
  deep-scored ticker behind `R4_TERM_STRUCTURE_ENABLED`; disabled, failed, or a
  curve that cannot span near → 45 DTE yields `None` — no slope, never a
  fabricated curve.
- **Coverage** = fraction of {EM, IV/RV, GEX, history} that actually fired
  (e.g. EM alone → 0.25). R4's coverage never feeds the direction confidence.

## Gate into `select_action` (live)

| | |
|---|---|
| |direction| ≥ 25 **and** vol-edge ≥ 50 | `OPTIONS_BULL` / `OPTIONS_BEAR` |
| |direction| ≥ 18 (R4 did not pass) | `SHARES_BULL` / `SHARES_BEAR` |

Without an expected move R4 is unavailable (vol-edge 0) and the card can never
be an options candidate.

## Intended vs live

The **intended** action ladder (`docs/rules/00_overview.md`) was stricter and
signed: `|direction| ≥ 45` and vol-edge `≥ 60` → `BULLISH_OPTIONS_SETUP` /
`BEARISH_OPTIONS_SETUP`, and `|direction| ≥ 45` → shares. The **live** ladder
above uses 25 / 50 thresholds and the shorter `OPTIONS_*` / `SHARES_*` names —
code is authoritative for what runs today.
