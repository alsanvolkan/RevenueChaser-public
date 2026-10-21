# Rule Redesign — living change log

**Purpose:** every rule change that reworks an Earnings Radar scoring rule lives
here, one entry per rule, newest last. This is the working plan + record. The
per-rule spec docs under `docs/rules/` stay as the *reference*; this file tracks
what we decided to change and why, and it is the place to keep adding the next
rule we touch (we will keep editing this file).

Scope so far: R4 (done, shipped) · R2 (done) · R3 (done) · R5 (done) ·
R6 (done) · R7 (done) · R8 (done) · GUI Earnings Radar Identity columns
(done) · Decision columns (done) · Earnings Setup + Drawer charts (done) ·
Price → GEX & Liquidity (done) · AGH header labels (done) · info texts → Phase 1
semantics (done) · remove bottom NRC block (done) · R4 Yang-Zhang RV30 +
term-structure slope (done) · GUI Trade By column (done). Rule specs plus board
layout changes all live here; every rule and GUI item is implemented.

---

## R8 polarity flip — fade → momentum (DONE)

Date: 2026-09. Status: shipped. `R8_SIGN` changed from `-1` (fade crowded retail,
PEAD) to `+1` (**retail call share is momentum**). `retail_call_share > 0.65`
now scores **+20** instead of −20; the threshold (`R8_RETAIL_FADE = 0.65`),
weight (`0.10`) and magnitude (`20`) are unchanged. The R8 section below is the
original fade spec — **its polarity is superseded by this entry**; every
`fade 10%` / `−20` label in this file should read `momentum 10%` / `+20`.
`R8_SIGN` remains the tunable; the v2 calibration sign sweep was removed with
Earnings Radar v2. `config_hash` still marks a new cohort boundary.

---

## R4 — Volatility edge (DONE)

Date: 2026-09 · Status: implemented, full unit suite green.

### What changed

R4 is still a **vehicle rule** (0% composite weight): it never votes bull/bear,
it only decides options vs shares / defined risk. The change is that the GEX
read now comes from the **Directionalized Volume strike profile** instead of a
single ambiguous `gex-levels` flip.

### New source (`core/gex_profile.py`)

Strike-level profile from `/stock/{ticker}/spot-exposures/strike`
(`spot_exposures_by_strike`), Directionalized Volume book:

- `normalize_strike_rows` — flatten payload / client rows into strikes
  (both `_oi` and `_vol` books retained).
- `build_profile(rows, spot, em_pct)` — one deterministic dict:
  - `regime`: `short` / `long` from the **sign of net gamma inside the
    expected-move band** around spot (heatmap semantics: red band = dealers
    amplify = short gamma; green = dampen = long gamma). This is preferred
    over an interpolated flip because a stale OI flip far from spot would
    mislabel an earnings-night regime.
  - `magnet` (within ATM window), `charm` (Pin vs Peak), `vanna` (call / put
    lobes, by magnitude), `flip` (nearest cumulative net-gamma zero crossing,
    informational).
- Everything is pure and additive: missing fields are `None`; an empty payload
  returns `{}` and never blocks a score.

### Scoring changes (`rule4_volatility.py`)

- EM / IV-RV / history buckets unchanged.
- **GEX block, clamped [+20 / −15]:**
  - regime `short` → +15, `long` → −10 (volume book when present, else
    fall back to `gex-levels` `gamma_flip` vs spot).
  - magnet within 5% of spot: below → +8, above → −8; farther magnet is
    reason-only.
  - charm / vanna / far walls: reasons only, never points.
- Coverage unchanged ({EM, IV/RV, GEX, history}); R4 coverage never feeds
  direction confidence.

**Clamp is GEX-only.** The `+20 / −15` clamp applies to the GEX block alone
(regime + magnet), never to the whole vol-edge. EM and the history term sit
outside the clamp. Example arithmetic: EM +15 + local short +15 + magnet below
+8 = GEX raw 23 → clamped +20 → +15 EM −10 history = **25**. Charm/vanna/far
walls are reasons only. All-expiry net GEX (+16k type readings) is annotation —
never the regime.

**Box is as-of.** The shipped COO box (EM ~7.05%, magnet $65) is a dated
snapshot. Live spot (postmarket 2026-09-09 ≈ $62.39–$63.23) re-runs the rule:
magnet within 5% still fires if it stays near spot, but $65 above live spot is
not a downside target. Freeze the rule (magnet ≤5%, put wall, EM buckets), read
the strikes fresh. Never paste a live EM onto a stale 25.

### Structure text (`_gex_structure_label`, `recommend_options_structure`)

- Regime label prefers the volume profile (`Negative/Positive gamma regime
  (volume)`); magnet and far walls are shown with distances.
- `recommend_options_structure` now accepts a `gex_profile`; a bearish+short-
  gamma read becomes `LONG PUT / PUT DEBIT SPREAD · target magnet $X,
  put wall/lobe $Y` — real levels only, never fabricated strikes. Debit
  (defined-risk) structures are the vehicle the profile favours over naked
  long / credit into a crush.

### Wiring

- `pipeline._gex_profile(ticker, spot, em_pct)` — one extra UW call per
  deep-scored ticker when `R4_GEX_PROFILE_ENABLED` (default True); failure or
  disabled → `{}` (R4 falls back to `gex-levels` exactly as before). Fetch
  path only; profile persisted in the raw packet.
- `config.py` constants: `R4_GEX_PROFILE_ENABLED`, `GEX_REGIME_SHORT_GAMMA`,
  `GEX_REGIME_LONG_GAMMA`, `GEX_MAGNET_NEAR_PCT`, `GEX_MAGNET_BELOW_POINTS`,
  `GEX_MAGNET_ABOVE_POINTS`, `GEX_BLOCK_MAX`, `GEX_BLOCK_MIN`,
  `GEX_ATM_WINDOW_PCT`, `CHARM_PIN_NEAR_PCT`, `CHARM_PIN_DOMINANCE_RATIO`.

### Files

| File | Change |
|------|--------|
| `core/gex_profile.py` | new — strike-level profile (vol book) |
| `module_earnings_radar/rules/rule4_volatility.py` | GEX block, clamp, profile regime/magnet, charm/vanna reasons |
| `module_earnings_radar/composite.py` | `recommend_options_structure(..., gex_profile)`, target band |
| `module_earnings_radar/pipeline.py` | `_gex_profile()` fetch, opt wiring, structure label, raw packet |
| `core/config.py` | R4 GEX constants |
| `docs/rules/r4_volatility.md` | updated scoring + honesty rules |

Tests: `core/tests/test_gex_profile.py`, R4 profile cases in
`test_rule4_volatility.py`, target-band cases in `test_composite.py`, wiring
tests in `test_pipeline_score.py`.

### R4 addendum — Yang-Zhang RV30 + term-structure slope (2026-09)

Carried from the `uw-earnings-vol-scan` skill so R4's richness reads use the
construction its thresholds were calibrated against.

