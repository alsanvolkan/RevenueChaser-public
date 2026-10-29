# R8 — Sentiment

**Weight:** 10% of composite direction score
**Source:** the stored prints (`option_print`, G9: a list name's list set, or an off-list name's earnings set of the evening before the report) via `aggregate_prints` (size &lt; 10 = retail); any other name reads "not on the option watch list" and gets no score
**File:** `module_earnings_radar/rules/rule8_sentiment.py`

## What It Measures (live)

Retail call share as a **momentum** signal. Same size split as R5:

```
retail_share = retail_call_premium / (retail_call_premium + inst_call_premium)
```

| Share | Score |
|-------|:-----:|
| no call premium | unavailable (`None`) |
| ≤ 65% | **0** (available) |
| &gt; 65% | **+20** (not scaled) |

Coverage is 1.0 when a share is present, else 0.

The polarity is the tunable `R8_SIGN` (`+1` momentum, default; `-1` fade/PEAD).
The calibration report (removed with Earnings Radar v2) used to run a
fade-vs-momentum sign sweep; a polarity change is a recorded, human-in-the-loop
tuning step.

## Not wired

- **Transcripts / LM tone** — optional kwargs, pipeline does not pass them.
- **Small-lot share** — would re-count the same size&lt;10 prints already in retail.

Size &lt; 10 is a heuristic, not identity. Shared with R5 on purpose.
