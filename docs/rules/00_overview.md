# RevenueChaser — Rule System Documentation

> **INTENDED DESIGN — not a description of what runs today.**
>
> This folder records the **intended** rule design: original weights, action
> ladder, NRC cutoffs and extra rule terms (e.g. R2 beat-trend, R3 RPO,
> R8 transcripts). It can diverge from the live scorers.
>
> **What actually runs:** `module_earnings_radar/rules/*.py` + `core/config.py`
> (`RULE_WEIGHTS`). **Code wins** for current behaviour; treat this folder as
> backlog/aspiration. R1 is reserved at 0% in code (see README); building it is
> board issue "improve RevenueChaser" (#15).

## Architecture

RevenueChaser scores every earnings event across **8 independent rules**, then combines them into a weighted composite direction score and confidence level. Rules that can't access data (unavailable endpoints, missing fundamentals) are excluded from the composite — only available rules contribute to the direction score. The confidence score still divides by the weight of ALL direction rules, so a missing rule lowers it.

```
Calendar → Ticker → Per-Rule Scoring → Composite → Direction + Confidence + Action
```

## Active Rules

| Rule | Weight | Role | Status |
|------|:------:|------|--------|
| R2 Guidance | 20% | EPS beat consistency & management credibility | ✅ Active |
| R3 Financial Quality | 20% | Sector-adaptive fundamental quality | ✅ Active |
| R5 Positioning | 20% | Institutional flow asymmetry & in-sector squeeze | ✅ Active |
| R7 Accounting | 15% | Sloan accruals, cash conversion, deferred revenue | ✅ Active |
| R8 Sentiment | 10% | Retail call share (momentum; `R8_SIGN` tunable) | ✅ Active |

## Inactive / Special

| Rule | Role | Why |
|------|------|-----|
| R1 Drivers | Revenue growth drivers | Requires 3rd-party supplier data |
| R4 Volatility | Direction-free options edge | Separate volatility_edge_score |
| R6 Macro | Veto-only layer | Blocks trades, never creates them |
| R0 NRC | Pre-filter | Categorizes into Tier A/B/C/Ignore |

## Composite Formula

```
direction_score = Σ(weight_i × score_i) / Σ(weight_i)    [available rules only]
confidence_score = 100 × Σ(weight_i × coverage_i) / Σ(all direction weights) × (1 − disagreement_penalty)
                   [numerator: available rules only; denominator: every rule with weight > 0 (0.65), available or not]
```

- **disagreement penalty**: 0.20 when R3 and R7 land on opposite signs (no other pair counts)
- **Capped**: direction ∈ [-100, 100], confidence ∈ [0, 100]

## Action Ladder

| Condition | Action |
|-----------|--------|
| R6 macro veto | NO_TRADE |
| \|direction\| < 20 | WATCH_ONLY |
| direction ≥ 45 AND vol_edge ≥ 60 | BULLISH_OPTIONS_SETUP |
| direction ≤ -45 AND vol_edge ≥ 60 | BEARISH_OPTIONS_SETUP |
| direction ≥ 45 | BULLISH_SHARES_OR_DEFINED_RISK |
| direction ≤ -45 | BEARISH_OR_HEDGE |
| Otherwise | WATCH_ONLY |

## NRC Tiers

| Tier | Score | Meaning |
|------|:-----:|---------|
| Tier A | ≥ 70 | Revenue Catalyst — strongest revenue signal |
| Tier B | 55–69 | Watchlist — solid indicators, track closely |
| Tier C | 40–54 | Emerging — mixed signals, monitor |
| Ignore | < 40 | No clear revenue catalyst |

## Data Sources

| Data | Endpoint | Used By |
|------|----------|---------|
| Earnings calendar | `/api/earnings/{premarket,afterhours}` | Event timing, estimates, sector |
| Earnings history | `/api/earnings/{ticker}` | R2 (beat history), price reactions |
| Stock OHLC | `/api/stock/{ticker}/ohlc/1d` | Price action, returns, Since ER |
| Financials | `/api/stock/{ticker}/financials` | R3 (income, balance), R7 (cash flow) |
| Options flow | `/api/stock/{ticker}/flow-recent` | R5 (flow asymmetry), R8 (retail share) |
| Short interest | `/api/short_screener` | R5 (in-sector squeeze Z) |
| Dark pool | `/api/darkpool/ticker` | reserved (unused by R5 today) |
| Screener | `/api/screener/stocks` | Liquidity filters, OI, volume |
| Rotation | ETF OHLC weekly vs SPY | Rotation quadrant per ticker |

## Sector Classification

Tick are classified into 6 clusters via GICS sector → cluster routing:

| Cluster | Sectors | Example Tickers |
|---------|---------|-----------------|
| C1 — SaaS | Software, Cloud, Internet Services | NET, DDOG, CRWD |
| C2 — Hardware | Semiconductors, Electronics, Aerospace | SMCI, AAOI, PH |
| C3 — Fintech | Banks, Lending, Financial Services | RKT, SOFI |
| C4 — Crypto | Crypto Mining, Blockchain | MARA, CLSK |
| C5 — Consumer | Retail, Travel, Entertainment | ABNB, LYFT, CAVA |
| C6 — Energy | Oil, Gas, Utilities, Mining | XOM, VST, PPL |
