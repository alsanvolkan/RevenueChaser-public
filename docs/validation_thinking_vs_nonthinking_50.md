# Thinking vs non-thinking — 50 tickers

Same prompt and filing text; only the thinking flag / effort differs.
Modes: non-thinking, thinking (effort low), thinking (effort high).

## Profile richness (mean tags per company)
| Mode | mean tags |
|---|---|
| non-thinking | 46.9 |
| thinking-low | 58.3 |
| thinking-high | 67.0 |

## Tag agreement with non-thinking (mean Jaccard per company)
| Comparison | overlap |
|---|---|
| thinking-low vs non-thinking | 0.46 |
| thinking-high vs non-thinking | 0.42 |

## Effect on pair scores (1225 pairs)
| Comparison | mean |delta| | Spearman rho |
|---|---|---|
| low vs non | 0.0203 | 0.791 |
| high vs non | 0.0194 | 0.774 |
| high vs low | 0.0169 | 0.805 |

## Peer lists (top-5) vs non-thinking
| Comparison | top-5 overlap | #1 peer changed |
|---|---|---|
| low vs non | 0.61 | 46% |
| high vs non | 0.64 | 32% |

## Token usage (this session's cache)
| Mode | calls | prompt tok | completion tok | reasoning tok |
|---|---|---|---|---|
| non-thinking | 217 | 456968 | 56414 | 0 |
| thinking-low | 50 | 107149 | 74069 | 61203 |
| thinking-high | 50 | 107149 | 93490 | 78440 |

Wall time this run (includes cache hits): {'non-thinking': 108.3, 'thinking-low': 361.5, 'thinking-high': 432.6}

## Reading
- Higher tag overlap + high Spearman rho => thinking mostly adds detail without
  changing conclusions.
- Low top-5 overlap or a high #1-change rate => thinking materially changes the
  peers, so the mode choice matters.

## Interpretation

- **Thinking adds detail, and shifts the peer lists.** Profiles get richer
  (46.9 -> 58.3 -> 67.0 tags) but tag overlap with non-thinking is only ~0.42-0.46
  and the **#1 peer changes 46% (low) / 32% (high)** of the time. Overall ranking
  stays correlated (rho ~0.77-0.81) with small absolute deltas (~0.02), so the
  board's big picture is stable while its top picks move.
- **Worked examples (same text, only the mode differs):**
  - **ADBE**: non-thinking's top peer was **DELL (0.20)** — a spurious
    hardware match. thinking-low moved the top toward **GOOGL / CRM** and put
    **CRM** in the top-3, i.e. closer to the software peers you would expect.
  - **ADI**: all three agree on the core (integrated/analog/mixed-signal ICs) and
    the top peer (**MPWR**); thinking-high added the more precise business model
    ("integrated device manufacturer") but also a questionable "software" tag.
  - **AMZN**: non-thinking was sparse (25 tags); thinking produced 41-57 tags and
    moved peers between retail (**WMT**) and tech (**GOOGL/ORCL/SNOW**) — AMZN is
    genuinely both, so the shift is legitimate ambiguity, not an error.
- **Cost:** thinking-low ~3.3x wall time and ~1.3x completion tokens of
  non-thinking (plus ~61k reasoning tokens for 50 calls); thinking-high is ~20%
  slower again and noisier.

### Verdict
Use **thinking-low** for the Relatedness board: it is materially richer and, on
the examples checked, more sensible than non-thinking (ADBE), at a modest cost.
Keep **non-thinking** as the cheap fallback / large-scale mode. **Skip
thinking-high** — marginal gain over low, more noise, more time.

Caveat: this is a 50-ticker sample with no ground-truth labels; the "ADBE looks
better" judgement is qualitative.