- **RV source.** IV/RV now divides `iv30d` by **Yang-Zhang RV30** — realized vol
  over the trailing 30 trading days, `sqrt(251)` annualisation — computed
  offline from the OHLC bars the pipeline already fetches
  (`pipeline._yz_rv30` → `core/vol_metrics.yang_zhang`); today's-or-later bars
  are dropped (the skill's `bars_through` rule). Too few clean bars → screener
  `realized_volatility` fallback, unchanged behaviour.
- **Rich-IV ceiling** lowered 1.5 → **1.25** (`IV_RV_RICH`), matching the skill.
- **Slope term** (`core/vol_metrics.term_structure_slope`, curve from a new
  `UWClient.term_structure` behind `R4_TERM_STRUCTURE_ENABLED`, mirroring
  `_gex_profile`): nearest expiry → 45 DTE, per day. Buyer polarity — deep
  backwardation ≤ −0.00406 → **−8**; (−0.00406, −0.002] → **−3**; contango > 0
  → **+5**. Absent/disabled/curve can't span → `None`, never fabricated.
- **Coverage** unchanged ({EM, IV/RV, GEX, history}); slope is scored but not a
  coverage part.

| File | Change |
|------|--------|
| `core/vol_metrics.py` | new — `yang_zhang`, `term_structure_curve`, `term_structure_slope`, `clean_ohlc` |
| `core/providers/unusual_whales.py` | `term_structure()` method |
| `module_earnings_radar/pipeline.py` | `_yz_rv30` + `_term_slope` wiring into `opt` |
| `module_earnings_radar/rules/rule4_volatility.py` | 1.25 ceiling, slope term, raw metrics |
| `core/config.py` | `IV_RV_RICH`…`TS_SLOPE_CONTANGO_POINTS`, `R4_TERM_STRUCTURE_ENABLED` |
| `docs/rules/r4_volatility.md` | inputs, scoring, honesty rules |

Tests: `core/tests/test_vol_metrics.py`, slope/boundary cases in
`test_rule4_volatility.py`, wiring cases in `test_pipeline_score.py`.

### Edge lock (display only — do not confuse with R4)

The "Edge" cell in Earnings Setup is a **display tag**, not R4, not a direction
vote, not the OPTIONS gate.

- Formula: `median(last-4 |T+1 1D move|) ÷ today's earnings expected_move`
  (`pipeline._compute_edge`). Pay = median of the last 4 completed prints (not
  mean — a −15% outlier would drag it). Pay = today's earnings EM (as-of),
  never the 9-DTE ATM or 30-day interpolated IV. `n < 3` or no EM → `N/A`,
  never a 0 fill.
- **Straddle P&L ≠ |spot|.** The Edge ratio is a raw price-move/implied
  comparison. Actual option P&L (long_straddle_1d) is R4's history term, and
  an intraday |spot| move ~0.09× EM can still be crushed by the IV collapse —
  that is why R4 uses the straddle bucket and Edge does not. `Edge > 1.0x` never
  softens R4 or opens OPTIONS.
- **This is not a VRP measure.** `median(|r|) / EM_today` turns red whenever
  today's IV is inflated; it does not say whether each print beat its *own*
  EM. True variance-risk premium would be `median(|r_i| / EM_i)` (per-print EM)
  — a separate study. When R4 falls back to `hist_move_ratio` (the same edge
  ratio) because `long_straddle_1d` is missing, the fallback must not be
  interpreted as VRP.
- Optional cleanup: `_compute_edge` picks `sorted(moves)[len//2]` — an upper
  median on even counts; a true even-median (mean of the two middle values) is
  a minor, display-only refinement.

---

## R2 — Guidance (DONE)

Date: 2026-09 · Status: spec agreed, code pending. One sign output, same API
family, less "sandbagging-as-guidance".

### Diagnosis

R2 today is a beat-track-record detector: did they beat street and is the beat
getting bigger? Problems on COO:

- equal weight on all 8 quarters (one +11% print inflates the mean);
- a +1% penny-beat counts the same as +11%;
- `%` explodes when `|estimated_eps|` is tiny (winsor ±50% is a patch);
- trend = last-4 vs prior-4 only (one outlier can flip it);
- 8/8 with 8 rows looks as sure as 3/3 (coverage already handles this);
- R2 consumes the **wrong endpoint**.

### Phase 1 — source split + robust statistics (no Advanced+ needed)

**Source.** R2 must read the raw EPS print series, not the calendar history:

- R2: `GET /api/stock/{ticker}/earnings` → `report_type == "quarterly"`;
  drop rows with null `reported_eps` / `estimated_eps` (tonight's unreported
  print is out); sort by `fiscal_date_ending` newest → oldest; last 8;
  `<3` usable → unavailable (never 0).
- R4 keeps `GET /api/earnings/{ticker}` (price reaction / straddle). R2
  ignores price reaction on purpose.
- Needs a new thin provider method (e.g. `stock_earnings`); the pipeline
  passes that series into `rule2_guidance.score`.

**Same −100..+100 ladder, better inputs** (UW's proposed
`0.45·avg + 0.25·median + 0.20·(beat−0.5)·10 + 0.10·trend` is NOT adopted —
mixed units; the ladder stays and the inputs change):

| Piece | Today | New |
|-------|-------|-----|
| Surprise | `(actual − est) / |est|`, drop `\|est\|≤0.001` | `(actual − est) / max(\|est\|, 0.10)` — no near-zero blow-up |
| Winsor | ±50% | keep ±50% |
| Central | equal-weight mean of 8 | **recency-weighted** mean (half-life ~4Q or weights 8…1) + **median** |
| Beat | `actual > est` | `surprise > +1%` (in-line if \|s\| ≤ 1%; 0.00 is not a beat) |
| Sign | avg alone | avg **and** median > 0 **and** beat-rate ≥ 5/8 → bullish; else mixed |
| Trend | last-4 vs prior-4 | last-4 vs prior-4 **plus consecutive beats ≥ 4** to call "accelerating" (one +11% outlier is not an acceleration) |
| Coverage | 3–4 / 5–7 / 8+ → 0.40/0.60/0.80 | unchanged; do NOT hard-cap 3–4 to mixed (coverage already discounts it) |
| `iv_rank`-style reason | — | recency-weighted avg surprise stays as the attached number |

### Phase 2 — optional true-guidance overlay (Advanced+ only)

- Only if `/api/companies/{ticker}/earnings-estimates` returns a real 30–90d
  revision path: estimate rising into the print → bullish; falling → the beat
  culture is less informative; no series → skip, never guess. Never blocks.

### Explicitly out of scope

Revenue / margins (R3), cash vs NI (R7), transcript tone (R8), post-print gap
(R4). Same-quarter YoY is deferred.

### COO expectation

Still bullish — but the headline should be closer to: median ~+3.8%, 8/8,
last-4 accelerating with one +11% outlier, not a clean "+4.75% they always
crush."

### Cross-rule locks (as-of 2026-09-09)

- **Do not freeze the pre-filter numbers.** "median ~+3.8% / 8/8 / one +11%
  outlier" is the raw tape, NOT evidence until the designed filters run
  (quarterly, drop null, recency-weighted mean, median, `|est| ≥ 0.10`,
  beat > +1%, last-4 vs prior-4 + consecutive beats). Never keep two R2 scores
  in the engine (current equal-weight vs designed).
- **R2 does not open OPTIONS.** R2 is a direction rule (20%). The vehicle is
  R4's vol-edge. A bullish R2 never forces OPTIONS on its own.
- Source boundary stays: R2 = `GET /api/stock/{ticker}/earnings` (EPS vs
  street); R4's last-4 straddle = `GET /api/earnings/{ticker}`. The 9-DTE ATM
  is not the EM.

### R2B — peer-earnings expectation context (PROPOSED)

User hypothesis recorded for later review: before a target company reports,
look at companies in the same theme that have already reported the comparable
quarter. Their earnings results and subsequent price reactions may reveal
whether expectations for the theme are rising or falling relative to the prior
quarter.

For each target event, the study should use only peers that reported before the
target's feature timestamp. Candidate peer features include:

- peer EPS surprise and beat/miss rate;
- peer post-earnings price movement, preferably relative to each peer's own
  expected move;
- the percentage of peers whose price reaction was positive or negative after
  the report;
- peer estimate revisions into the target's report, when a reliable revision
  series exists;
- comparison with the previous comparable earnings season or quarter.

The intended question is not simply "did peers beat?" It is:

> Did earlier peer reports raise or lower the market's expectation for this
> theme, and did that change the probability of a target surprise or a
> positive target reaction?

Bad peer earnings could increase the target's **relative surprise** probability
if they lower the market's bar for the target. They could also decrease the
target's actual surprise probability if the peers reveal a shared demand,
margin, or industry problem. The calibration must keep these hypotheses
separate rather than assuming that bad peer results are bullish for the target.

R2B therefore needs two separate outcomes:

1. target EPS surprise versus the estimate; and
2. target price reaction versus the pre-event price and expected move.

This is a context feature for R2, not an automatic score change. It must be
tested with matched non-peer or non-event examples, a no-look-ahead timestamp,
and enough observations before changing the live R2 decision.

---

## R3 — Financial quality (DONE)

Date: 2026-09 · Status: spec agreed (Phase 1), code pending. UW v2 prose
(mixed flip, 0.40 formula) is deliberately NOT implemented — see "Out of
scope".

### Vote (COO lock)

**bearish.** Headline: **op. margin −2.9%**. Tag: **opex event** (GM held,
SG&A z≈17.5, core OM ~23%). No mixed flip, no R3-core swap. The trader sees:
negative vote, reason = SG&A spike; GM holding; core add-back ~23%; sales /
inventory / AR clean. Not "margins collapsed", not "franchise healthy".

### Source

Existing `_map_financials` / `/financials` one-shot (income + balance). No
separate GETs. Filters: `report_type == "quarterly"`; drop null
`total_revenue` / `operating_income`; never fill missing with 0.

- **OM = `operating_income / revenue`**; fall back to `ebit` ONLY when
  `operating_income` is null (a valid negative OI is real — never prefer EBIT
  while OI exists).
- Required fields so tags can fire: `operating_income`, `total_revenue`,
  `gross_profit` (or `cost_of_revenue` for GM), `sga`, `inventory`, `ar`,
  `deferred_revenue`. If SG&A or GM is missing the tag path dies — an OM-down
  must not then read as blanket "franchise".
- No cash flow here (that is R7 / Sloan).
- **Usable quarters ≥ 4**, take last 8; coverage buckets (3–4 / 5–7 / 8+)
  line up with the ≥4 gate so the 3-row bucket never goes silent.

### Phase 1 ladder (−100..+100)

1. **Reported OM YoY points + last-4 median vs prior-4 median.** The mean is
   excluded from the vote (COO last-4 mean 11.9% is dragged by the −2.87%
   quarter; median is ~14.9%).
2. **Magnitude gate:** `< 2 pt` down AND GM held → mixed; `2–8 pt` down →
   bearish; **`> 8 pt` down → bearish, the vote does NOT flip** (COO is
   −21.3 pt).
3. **Upside path:** reported OM up AND last-4 median up → bullish; anything
   else (contradiction) → mixed. Latest magnitude overrides last-4: COO
   −21 pt + median down is not a contradiction.
4. **GM gate:** YoY ≥ **−50 bp** = held/up; below = down. Latest GM never
   enters the vote. An Oct-25-style GM air pocket is a flag on the last-4 GM
   block, not a latest-quarter driver — Phase 1 scores latest vs same-quarter
   YoY only.
5. **SG&A z > 3** → tag + `core_OM = (OI + max(0, SG&A − median_SG&A)) / rev`.
   **Sign flip is never allowed.** Baseline excludes the latest quarter (the
   spike would inflate σ and shrink z). With 8 usable, SG&A baseline n = 7
   (prior 7) — hold 9–12 SG&A quarters if the series allows. Use
   `(SG&A − median) / σ` or MAD; the z > 3 threshold is fixed. Median add-back
   from the same baseline window.
6. **Working capital:** inventory YoY vs sales YoY; AR YoY vs sales YoY —
   annotation only. `deferred_revenue` null → skip, never 0. Other-current-
   liabilities "BS parking lot" note never enters the vote.
7. **Cluster C1–C6 extras remain** (SaaS DR, hardware inventory Z, …). This
   universal OM layer does NOT override a cluster, and a cluster does NOT
   override the >8 pt bearish.
8. Coverage 3–4 / 5–7 / 8+ **never flips the vote** (it feeds confidence).
   `< 4` usable → unavailable.

### COO lock numbers

| Quarter | Rev | Op. inc. | OM | GM | SG&A |
|---------|-----|----------|----|----|----|
| 2026-04-30 | $1.082B | −$31.0M | −2.87% | 68.00% | $676.2M |
| 2026-01-31 | $1.024B | $212.8M | 20.78% | 67.88% | $390.2M |
| 2025-10-31 | $1.065B | $140.4M | 13.18% | 56.42% | $419.2M |
| 2025-07-31 | $1.060B | $175.7M | 16.57% | 65.26% | $421.7M |
| 2025-04-30 | $1.002B | $184.8M | 18.44% | 67.75% | $399.0M |
| 2025-01-31 | $0.965B | $182.0M | 18.87% | 68.44% | $387.9M |
| 2024-10-31 | $1.018B | $198.4M | 19.48% | 66.55% | $391.4M |
| 2024-07-31 | $1.003B | $192.5M | 19.20% | 66.11% | $381.1M |

- Δ OM = −2.9% vs +18.4% = **−21.3 pt** (> 8 → bearish, no flip).
- Last-4 median OM 14.9% vs prior-4 19.0% (mean excluded).
- GM 68.00% vs 67.75% YoY → held (+0.2 pt) → opex event tag.
- SG&A prior-7: median ~$390.8M, σ ~$16.0M; latest $676.2M → z ≈ 17.5
  (MAD-z ≈ 21.5). add_back = 285.4 → core_OM ≈ 23.5% (~22.8% on the +$277M
  YoY spike). Tag only.
- WC: inv +1.8%, AR +3.6% vs sales +7.9% → no build / no stuffing.
- `deferred_revenue` null → skip.

### Explicitly out of scope

- UW v2 mixed flip and the `0.40·OM + 0.20·medianΔ + 0.20·GM + 0.10·SG&A +
  0.10·WC` formula (mixed units; the −100..+100 ladder stays and the inputs
  change).
- "R3-core" (core-OM as the vote) — an add-back is a tag, not a silent swap
  to "healthy".
- EBITDA replacing OM as headline; last-4 **mean** in the vote; 4–5 quarter
  mixed lock.
- Anything from R2 (EPS vs street), R7 (cash vs NI / Sloan), R4 (gap /
  straddle), R8 (transcript / tone).

### Cross-rule lock (R3 × R7)

R3 bearish + R7 +20 is the ONLY pairing that pays the confidence `× 0.8`
disagreement penalty in Phase 1. R2 vs R3 is NOT a second multiplier (R2's
ladder and R7's +20 bucket are different scales — sign disagreement between
them is a headline, never a score arithmetic). R3 never turns bullish because
R7 printed +20, and R7 never cancels a bearish vote.

---

## R5 — Positioning (DONE)

Date: 2026-09 · Status: spec agreed (Phase 1), code pending. UW flow lecture +
COO package read folded in. No −13 style composite contribution.

### Vote (COO lock)

**R5 vote: NONE.** Lean + squeeze are **tags**, never a direction vote. This is
not the flow-form of an R3-core swap and it never opens the R4 OPTIONS gate.
"Tape is on the same side" is a note ONLY when the composite is already
directional — R5 never writes "R5 confirms R3".

### Source

- `/api/stock/{ticker}/flow-recent` — accepts `side` + `min_premium`.
- `/api/short_screener` — gives `fee_rate`, `short_shares_available`,
  `days_to_cover`, `si_float`; there is **no** utilization field — never invent
  one. SI is FINRA 2×/month, weeks stale (interest-float/v2).
- Multi-leg is not read leg-by-leg (multi-leg feed).

### Lean = UW side, not raw call/put buckets

- Bullish lean = **ask-call + bid-put**. Bearish lean = **bid-call +
  ask-put**. **Mid is dropped.**
- If side is unavailable, no lean tag is possible → `unavailable` /
  `contaminated`. "More call premium = bullish" is out of spec.
- Retail call share stays in R8.

### Institutional gate: size stays, premium adds

- **size ≥ 10 STAYS** as the institutional classifier (the same split R8 uses
  for retail). It is not dropped — dropping it would push size < 10 prints into
  both the lean and the fade.
- On top of size, a **premium ≥ $10k** print floor filters retail-dollar
  leakage. Both gates apply: institutional = size ≥ 10 AND premium ≥ $10k.
- R5 and R8 read the SAME `flow-recent` response with TWO local filters:
  R5 = size ≥ 10 + premium ≥ $10k + UW side (mid dropped); R8 = size < 10 +
  call only, no $10k. Never fetch `min_premium=10000` and hand it to R8 —
  small-lot vanishes and R8 turns false-unavailable or false-0%.

### Package rule (one rule for Phase 1)

- Multi-leg **drops out of the lean math**. If the package premium is material,
  tag `package-risk`. Net-package handling is Phase 2.
- COO 3-leg ($65P buy / $60P sell / $75C sell, same expiry) does not flip the
  lean — it **contaminates** it.

### Two thresholds + window (they must not collide)

- Print floor: institutional premium **≥ $10k** AND **size ≥ 10** (a size-10
  fill at $0.50 is ~$500 retail leakage, so size alone is not enough).
- Session net: `|net| < $25k` → **Thin** — Thin is PRESENT (a real but small
  tape), not missing.
- Window: last 1–3 sessions.
- Strike filter = R4 EM-band + earnings-expiry proximity. `flow-recent` does not
  slice strikes — slice locally.

### Squeeze tag

`none` / `fuel` from `si_float` or `fee_rate`. No Z ladder. Low SI ≠ bearish.

### Coverage (single rule — no R3 contradiction)

- Field present → component is **present** (COO squeeze present, score **0**,
  not missing).
- Both present → coverage 1.0.
- One genuinely missing → 0.5, **no vote**.
- Both missing → unavailable.

### COO lock (no vote)

- flow: −$390k / ~$610k → imbalance ≈ −0.64 → lean **PUT** (above Thin), but
  3-leg → tag `contaminated` / `package-risk`.
- squeeze: SI 3.92% → `none` (score 0, not missing).
- **R5 vote: NONE.** Tags: put-lean, squeeze-none, package-risk.
- R4 OPTIONS gate stays closed. Vehicle: debit / shares.
- **The −13 is the CURRENT code, not the designed engine.** Designed R5
  contributes no number to the composite — the put-lean tape only adds a note
  when the composite is already bearish ("tape on the same side"), and it never
  opens OPTIONS or confirms R3.

### Kept / dropped

Kept: two independent inputs; no zero fill; **size ≥ 10 stays as the
institutional heuristic** (not identity, and premium ≥ $10k added on top); low
SI ≠ bearish; coverage → confidence (not a multiplier); retail → R8; GEX / EM /
straddle → R4.

Dropped: the 20% direction vote (−13 never enters the composite); side-less
call/put buckets; leg-by-leg multi-leg scoring; the ±40/−10 squeeze ladder as a
vote; invented utilization; whole-chain lean.

---

## R6 — Macro (DONE)

Date: 2026-09 · Status: spec agreed (Phase 1), code pending. No COO beta or
veto is invented.

### Pattern

0% weight, no direction vote, audit-only (R4 vol-edge pattern). No number in
the composite. Not an R5 lean; does not soften R4 (a veto kills the debit map —
R6 never downgrades R4 OPTIONS to "just shares").

### Source

FRED `DGS10` **or** `GET /api/economy/treasury-yield?maturity=10year&interval=daily`
(Advanced+). Same decimal unit; inner join on common dates. Neither available →
`unavailable`, run continues. **No ^TNX.**
`GET /api/market/economic-calendar` enters Phase 1 ONLY for FOMC/CPI overlap;
it never mixes into β. Payrolls / GDP are not rate events.

### Beta

Intercept-inclusive OLS. Phase 1 return = **simple decimal** (pick one, do not
leave simple/log open). 10Y in decimal (4.80% → 0.048) so β ≈ equity duration.
Use |β| — sign is not a vote.

**Never use a stock/market beta.** The COO profile beta ≈ 0.52 is equity vs
market — not a rate beta. Only the OLS of the ticker's daily simple return vs Δ
daily 10Y (same unit, inner join) may feed R6. A market beta plugged in makes
every duration story wrong.

### Window

60 trading days, ≥ 20 paired returns, var(Δy) ≠ 0 **and** var(r) ≠ 0.
Earnings-gap days are dropped from β.

### Badge ≠ veto_flag

- |β| > 4 → reason + badge.
- **NO_TRADE only when |β| > 4 AND a rate event (FOMC/CPI) overlaps earnings
  ±1 trading day.** Otherwise a hard |β|>4 → NO_TRADE becomes a sector ban
  (REIT/utility/software), not a macro filter.
- |β| > 2 → reason only, no block.
- Noisy gate = |t| < 2 → no veto (no R² number).

### Coverage

1.0 or 0. No R5-style 0.5 contradiction. `unavailable` never equals veto.

### COO lock

- R6 measured, not invented. |β| ≤ 4 **or** unavailable → **NO_TRADE not set**.
- R3 bearish + opex tag; R4 no vote / vol-edge 25 / OPTIONS closed; R5
  put-lean / package-risk / squeeze-none.
- If a veto fires the R4 debit map dies — R6 does not soften it to shares.

### Kept / dropped

Kept: 0% weight; single slot; 0 no-fill; unavailable → run continues; coverage
1.0/0; |β|>2 reason not block; audit-only; "low" regulatory text is no signal;
negative duration is not bearish.

Dropped: |β|>4 → NO_TRADE on every name; intercept-less r/Δy ratio; %/% unit
mix; FX / geo / crowding; ^TNX.

---

## R7 — Accounting quality (DONE)

Date: 2026-09 · Status: spec agreed (Phase 1), code pending. One Sloan input;
period-matched to the last filed quarter. No COO mixed/bearish flip.

### Vote (COO lock)

**+20.** Direction vote, weight 15%, not a veto (that is R6). Sloan on the last
filed quarter (FY26 Q2, 2026-04-30):

```
Sloan = (−77.9 − 182.8) / 12,482.3 = −2.09%   → bucket +20
```

Coverage 1.0. Reason: cash well above earnings. Not R3 margins, not R2 EPS, not
R4 gap.

### Source + period lock

- **NI source is single:** NI = income-statement `net_income` (cross-checked
  against cash-flows `net_income` only). CFO = cash-flows `operating_cashflow`.
  TA = balance-sheets `total_assets`. On COO Q2 both NI readings are −$77.9M;
  if they ever diverge the quarter is a period-mismatch → Sloan is NOT computed
  (unavailable), never forced.
- Endpoints: `/api/stock/{ticker}/income-statements`,
  `/api/stock/{ticker}/cash-flows`, `/api/stock/{ticker}/balance-sheets`.
- Join key: **`fiscal_date_ending` + `report_type == "quarterly"`**; drop
  annual rows (the public path takes no `report_type` — filter locally).
- NI and CFO must cover the **same period**. A YTD income row joined to a
  quarterly cash-flow row (or vice versa) makes Sloan a lie — never mix. COO's
  YTD Sloan was −3.13% (Q1+Q2); the correct quarterly number is −2.09%.
- All three present and TA ≠ 0, else `unavailable` (never 0). `profit_loss`
  null on this row — do not use. Deferred revenue / RPO are R3, not here.

### Buckets (kept)

| Sloan | Score |
|---|---|
| > +10% | −30 + flag |
| > +5% | −10 |
| else (incl. negative) | **+20** |

No `abs()`: CFO ≫ NI is cash-rich, never a penalty.

### R3 split (intentional)

R3 reads the GAAP P&L (OM −2.9%, opex event, bearish). R7 reads cash vs NI
(+20). The litigation/SG&A accrual that sinks GAAP OP still leaves cash above
earnings — R7 does NOT say "healthy franchise"; it says "this quarter's profit
is cash-backed (even cash > profit)". Composite disagreement (R3 bearish + R7
bullish → confidence × 0.8) is a feature, not a bug.

