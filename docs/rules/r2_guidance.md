# R2 — EPS Beat Track Record vs the Street

**Weight:** 20% of composite direction score
**Source:** Unusual Whales `/api/earnings/{ticker}` — historical EPS data
**File:** `module_earnings_radar/rules/rule2_guidance.py`

## What It Measures

Despite the legacy name "Guidance", R2 scores **consensus-EPS surprises** — how a
company's reported EPS has done against the street estimate over its recent
reports. A serial beater signals conservative guidance and disciplined
execution; a serial misser is a negative-surprise risk. It does **not** read
management guidance language, and it deliberately does **not** use price
reaction (`post_earnings_move_*`), which belongs to volatility/edge scoring.

## Data Input

UW's earnings history endpoint (`/api/earnings/{ticker}`) returns per-event:
- `actual_eps` — reported EPS
- `street_mean_est` — consensus estimate before the report
- `report_date` — when earnings were released (used to order the history)
- price/straddle fields (`post_earnings_move_*`, `expected_move_perc`, …) — **not used here**

No revenue estimates exist on this endpoint.

## Scoring Logic

### Preparation (robust + recency-aware)

1. **Usable rows** = events with a valid `actual_eps` and non-zero estimate.
2. Rows are sorted by `report_date` when every row has one; a partially-dated
   series keeps the provider's order (never re-sorted into a lie).
3. Only the **last 8 completed prints** (`MAX_WINDOW`) steer the score, so a
   great 2019 no longer carries a 2026 setup.
4. Each surprise `(actual − estimate) / |estimate|` is **winsorized to ±50%**
   (`SURPRISE_CLIP`), so one special item or an estimate near zero cannot
   dominate the mean.

### Core Metrics (over the window)

```
avg_bias  = mean(winsorized surprise)          # capped at ±50% per event
beat_rate = fraction of prints where actual > estimate
```

### Score Components

| Condition | Score | Reason |
|-----------|:-----:|--------|
| `avg_bias > 5%` | **+30** | Strong beat history |
| `avg_bias > 2%` | **+15** | — |
| `avg_bias < -5%` | **−30** | Consistent miss |
| `beat_rate ≥ 75%` | **+15** | High beat frequency |
| `beat_rate < 40%` | **−15** | Low beat frequency |
| Recent-4Q avg bias ≥ earlier +2pp (`TREND_DELTA`) | **+10** | Beat trend improving |
| Recent-4Q avg bias ≤ earlier −2pp | **−15** | Beat trend declining |

**Trend term:** needs ≥2 usable quarters in the recent half (last 4) **and** ≥2
in the earlier half of the 8-print window. When either side is missing the term
is skipped — no data never means a penalty.

### Coverage (feeds composite confidence)

| Usable quarters in window | Coverage |
|---------------------------|:--------:|
| 3–4 | 0.40 |
| 5–7 | 0.60 |
| 8+ | 0.80 |

### Availability

Below **3 usable quarters** the rule is `unavailable` (score `None`, coverage 0)
— it never contributes a zero fill, so a young ticker is not pulled toward
neutral by silence.

## Example

Eight completed prints, estimate held at 1.00; oldest four beat by 3%,
newest four beat by 8%:

| Metric | Value |
|--------|-------|
| `avg_bias` | 5.5% → **+30** |
| `beat_rate` | 100% → **+15** |
| Trend (recent 8% vs earlier 3%) | ≥ 2pp → **+10** |
| **Score** | **55** (coverage 0.80) |

Contrast: if that +8% recency flipped to −1% while the earlier half stayed
+10%, the trend term adds −15 — a serial beater whose beats are fading scores
worse than its all-time average would suggest.

## Interpretation

| Score | Signal |
|:-----:|--------|
| > 0 | Recent history tends to beat the street |
| ≈ 0 | Mixed, fading, or offsetting record |
| < 0 | Recent history tends to miss — negative-surprise risk |

Winsorization keeps magnitude sane (a single ±400% event does not rewrite the
mean), and the window + trend term make the rule *current* rather than a
lifetime average.
