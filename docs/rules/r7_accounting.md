# R7 — Accounting Quality

**Weight:** 15% of composite direction score
**Source:** Unusual Whales `/api/stock/{ticker}/financials` (same `fin_records` as R3)
**File:** `module_earnings_radar/rules/rule7_accounting.py`

## What It Measures

Are reported earnings cash-backed, or booked ahead of cash? Signed Sloan
accruals only. Deferred revenue / RPO live in **R3** (SaaS cluster) — scoring
them here double-counted SaaS and false-fired names whose “DR” is
`customer_deposits`.

## Sloan Accruals

```
accruals = (net_income − cash_from_operations) / total_assets
```

Need all three fields and `TA ≠ 0`. Else `unavailable` (`score=None`, never 0).

| Accruals | Score | Signal |
|:--------:|:-----:|--------|
| > +10% | **−30** + flag | Earnings not cash |
| > +5% | −10 | Elevated accruals |
| ≤ +5% (incl. negative) | **+20** | Cash-backed; `< −5%` reason is “cash well above earnings” |

No `abs()`. CFO ≫ NI is not manipulation.

Coverage is 1.0 when Sloan is present, else 0. The score is **not** scaled by coverage.

## Interpretation

R7 leans the **direction** score (Sloan: high positive accruals underperform).
It is not a veto — that is R6.