### Explicitly out of scope (Phase 1)

- SG&A add-back into R7 (that is R3's opex tag — the cash form of an R3-core
  swap is not a second home for it).
- Last-4 median Sloan as a vote — R7 stays latest-quarter-only. A multi-quarter
  Sloan tag is a later option and would never flip the vote.
- YTD / annual-period Sloan; anything from R2 / R4 / R6.

---

## R8 — Sentiment (DONE)

Date: 2026-09 · Status: spec agreed (Phase 1), code pending. Retail call share
**momentum** (polarity flipped from the original PEAD fade — see the "R8 polarity
flip" entry above). Transcript / LM tone stays unwired. No COO %0 fill.

### Vote (COO lock)

**unavailable** — `flow-recent` could not be fetched for COO this run. No fade,
no 0, no −20. Coverage 0. A prior "size<10 exists" claim is not repeated: that
bucket was not proven in this run.

### Source

`/api/stock/{ticker}/flow-recent` (same tape as R5). Query: `min_premium`
default 0, `side` = ASK/BID/MID; there is no size filter and no `option_type`
param — retail (size < 10) and call are classified locally. Fields: `size`,
`premium`, `option_type`. `/earnings/{ticker}`, IS / CF / GEX not used.

### Unavailable ≠ 0

When R8 is unavailable its **slot is dropped** — the 10% weight is not filled
with 0. A 0 fill would read as "not extreme" and the fade could never fire.
Coverage 0; the composite average excludes the slot rather than averaging 0.

### Formula

```
retail_share = retail_call_premium / (retail_call_premium + inst_call_premium)
```

Denominator is **call premium only**.

| Bucket | Score |
|---|---|
| no call premium, or flow-recent missing | **unavailable** (`None`) |
| share ≤ 65% | 0 (available) |
| share > 65% | +20 |

Coverage 1.0 or 0; score is not scaled by coverage. Missing retail is NEVER 0
("not extreme" must not be fabricated).

### Kept / dropped

Kept: 10% weight fade vote; single slot `retail_call_share`; no call premium →
None; coverage 1.0/0; shared tape with R5 but a different bucket; small-lot not
double-counted; transcripts not a vote; R8 is not a veto and does not open the
R4 OPTIONS gate.

Dropped: using option-trades tape as the R8 source (that would make COO's
unavailable disappear — source stays flow-recent in Phase 1); folding R8 into
R5's ≥$10k / UW-side lean; scoring small-lot a second time; treating missing
retail as 0%; pulling in R3 / R4 / R7.

### R5 vs R8 split (deliberate)

R5 Phase 1 reworks to premium ≥ $10k + UW side. R8 keeps **size < 10** on the
same tape. Two intentional buckets, never merged. Side does not enter R8 (the
fade is call share, not ask/bid lean).

### Composite (COO)

R3 bearish + opex tag · R4 no vote / vol-edge 25 / OPTIONS closed · R5
put-lean / package-risk / squeeze-none (no vote) · R6 β not invented · R7 +20 ·
**R8 unavailable**. Confidence skips the 10% R8 weight.

Trader: retail fade none (unavailable), not 0%, direction is not R5's lean.

---

## Cross-rule composite locks (Phase 1, all rules)

These hold for every COO-style lock and stop two engines from appearing.

### Disagreement, votes, vehicles

- **Disagreement ≠ bearish.** The `× 0.8` confidence penalty applies ONLY to
  the R3 (bearish) × R7 (+20) pairing in Phase 1. R2 vs R3 sign tension is a
  headline, not a second multiplier. Sign counting never yields a net
  bull/bear score.
- **Debit ≠ R4.** The R4 row only produces vol-edge + the OPTIONS/SHARES gate —
  it does not map a put-debit. A `LONG PUT / PUT DEBIT SPREAD` structure is
  suggested only when the composite is already bearish, and never appears in
  the composite row itself.
- **R5/R6/R8 add no votes.** R5 put-lean (tag), R6 badge (no veto without a
  rate event), R8 fade (unavailable or −20) never open OPTIONS, never block,
  never confirm another rule.

### As-of discipline

- **No strike is frozen.** Shipped COO levels (magnet $65, EM ~7.05%) are a
  dated snapshot. The rule is frozen (magnet ≤5%, put wall, EM buckets); strikes
  are re-read against live spot. A magnet above live spot is not a downside
  target.
- **No live EM is pasted onto a stale vol-edge.** If spot/EM changed, re-run
  R4 — do not hand-edit 25.
- **Pre-filter numbers are never evidence.** R2's 8/8 / median ~3.8% /
  +11% outlier describe the raw tape only; the designed vote needs the designed
  filters run. Never ship two scores for the same rule.

### COO Phase 1 consolidated (as-of 2026-09-09, postmarket)

| Slot | Role | COO |
|---|---|---|
| R1 | off | — (no data, 0% reserved) |
| R2 | direction 20% | designed bullish — number after filters, no "always crushes" |
| R3 | direction 20% | bearish — OM −2.9%, opex/SG&A tag, core ~23%, GM held |
| R4 | vehicle 0% | no vote — vol-edge ~25 (as-of box), OPTIONS closed, no debit map here |
| R5 | tag | NONE — put-lean / package-risk / squeeze-none (no −13) |
| R6 | veto 0% | β not measured — no NO_TRADE |
| R7 | direction 15% | +20, Sloan −2.09% (no YTD −3.13%) |
| R8 | momentum 10% | unavailable — slot dropped, not 0 |

Direction source = R2 / R3 / R7 only (R3×R7 → ×0.8). Vehicle = R4. No veto.
Debit structure belongs to a bearish composite read, not to this table.

Trader: beat culture vs dirty GAAP vs cash above profit; tape is a tag, not a
vote; retail fade absent (not 0); box is as-of; no macro block. Direction is
not R4 / R5 / R8.

---

## GUI — Earnings Radar Identity columns (DONE)

Date: 2026-09 · Status: implemented, `module_earnings_radar` suite green.

### Change

The Earnings Radar Identity group is reordered so the earnings date/time is
read first:

```
Before:  Ticker | Name | Sector | Rotation | Date/Time
After:   Date/Time | Ticker | Name | Sector | Rotation
```

**Files:** `module_earnings_radar/report.py` — `_COL_WIDTHS` (70 Date/Time
first, then 56 Ticker), Identity subheader row, and the per-row cells all emit
Date/Time before Ticker.

### Sticky behavior

`sticky-tick` stays on **Ticker** (now column 2), not Date/Time. On horizontal
scroll the 70px Date/Time column scrolls away first; Ticker then pins to the
left edge (`position: sticky; left: 0` already supports a second pinned
column). No CSS change.

### Invariants kept

- Identity `colspan=5`, 30 total columns, group spans `[5, 7, 9, 6, 3]`.
- Date/Time sort (`data-sort-key="date"`) and AM/PM + `mm/dd` merged cell
  unchanged.
- Date cell keeps the same styling (10px, `#a5d6ff`); Ticker keeps
  `sticky-tick` + watchlist/excluded markers.

### Tests

`test_html_export.py`:
`test_identity_columns_date_time_first` (subheader order + sticky not on
Date/Time) and `test_identity_body_cells_date_time_first` (row cell order).

---

## GUI — Earnings Radar Decision columns (DONE)

Date: 2026-09 · Status: spec agreed, code pending. Locked: main table shows the
decision only; the reason opens in the row drawer. `q1` = the "Inst. Flow
(net)" sort chip is removed; `q2 = A` = Structure lives in the drawer, not the
main table.

### Change

Decision group goes from 7 columns to 3:

```
Before:  Cov | Inst. Flow | Bias | Action | Structure | Why | LEAPS
After:   Cov | Bias | Action
```

Total table: Identity 5 + Decision 3 + Setup 9 + GEX 6 + AGH 3 = **26**
columns (was 30). Group spans become `[5, 3, 9, 6, 3]`.

| Dropped | Why |
|---|---|
| Inst. Flow | R5 is a **tag** in the redesign, not a vote — a main-table column reads as a decision input it no longer is |
| Structure | Action already names the vehicle (OPTIONS/SHARES/WATCH); the debit map only exists for a bearish composite (lock). Strike detail belongs in the drawer |
| Why | Duplicates the rule table the drawer already renders |
| LEAPS | Not a scoring rule; an overlay for the drawer |

### Files

`module_earnings_radar/report.py`:

1. `_COL_WIDTHS` — drop 4 widths (Inst.Flow 120, Structure 150, Why 95, LEAPS
   90) → 26 entries.
2. Decision group header `colspan="7"` → `colspan="3"`; subheader keeps only
   `Cov` (`data-sort-key="conf"`, "Rule coverage, not hit rate" tooltip),
   `Bias`, `Action`.
3. Row cells keep `_conf_cell` / `_bias_cell` / the action cell. Remove
   `_inst_flow_cell`, the structure cell (incl. `_gamma_regime_tip`),
   `why-cell` chips, and `_leaps_line` from the row. Strip their sort keys /
   row `data-sort-inst`.
4. Remove `sortByInstFlow` and the "Inst. Flow (net)" toolbar chip (q1).
5. `why-cell` CSS and the `_leaps_line` / structure helpers are reused by the
   drawer, not deleted.

### Drawer (tapped) — already present, strengthened

Row click opens "Decision — which rule produced this action":

- Summary: Action · Bias · dir · R4 vol-edge · confidence.
- **Rule table** rendered dynamically from `rule_audit`, so it tracks the
  engine automatically when the Phase 1 rule specs ship:
  | Rule | Role | Shows |
  |---|---|---|
  | R2 | direction 20% | score + reasons |
  | R3 | direction 20% | score + reasons (opex tag once coded) |
  | R4 | vehicle 0% | vol-edge + reasons |
  | R5 | tag | lean / Thin / package — **no score, no −13** |
  | R6 | veto 0% | badge or — |
  | R7 | direction 15% | Sloan bucket |
  | R8 | momentum 10% | +20 or **—** (never 0) |
- Non-rule context rows: Structure (`gex_structure` + `_gamma_regime_tip`),
  Inst. Flow detail, LEAPS.

### Tests (`test_html_export.py`)

- Colgroup invariants → `sum == 26`, `spans == [5, 3, 9, 6, 3]`, 26 `<th>`,
  no `>LEAPS<` / `>Structure<` / `>Why<` / `>Inst. Flow<` in the subheader.
- Structure + gamma-tooltip strings asserted inside the drawer instead of the
  main row.
- `test_inst_flow_sort_button` updated to assert the chip and `sortByInstFlow`
  are gone.
- `TestDecisionAttribution` assertions move to the drawer surface.
- New: Decision subheader order `Cov | Bias | Action`; dropped labels absent
  from the main header but present in the drawer payload.

### Note

This GUI change is independent of the Phase 1 rule specs: the drawer renders
whatever `rule_audit` contains, so shipping R2–R8 later only changes the
numbers/tags inside the drawer, not this layout.

---

## GUI — Earnings Radar Earnings Setup + Drawer charts (DONE)

Date: 2026-09 · Status: spec agreed, code pending. Two parts: (A) drop the
duplicate `EPS Est.` column from the main table; (B) rework the row drawer —
three charts in one row, real y-axes, and an optional revenue estimate.

### A — Remove `EPS Est.` column (Earnings Setup 9 → 8)

The main-table `EPS Est.` column duplicates the drawer's "EPS vs Estimate"
panel, so it is dropped.

- `report.py`: `_COL_WIDTHS` drop `48  # EPS Est.` (→ 29 entries);
  Earnings Setup group `colspan="9"` → `colspan="8"`; drop the `EPS Est.`
  `<th>` (`data-sort-key="eps"`), the row cell `card.eps_estimate or "—"`,
  and `sort_vals["eps"]` / `data-sort-eps`.
- Total table: Identity 5 + Decision 7 + Setup **8** + GEX 6 + AGH 3 = **29**
  columns; group spans `[5, 7, 8, 6, 3]`.
- `eps_estimate` stays on `TradeCard` (the drawer/EPS panel still uses it).

### B — Drawer charts

#### B1. Three panels, one row

`buildDrawer` puts Price Reaction + EPS vs Estimate in the 2-col
`drawer-grid`, with Revenue (actual) as a separate full-width panel below.
Change: all three in the same row.

- `.drawer-grid` → `grid-template-columns: repeat(3, 1fr)`.
- Row order: **Revenue · EPS vs Estimate · Price Reaction** (Revenue panel
  moves into the grid; the standalone full-width Revenue panel is removed).
- SVGs may need a smaller width (680 → ~420) so three fit; `overflow-x: auto`
  stays as a safety net.

#### B2. Y-axes (rule locked by decision)

- **EPS vs Estimate:** y-axis shows EPS **$** ticks + grid; 0 line visible.
- **Price Reaction:** y-axis shows **%** ticks + grid (0 centered).
- **Revenue:** y-axis shows **$** ticks (`fmtRev`) + grid.
- **Symmetry rule (locked):** when a chart's values contain BOTH positive and
  negative readings, the y-range is computed symmetric about 0 as
  `±abs(max(|values|))` so both directions are the same level. Single-sign
  series keep a normal min/max with a visible 0 line.
- Shared JS helper for "nice" tick steps; EPS/Price/Revenue each label the
  left axis with its unit.

#### B3. Revenue estimate (option A — Advanced+)

Revenue estimate is forward-looking analyst data, not per-print history.

- **Config:** `REVENUE_EST_ENABLED = False` in `core/config.py` (default off).
- **Provider:** `earnings_estimates(ticker)` →
  `GET /api/companies/{ticker}/earnings-estimates` (**Advanced+**; 403/empty →
  `[]`).
- **Pipeline** `_score` / `_build_earnings_detail`: when the flag is on, map
  `estimates[]` `{date/horizon: revenue_estimate_average}` onto the matching
  completed-quarter rows as `revenue_est`. No match / flag off / 403 →
  `revenue_est = None` (never fabricated). Because the endpoint is forward,
  most past quarters will have no estimate.
- **`renderRevenueChart`:** when `revenue_est` is present draw an est marker
  next to the actual bar; otherwise actual bar + y-axis only, and
  `renderRevenueMetrics` states "Consensus: N/A (no consensus / Advanced+)".

### Tests

- Colgroup → `sum == 29`, `spans == [5, 7, 8, 6, 3]`, 29 `<th>`, no
  `>EPS Est.` in the subheader; `eps_estimate` still on `TradeCard`.
- Drawer: three panel containers in the grid; y-axis tick text present per
  chart (EPS `$`, Price `%`, Revenue via `fmtRev`).
- Symmetry: two-sign data set produces a symmetric range; single-sign keeps
  min/max.
- Revenue: `revenue_est` None when flag off / no data / fake lacks the method
  (`hasattr` guard); metrics N/A message when absent.

### Note

Independent of the Decision-column redesign: Setup shrinks 9→8 in this change,
and Decision shrinks 7→3 in its own change. Combined end state would be
Identity 5 + Decision 3 + Setup 8 + GEX 6 + AGH 3 = **25** columns.

---

## GUI — Earnings Radar: Price — GEX & Liquidity (DONE)

Date: 2026-09 · Status: spec agreed, code pending. Pure reorder — total stays 30.

### Change

`Price` leaves Earnings Setup and becomes the first column of GEX & Liquidity,
toned like GEX (`td-g4`):

```
Setup    : Exp. Move | Edge | EPS Est. | Return since print (-4E .. -1E) | Since ER      (9 → 8)
GEX & Liq: Price | GEX | Liquidity | MCap | Stk Vol | Opt Vol | OI   (6 → 7)
spans    : [5, 7, 8, 7, 3]   (30 columns — unchanged)
```

`Return since print (-4E..-1E)` is the return from each print's reaction-session close to today's close, not a one-day
move. A cell is blank when the print predates the fetched price window (270 sessions; UW may return fewer, e.g. 252, so
-4E can be blank) or when the print has no `actual_eps` yet (`pipeline._compute_earnings_reactions`).

### Files

`module_earnings_radar/report.py`:

1. `_COL_WIDTHS` — move `60 # Current Price` to sit between `90 Since ER` and
   `170 GEX Grid`.
2. Group header — `Earnings Setup colspan="9"` → `"8"`; `GEX & Liquidity
   colspan="6"` → `"7"`.
3. Subheader — drop the `Price` `<th>` from the Setup row; prepend `Price` to
   the GEX & Liquidity subheader.
4. Body — move `_price_cell(card.current_price)` from the Setup block to the
   front of the GEX & Liquidity block (before `_gex_grid_cell`).
5. Tone — the moved cell gets **`td-g4`** (GEX group tone). `_price_cell`'s
   inner blue `#a5d6ff` text stays as-is.

### Tests (`test_html_export.py`)

- Colgroup: `sum == 30`, `spans == [5, 7, 8, 7, 3]`, 30 `<th>`.
- `Price` header lives under GEX & Liquidity, not Earnings Setup; the
  `_price_cell` output sits in the `td-g4` cell block.

### Note

Independent of the pending Decision (7→3) and EPS Est (Setup 9→8) designs.
This move keeps 30 columns; those two are separate column-count changes.

---

## GUI — Earnings Radar AGH header labels (DONE)

Date: 2026-09 · Status: spec agreed, code pending. Full names in the AGH group
header and the 13F period shown on the Owners header.

### Change

- Group header (`report.py` `_render_section`): `AGH` → **`Asymmetric Growth
  Hunter`** (colspan=3 unchanged).
- Column subheaders: `AGH` → **`Score (0-9)`**; `Accel` → **`Acceleration`**
  (sort keys `agh` / `accel` unchanged).
- `Owners` → **`Owners (13F 2026-Q1)`** where the quarter is the **global**
  latest 13F filing period, not per-ticker.

### Owners quarter — option A (locked)

- Source: `RevenueChaser.core.provenance.sec13f_period()` (max
  `period` across `sec13f_submissions`, e.g. `2026-03-31`).
- Format as `YYYY-QN` (2026-03-31 → `2026-Q1`).
- Header is static for the whole report (single value); no per-ticker
  `inst_period` column is added (that was option B, rejected).
- **Fallback:** when `sec13f_period()` returns `None` (empty store / tests) the
  header renders `Owners (13F)` — never an empty bracket, never a crash.

### Files

- `module_earnings_radar/report.py` — group header label, three subheader
  labels, and the Owners-period lookup at export time.

### Tests (`test_html_export.py`)

- Header contains `Asymmetric Growth Hunter`, `Score (0-9)`,
  `Acceleration`, and `Owners (13F`.
- Empty-store fallback: no `sec13f_period` row → `Owners (13F)` without a
  trailing bracket/dangling text; no exception.
- Existing AGH sort tests (`data-sort-key="agh"` / `"accel"`) unchanged.

### Out of scope

Per-ticker `inst_period` (option B), cell contents, sort behaviour.

---

## GUI — Earnings Radar info texts — Phase 1 semantics (DONE)

Date: 2026-09 · Status: spec agreed, code pending. Every explanatory string on
the Earnings Radar board is aligned to the **Phase 1 rule design** (option B
locked), even though the scoring engine still runs today's code until Phase 1
ships.

A single disclaimer sits at the top of the Methodology block: *"Explanations
reflect the Phase 1 rule design; the score engine matches them once the Phase 1
transition lands."* This is the honest marker for the temporary (B) mismatch —
no engine change happens here.

### 1. Header tooltips (group + column)

- **Cov**: "Rule coverage, not hit rate. 100 × (Σ weight × coverage) / (Σ live
  weights) × (1 − disagreement). R3×R7 opposite signs → × 0.8."
