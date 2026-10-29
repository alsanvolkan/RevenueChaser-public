# R5 — Positioning & Short Squeeze

**Weight:** 20% of composite direction score
**Sources:** Unusual Whales `/stock/{ticker}/flow-recent` (options flow) and `/short_screener` (batch short interest / borrow / float)
**File:** `module_earnings_radar/rules/rule5_positioning.py`

## What It Measures

Who is positioned how, and what does that imply for the stock's reaction to earnings? Two independent components, **at least one of which must be present** or the rule is `unavailable` (score `None`, never a 0 fill):

1. **Institutional flow asymmetry** — are institutions net buyers or sellers of options?
2. **Squeeze potential** — is there elevated short interest / borrow cost / days to cover creating squeeze risk, measured **within the ticker's own sector**? There is no utilization: UW's short screener serves none and redesign.md R5 says never invent one (the old `short_interest / (short_interest + short_shares_available)` proxy is gone, #256).

> Retail-call euphoria is **not** scored here. The same `retail_call / (retail_call + inst_call) > 65%` fade is R8's job; scoring it in both R5 and R8 double-faded retail and effectively gave one signal ~30% weight.

## Data Input

`aggregate_flow()` buckets per-print option premium by size (≥ 10 contracts → institutional, else retail). Short data comes from the UW **short screener** batch endpoint (same row shape as V2 `/shorts/{ticker}/interest-float/v2`), fetched once per ~100 deep-scored tickers. R5 uses:

| Metric | Field | Notes |
|--------|-------|-------|
| SI % | `si_float` | decimal → ×100 when `< 1` |
| Borrow | `fee_rate` | hard-to-borrow cost |
| Days to cover | `days_to_cover` | |

Missing fields are `None`, never a 0 fill — an absent borrow fee must not read as "cheap to borrow".

## Scoring Logic

### Institutional Flow

```
score += 20 × inst_net / (inst_call + inst_put)     # needs premium > 0
fallback: +20 / −20 when a non-zero inst_net has no premium breakdown
```

A one-sided but small imbalance scores less than a clean sweep. Zero / empty institutional flow is not a signal.

### Squeeze (peer Z within sector)

```
squeeze_z = (0.4·Z(si) + 0.3·Z(borrow) + 0.1·Z(dtc))
            / (sum of weights actually present)
```

Z-scores are computed against the ticker's **own sector** peers (never cross-sector borrow comparisons). Weights renormalize over whatever metrics are present.

| Squeeze Z | Score | Signal |
|:---------:|:-----:|--------|
| > 3 | **+40** | Extreme squeeze potential |
| > 2 | +25 | Elevated squeeze potential |
| > 1 | +15 | Mild squeeze risk |
| < −1 | −10 | No squeeze pressure |

**Peer gate:** a Z requires ≥ 2 in-sector peers. With fewer than 2 (or no sector), an **absolute short-interest fallback** applies instead — `> 20% → +15`, `> 10% → +5`. Low short interest is *not* a signal, and an empty peer group never marks the rule available.

## Minimum Signal Gate

R5 needs at least one of {institutional flow, squeeze}. No flow data and no short data → `unavailable` (never a neutral 0).

## Coverage

`coverage = present / 2` over {flow, squeeze} — 0.5 or 1.0. It feeds composite confidence; the score is not scaled by coverage.

## Interpretation

| Component | Bullish | Bearish |
|-----------|:---:|:---:|
| Inst flow | Net institutional call premium | Net institutional put premium |
| Squeeze | High in-sector Z → squeeze fuel | Low Z → no squeeze catalyst |

## Example: SE (Sea Limited) — flow only

```
inst_net = +77,434 (bullish — institutions buying calls)
imbalance vs total inst premium small → modest positive contribution
```

Retail euphoria that used to be read here is now scored by R8 — read SE's retail-call share there.
