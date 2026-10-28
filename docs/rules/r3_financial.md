# R3 — Financial Quality (Sector-Adaptive)

**Weight:** 20% of composite direction score  
**Source:** Unusual Whales `/api/stock/{ticker}/financials` — income statements, balance sheets, cash flows  
**File:** `engines/rule3_financial.py`

## What It Measures

Is revenue growth backed by sector-appropriate financial quality? Different sectors have different quality indicators — SaaS companies should show growing deferred revenue, hardware companies should manage inventory efficiently, and energy companies should convert revenue to cash.

The rule adapts its scoring to the ticker's cluster classification (C1–C6).

## Data Input

UW's financials endpoint provides 54 quarters of:

**Income Statements:**
- `total_revenue` → revenue
- `ebit` → used to compute operating margin
- `cost_of_revenue` → used to compute gross margin
- `net_income` → net profit

**Balance Sheets:**
- `inventory` → stock levels (hardware)
- `total_assets` → company size reference
- `deferred_revenue` → unearned revenue (SaaS)
- `net_receivables` → outstanding payments (fintech, hardware)
- `cash_and_short_term_investments` → liquidity

**Cash Flow:**
- `operating_cash_flow` → cash from operations (energy, R7)

## Per-Cluster Scoring

### C1 — SaaS / Software
| Metric | Positive | Negative |
|--------|----------|----------|
| Revenue growth | > 15% YoY → +15 | < -10% → −15 |
| Deferred revenue growth vs revenue growth | DR faster → +25 | DR slower → −15 (deceleration) |
| RPO/backlog growth vs revenue | RPO faster → +20 | RPO slower → −15 |
| Gross margin | > 50% → +10 | < 10% → −10 |

### C2 — Hardware / Semiconductors
| Metric | Positive | Negative |
|--------|----------|----------|
| Inventory vs Revenue Z-divergence | Z < −0.5σ → +15 (lean inventory) | Z > 1.5σ → −25 (excess stock) |
| Gross margin elasticity | > 0.5 → +15 (margin expanding) | < −0.5 → −20 (margin compressing) |
| Operating margin trend | YoY up > 2% → +10 | YoY down > 2% → −10 |
| Inventory/Revenue ratio | < 10% → +10 (lean) | > 40% → −15 (overstocked) |

### C3 — Fintech / Lending
| Metric | Positive | Negative |
|--------|----------|----------|
| NIM / interest margin | > 50% → +15 | < 20% → −15 (compression) |
| Efficiency ratio | > 40% → +15 | < 10% → −15 (deteriorating) |
| Receivables/Revenue | < 10% → +10 | > 30% → −15 (credit risk) |

### C4 — Crypto Mining
| Metric | Positive | Negative |
|--------|----------|----------|
| Revenue growth | > 20% → +20 | < −20% → −20 |
| Gross margin (mining efficiency) | > 60% → +15 | < 30% → −20 |
| Cash/Asset coverage | > 30% → +15 | < 10% → −15 |

### C5 — Marketplace / Consumer
| Metric | Positive | Negative |
|--------|----------|----------|
| Take rate / Gross margin | > 60% → +20 | < 20% → −20 |
| GM trend YoY | ↑ 2% → +10 | ↓ 2% → −10 |
| Operating margin | > 15% → +15 | — |

### C6 — Energy / Utilities
| Metric | Positive | Negative |
|--------|----------|----------|
| Revenue growth | > 10% → +15 | < −10% → −15 |
| Gross margin | > 35% → +15 | < 10% → −10 |
| GM elasticity | > 0.5 → +10 | < −0.5 → −15 |
| Cash conversion (CFO/revenue) | > 15% → +15 | — |

## Minimum Coverage Gate

R3 needs **at least 4 usable quarters** (a quarterly row with revenue; the last 8 are read), as `docs/rules/redesign.md` R3 locks: with fewer it returns **unavailable** and does not contribute to the composite, so one quarter's levels never vote (Fable review S5). With enough quarters, fewer than 2 financial features available is unavailable too. The rule's coverage is the quarter bucket (4: 0.40, 5-7: 0.60, 8: 0.80, R2's numbers); the share of the cluster's signals present stays a raw metric (`signal_coverage`, never above 1). **Default chosen, change if avo prefers:** the alternative is the older rule of this page, unavailable only below 2 features, which would then be written into redesign.md instead.

## Example: SE (C5 — Marketplace/Consumer)

```
Revenue: $7.1B (YoY growth positive)
Gross margin: 44% (strong take rate)
Operating margin: positive
→ Score: +10
→ Reason: "strong GM 44%"
```

SE has solid marketplace economics — good take rate and margins. This positive signal partially offsets the very negative R2 guidance score.