- **Bias / Action**: Action comes from the R2/R3/R7 direction votes + the R4
  vehicle gate; R5/R6/R8 do NOT vote direction.
- **Earnings Setup (Edge)**: "Not the R4 vol-edge; display only — median of the
  last-4 |1D| moves ÷ today's earnings EM."
- **GEX & Liquidity**: GEX grid = Call wall / Put wall / Flip / Magnet;
  "regime = EM-band net gamma (volume book)".
- **AGH**: "Asymmetric Growth Hunter score (universe overlay) — not a scoring
  rule."

### 2. Cell tooltips

- `_action_tip`: new gate language —
  - OPTIONS: "R4 vol-edge ≥ 50 AND |dir| ≥ 25 → options vehicle (direction from
    R2/R3/R7)".
  - SHARES: "R4 vol-edge < 50 → shares / defined risk".
  - NO_TRADE: "R6 veto: |β| > 4 AND FOMC/CPI within earnings ±1 trading day"
    (Phase 1 — a bare |β|>4 is not a veto).
- `_conf_cell`, `_bias_cell`, `_edge_cell` (explicitly "not R4 vol-edge"),
  `_inst_flow_cell` (Thin / package-risk / lean — no vote), `_gex_grid_cell`,
  `_liquidity_cell`, `_agh_*`: one sentence + unit + Phase 1 role
  (direction / vehicle / tag / veto / momentum).

