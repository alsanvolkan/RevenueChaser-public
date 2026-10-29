# R5 — Positioning & Short Squeeze

**Weight:** 20% of composite direction score
**Sources (G9):** the options store's prints (`option_print`, the list set or the earnings set, see below) and the ownership store (FINRA short interest and days to cover, IBKR borrow fee and shares available)
**File:** `module_earnings_radar/rules/rule5_positioning.py`

## What It Measures

Who is positioned how, and what does that imply for the stock's reaction to earnings? Two independent components, **at least one of which must be present** or the rule is `unavailable` (score `None`, never a 0 fill):

1. **Institutional flow asymmetry** — are institutions net buyers or sellers of options?
2. **Squeeze tag** — `none` or `fuel`: is short interest (as % of float) or the borrow fee high enough to fuel a squeeze? A tag, not a score (redesign.md R5, #256): no Z ladder, and low short interest is not bearish. There is no utilization: no source serves one and redesign.md R5 says never invent one.

> Retail-call euphoria is **not** scored here. The same `retail_call / (retail_call + inst_call) > 65%` fade is R8's job; scoring it in both R5 and R8 double-faded retail and effectively gave one signal ~30% weight.

## Data Input

`aggregate_prints()` buckets per-print option premium by size (≥ 10 contracts → institutional, else retail) and side. The prints are the stored ones (G9, build plan PR 19), three ways per name:

1. a name on the option watch list: the list set's prints of its newest session before the report (`uw/option_list`);
2. a name off the list that passed the gates: the earnings set `uw/option_screener` fetched the evening before this report;
3. any other name: R5 reads **"not on the option watch list"**, `score=None`, no contribution (R4 and R8 too).

The short data is the ownership store's (`stored_inputs.short_rows`, G9): FINRA's shares short and days to cover, IBKR's borrow fee and shares available as a count. No float is stored yet, so SI % of float is missing and the tag reads the borrow fee alone. R5 uses:

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

### Squeeze tag (G9, #256)

`fuel` when SI % of float ≥ 20 or the borrow fee ≥ 10% a year (`FUEL_SI_PCT`, `FUEL_FEE_PCT`; default chosen, change if avo prefers), else `none`; the reason names what fired. The tag adds no points. With neither field present the squeeze component is missing, not `none`. The peer-Z ladder and the absolute short-interest fallback are gone.

## Minimum Signal Gate

R5 needs at least one of {institutional flow, squeeze}. No flow data and no short data → `unavailable` (never a neutral 0).

## Coverage

`coverage = present / 2` over {flow, squeeze} — 0.5 or 1.0. It feeds composite confidence; the score is not scaled by coverage.

## Interpretation

| Component | Bullish | Bearish |
|-----------|:---:|:---:|
| Inst flow | Net institutional call premium | Net institutional put premium |
| Squeeze | `fuel` tag (no points) | never bearish: `none` is no catalyst, not a short signal |

## Example: SE (Sea Limited) — flow only

```
inst_net = +77,434 (bullish — institutions buying calls)
imbalance vs total inst premium small → modest positive contribution
```

Retail euphoria that used to be read here is now scored by R8 — read SE's retail-call share there.
