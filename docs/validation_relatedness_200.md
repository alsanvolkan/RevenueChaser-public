# Relatedness 200-ticker trial ? validation

Model `deepseek-flash`, as_of 2026-09-18T22:34:57+00:00. 200 companies, 163 with a
usable business profile (37 had no 10-K/20-F text).

## Sanity checks
- Same sub-industry pairs: n=420, mean 0.148, median 0.113.
- Cross pairs: n=12783, mean 0.03, median 0.015.
- Expected orderings:
  - NVDA-AMD: 0.257 (p98.8)
  - JPM-BAC: 0.366 (p99.6)
  - AAPL-DELL: 0.37 (p99.6)
  - TSM-NVDA (different business): 0.063 (p83.7)
- Top pairs overall: PBR-PBR-A (0.81), GOOG-GOOGL (0.77), SHEL-TTE (0.71), BP-SHEL (0.6), BP-PBR (0.58), DUK-NEE (0.57), NVS-PFE (0.57), EQNR-TTE (0.55).
- thinking vs non-thinking on the sample: {'n': 6, 'mean_abs_diff': 0.012}.

## Notes
- Cosine of field-weighted profile tags; higher = more shared business attributes.
- 37 companies have an empty profile (no filing text yet).
- Cost: off-peak, 200 non-thinking + 20 thinking calls (~$0.3-0.5).