### 3. Drawer — "Decision — which rule produced this action"

- Each rule row gains a **role** tag: R2/R3/R7 `Direction %`, R4
  `Vehicle (0%)`, R5 `Tag (no vote)`, R6 `Veto (0%)`, R8 `Momentum`.
- R5 row shows `lean / Thin / package-risk / squeeze`, never a −13 score.
- R8: `unavailable` renders "— (no data)", never "0 / not extreme".
- R6: badge or "—", with the NO_TRADE condition in its reason.
- Footnote: "Direction comes only from R2/R3/R7; R3×R7 sign conflict lowers
  confidence."

### 4. Methodology tables (`_methodology_tables`)

- **Active Scoring Rules**: add a Phase 1 column per rule — R2 source
  `/stock/{t}/earnings` + recency/median; R3 GAAP OM + opex tag; R5 "tag, no
  vote (lean = UW side; ≥$10k + size≥10)"; R6 "badge; NO_TRADE only on
  FOMC/CPI overlap"; R7 quarterly Sloan; R8 unavailable ≠ 0.
- **NRC / Coverage / Rotation**: Coverage text narrows ×0.8 to "R3×R7 only";
  R5 drops out of the direction-rule list.
- R1 "reserved" note unchanged.

### 5. Sync with GUI column renames

Texts use the new labels: Date/Time · Score (0-9) · Acceleration ·
Owners (13F …) · Price (in GEX & Liquidity) · Decision (Cov | Bias | Action
design).

### 6. Tests (`test_html_export.py`)

- Assert presence of: "hit rate", R5 "no vote"/"tag", "no data ≠ 0",
  Edge "not the R4 vol-edge", "Owners (13F", "Asymmetric Growth Hunter".
- Existing Methodology assertions (Filter / NRC / Active Rules / `&ge; 70`)
  remain.

### Out of scope

Rule **code** (the Phase 1 implementation), the chart y-axis + revenue-estimate
work (separate DESIGNED items), and the other boards (v2 etc.).

---

## GUI — Earnings Radar: remove bottom NRC block (DONE)

Date: 2026-09 · Status: spec agreed, code pending. GUI-only removal.

### Change

The standalone **"NRC Filter — Notable Revenue Candidates"** block embedded at
the bottom of the Earnings Radar dashboard is removed. Scope is deliberately
narrow: the main table's Tier A / Tier B / Tier C / Ignore section headers stay,
the Methodology "NRC Scoring" table stays, and the legacy CLI path stays.

- Dashboard path (`app/runners.py` → `export_html`) is the only GUI surface
  that rendered this fragment.
- GUI surface removed: `render_nrc_section(...)` call and the `nrc_section`
  template slot.

### Files

- `module_earnings_radar/report.py` — drop the `nrc_section: str = ""`
  parameter from `export_html` and remove the ` {nrc_section}` slot at the
  bottom of the HTML template. `nrc_results` + the tier-section grouping logic
  are unchanged.
- `app/runners.py` — remove the `render_nrc_section` import and call; drop
  `nrc_section=nrc_section` from the `export_html(...)` invocation.

### Retained (not touched)

- `rules/nrc.py` (`render_nrc_section`, `export_nrc_html`, `score_all`) — still
  used by the legacy CLI.
- `main.py` CLI (`export_nrc_html` + `report_files`).
- Earnings Radar main-table Tier A/B/C/Ignore sections and the Methodology
  "NRC Scoring — What Each Component Measures" table.

### Tests

- `export_html` output contains no `NRC Filter — Notable Revenue Candidates`;
  `Tier A — Revenue Catalyst` (main sections) still present.
- Dashboard `app` suite updated if a runner mock expected `render_nrc_section`.
- Existing `"NRC Filter" not in body` tab assertion remains valid.

---
