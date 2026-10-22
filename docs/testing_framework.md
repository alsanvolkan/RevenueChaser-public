# Testing Framework — a reading guide

This is orientation, not a status report. It explains how the test suite is shaped, what each
module is responsible for, and what its tests actually guarantee — so you can find the test that
covers a behaviour, understand why it asserts what it does, and add new ones that fit.

Read the first three sections once. After that, jump to whichever package you're working in.

**There are two tiers.** This document describes the **unit** suite — fast, offline, ~1180 tests,
what `./app.sh test` runs. The **end-to-end** tier boots the real dashboard against a throwaway
store and a local mock provider server; it is opt-in and documented separately in
[`e2e_testing.md`](e2e_testing.md). Unit tests prove a function is right; e2e tests prove a board
still works. You generally want both.

---

## Finding your way around

**Tests live next to the code they test.** There is no top-level `tests/` tree. If you're editing
`module_swing_potential/ranking.py`, its tests are in `module_swing_potential/tests/`.

```
<package>/
  <module>.py
  tests/
    test_<module>.py
```

So the answer to "where is the test for this?" is almost always "the `tests/` directory beside it".
Most files are named `test_<module>.py`; a handful are named after the *concern* instead, when the
behaviour spans several modules — `test_gates.py`, `test_tuning.py`, `test_guardrail.py`,
`test_decision_id.py`, `test_html_export.py`. Test functions are plain `def test_*`, and the larger
files group related cases into `class Test*` containers.

**One file is misfiled.** `module_leaps/tests/test_holdings.py` tests
`module_sector_rotation.{engine,report}` exclusively — it contributes nothing to
`module_leaps`. If you're looking for sector-rotation holdings coverage, it's there. It belongs
at `module_sector_rotation/tests/test_holdings.py`.

**Running things:**

```bash
./app.sh test                 # everything
./app.sh test -k rule3        # by name
./app.sh test-x               # stop at first failure
./app.sh list                 # collect without running
python -m pytest module_earnings_radar -q
```

---

## Two rules the suite holds itself to

These are the load-bearing constraints. Understand them before adding a test, because most new-test
mistakes are violations of one of them.

### 1. No test touches the network

Every test runs offline. Providers are mocked at the **HTTP boundary** — never at rule-math level,
so scoring logic is always exercised with real arithmetic on synthetic inputs. The root
`conftest.py` supplies the mock:

```python
def test_something(mock_httpx):
    mock_httpx("/api/screener/stocks", {"data": [{"ticker": "AAPL"}]})
    mock_httpx("data.sec.gov", {"facts": ...})     # substring match
    # any unmatched URL returns 200 []
```

**This is enforced, not merely intended.** `conftest.py`'s `pytest_configure` patches
`socket.connect`, `connect_ex`, `create_connection`, and `getaddrinfo` to raise
`NetworkAccessAttempted`. A test that tries to reach a real endpoint fails immediately with a
message telling you to mock instead, so you cannot accidentally ship one that goes live.

That enforcement matters because **credentials are always present at run time**: the UW key lives in
`.env`, and `core/config.py` calls `load_dotenv()` at import, so `os.environ` is populated the moment
any package module loads. Nothing else stops a stray live call.

**One carve-out, and only one.** When `RC_E2E=1` the socket guard permits **loopback**
(`127.0.0.1`) so the end-to-end tier can run its local mock provider server. Everything that is not
loopback still fails loudly — an e2e test reaching the real internet is as much a bug as a unit test
doing it. The `--live` tier is the single deliberate exception, and it is opt-in twice over
(`./app.sh test-e2e-live`).

Testing a real API is out of scope for *this* suite. If you need it, add it to the e2e `--live`
tier (see [`e2e_testing.md`](e2e_testing.md)) rather than smuggling it in here.

### 2. No test depends on today's date

Fixtures use hardcoded dates. Any production function that would otherwise read `date.today()`
accepts an optional `today` the test pins:

```python
far_oi_totals(rows, date(2026, 8, 14))
build_snapshot(..., today=date(2026, 8, 14))
compute_flips(today=date(2026, 8, 14))
scan_leaps(uw, today=date(2026, 8, 14))
```

Each defaults to `date.today()`, so production behaviour is untouched.

**Why this matters more than it looks.** The failure mode is a hardcoded fixture value compared
against a *moving* reference — the test doesn't fail, it silently changes meaning, then breaks
months later for no apparent reason. The LEAPS tests pinned an option expiry of `2027-06-17`
against `FAR_DTE = 270`; that fixture was valid when written and would have started failing on
2026-09-21, with nothing in the diff to explain why.

If you're tempted to reach for mocking instead: patching `datetime.date` does **not** propagate.
`from datetime import date` copies the reference into each module's namespace at import time, and
the ~50 modules here that do so never consult `datetime.date` again. You'd have to rebind `date` in
every module of the call chain. Passing the date in is simpler and visible in the signature.

### 3. No test touches the developer's real data

`pytest_configure` points `RC_STORE_DIR` and `RC_CACHE_DIR` at a session temp directory **before
anything imports RevenueChaser**, so `.store/` and `.cache/` both resolve somewhere disposable for
the whole run. The per-test `isolate_rawstore` fixture additionally gives each test its own DuckDB
file.

Both layers are needed. `isolate_rawstore` covers only `rawstore.DB_PATH`; every `*_LATEST`
artifact constant is derived from `OUTPUT_DIR` at import and re-bound across ~20 module-level names,
so before the env redirect a route test hitting `?refresh=1` would unlink the developer's actual
rendered boards, and a runner test would overwrite them. Neither failure was visible from the test
output — the suite stayed green while eating `.cache/`.

The env vars must be set in `pytest_configure`, not a fixture: `core/config.py` reads them at
import. See [`data_architecture.md`](data_architecture.md#test-and-harness-isolation).

---

## Idioms you'll meet

Recognising these makes the rest of the suite legible.

**Table-driven pinning.** The rule engines and screener gates pin exact thresholds, and their
docstrings state which. This is intentional: a threshold change should fail loudly and name itself,
        not drift silently. `test_rule7_accounting.py` opens with "Pin: signed Sloan (positive
        accruals penalize, negative do not)" — that docstring is the specification.

**`unavailable` is `None`, never `0`.** A rule with insufficient data must return
`RuleResult(score=None)`. Zero is a *display* value meaning "neutral". Conflating them would let
missing data drag a composite score toward neutral, which is why nearly every rule test has an
explicit "unavailable, never 0" case. `test_trade_card_schema.py` pins this as a contract.

**Auditable gate results.** Screener gates return a `GateResult`:

```python
{"name": str, "passed": bool, "score": "X/Y",
 "rules": [{"id", "label", "unit", "passed", "actual", "threshold"}, ...]}
```

The `rules[]` checklist is what the UI renders in its audit modal, so it's part of the contract —
`test_gates.py` verifies the shape across all three swing gates, not just the pass/fail verdict.

**Rendered-HTML assertions.** Report modules are tested by rendering to a string and asserting on
structure and substrings. There is no golden-file comparison — `golden_dir` is wired into the root
conftest and `tests/fixtures/` is reserved for it, but empty.

**Regression tests that name their bug.** Some files exist for one historical defect and say so.
`test_pipeline_score.py` is entirely about UW returning numerics as strings (`close="6.355"`), where
an `f"${spot:.2f}"` raised `ValueError` and silently dropped every priced ticker.

**`tmp_path` for anything on disk.** Caches, snapshots, and the append-only
forward-test logs all write into `tmp_path`; every test's DuckDB store is
isolated via the `rawstore_tmp_db` fixture to `tmp_path/raw.duckdb` **and**
`tmp_path/edgar.duckdb` (the two store files live side by side) — never the real
`.store/`.

---

## Where shared fixtures come from

| Location | Provides |
|---|---|
| `conftest.py` (root) | `mock_httpx`, `fixtures_dir`, `golden_dir`, `network`-marker gating |
| `module_financials/tests/conftest.py` | `financials` — a synthetic 3-year annual statement set (IS/BS/CF), shared by every engine test |
| `module_spx_gamma/tests/conftest.py` | `raw` / `snapshot` — a synthetic UW payload (GEX levels, expiry/strike/spot exposure, IV, OHLC) and its normalized form |
| `pytest.ini` | `--strict-markers`, so an unregistered `@pytest.mark.X` is an error rather than a silent no-op |
| `setup.py` | Maps the repo to the `RevenueChaser` package, so every test imports absolutely regardless of cwd |

---

# The map

What each module does, and what its tests guarantee.

## `core/` — shared primitives

Everything else depends on these, so their tests are mostly about *consistency guarantees* rather
than behaviour.

| Module | Does | Tests guarantee |
|---|---|---|
| `taxonomy.py` | The shared sector vocabulary — the `core/gics_map.py` SSoT (the 11 Morningstar names + Unclassified) — and the theme labels: the pinned NMF run's medium themes, loaded per ticker | The sector list is the eleven Morningstar names then `Unclassified`; a ticker's themes are the pinned run's or `[]` — never a keyword or sector guess (C21, #383), because every dashboard's labels come from here and drift desynchronizes all of them |
| `index_registry.py` | Cache-first `ticker -> index` registry (S&P 500 / Dow / Nasdaq) | Membership inversion is correct, lookups resolve, cards get stamped, and `fetch_index` serves cache rather than refetching |
| `ticker_store.py` | Shared per-ticker store (the `ticker_scores` table in raw.duckdb) — the Hunter writes, other boards read | Roundtrip, missing keys, and that a repeat write overwrites instead of duplicating |
| `module_earnings_radar/models.py` | `TradeCard` / `RuleResult` | Every column the radar renders maps to a `TradeCard` field; `TradeCard()` always constructs (all fields defaulted); unavailable rules carry `score=None` |
| `config.py`, `reporting_common.py` | Constants; shared dashboard filter HTML/JS | No dedicated tests — exercised incidentally |

`core/tests/test_test_helpers.py` is unusual: it tests the *test infrastructure*, checking that
`mock_httpx` returns canned payloads and defaults unmatched URLs to `[]`. If that file fails,
suspect the conftest before suspecting your code.

## `core/providers/` — talking to the outside world

Adapters over UW, SEC EDGAR, and yfinance. Their job is normalization and graceful degradation, and
that's what their tests are about: **bad input must degrade, not raise.**

| Module | Does | Tests guarantee |
|---|---|---|
| `cache.py` | JSON cache with TTL | Corrupt JSON returns `None` rather than raising; TTL expiry and `ignore_ttl`; `get_or_fetch` skips the fetcher on a hit and saves on a miss; `save` never writes a payload with no `annual` and no `info`, nor one without `annual` over a stored `annual`, and never persists `_stale`; an unusable refetch returns the previous payload (any age) flagged `_stale` |
| `yfinance.py` | Normalizes yfinance DataFrames into `financials["annual"]` | Label matching and column mapping; the total-debt fallback (short + long) when `Total Debt` is absent; `info` exceptions swallowed; `interest_expense` comes from "Interest Expense", then "Interest Expense Non Operating", and is `None` for a frame with only "Net Interest Income" |
| `validator.py` | Missing-field validation + derivation | EBITDA derived from EBIT + depreciation (and correctly *not* derived without it); `latest_valid` picks the most recent complete record; inputs never mutated |
| `technical.py` | 52w distance, 10d/90d volume, 50d SMA | Every insufficient-history branch returns defaults instead of raising |
| `sec_edgar.py` | XBRL companyfacts — the small/micro-cap fundamentals source | `extract_annual` / `extract_quarterly` over synthetic facts; concept fallback prefers the recent tag; `no_fetch` never hits the network; `gross_profit` is derived as revenue − `cost_of_revenue` of the **same period** only when the filer reports no `GrossProfit` (a reported value always wins; revenue ≤ 0 or no cost → none; a negative cost → none plus `gross_profit_suspect`, never `abs()`); `cost_of_revenue` is resolved per period in the order `CostOfRevenue` → `CostOfGoodsAndServicesSold` → `CostOfGoodsSold` → `CostOfServices`, and `CostsAndExpenses`, `OperatingCostsAndExpenses` and `CostOfGoodsAndServicesSoldAndOperatingExpenses` are never used (they include operating expenses); `short_term_investments` (#187) is resolved per period next to `cash`: a total tag (`ShortTermInvestments` → `MarketableSecuritiesCurrent`) wins and two totals are never summed, otherwise the AFS part (debt tag first) plus the HTM part (the pre-CECL tag first) are summed; every counted fact must end on the date of that record's `cash` fact, a record with no `cash` gets no investments, a negative fact gives none plus `short_term_investments_suspect` (never `abs()`); `marketable_securities_noncurrent` and `long_term_investments` are stored and never counted; none of the investment fields adds a record (`_NEVER_A_RECORD`); the debt component fields (#239: `debt_and_lease_total`, `lt_debt_total`, `debt_current`, `short_term_borrowings`, `lt_debt_and_lease_noncurrent`, `convertible_debt_noncurrent`/`_current`, `notes_payable_noncurrent`/`_current`, `line_of_credit`, `line_of_credit_noncurrent`, `finance_lease_noncurrent`/`_current`, `operating_lease_noncurrent`/`_current`) are stored per period, never summed and read by no board, and add no record; `total_debt` stays `LongTermDebtNoncurrent` + `LongTermDebtCurrent` with the same 0-fill, byte-identical to before (`NotesPayableNoncurrent` is not a us-gaap element and is not mapped); operating leases are stored for visibility only and are no borrowings; annual records carry `period_ends` (`{field: end date of the fact that supplied it}`, a derived `gross_profit` ends with its revenue), quarterly records carry none; since #158 both carry per-field `filed` and `accn` (a value derived from two YTD facts takes the later accession and date), and quarterly records are discrete quarters: for additive USD flows a reported 3M fact is the quarter, else Q2 = 6M − 3M, Q3 = 9M − 6M, Q4 = FY − 9M from facts that share a `start` and one concept (facts keyed by `(start, end)`, latest `filed` wins, `fp` unused, share counts and instants keep the old rule, a missing link is a gap), the record lists derived fields in `derived`, keeps the cumulative fact in `ytd` (`months`, `start`, `val`; the longest span wins a tie) and the first day of each flow in `period_starts` (annual records too), a negative derived value of a field that cannot be negative (`revenue`, `cost_of_revenue`, `capex`, `depreciation`, `rd_expense`, buyback, SBC) is dropped and flagged `<field>_suspect` (never `abs()`), a derived value's `filed` and `accn` come from the later-filed of its two facts, a reported fp Q1-Q4 3M fact outranks a 10-K quarterly note, the quarterly concept pick ranks on reported (v1) periods only so v2 never switches a filer's tag (a differential test against a frozen copy of the old extraction pins that every v1 quarter is unchanged), and `meta.extract_version` is 2; `share_repurchases` (`PaymentsForRepurchaseOfCommonStock`, then `PaymentsForRepurchaseOfEquity`) and `share_based_compensation` (the cash-flow add-back `ShareBasedCompensation`) are new fields resolved per period that add no record; a multi-tag field on the annual path (#246: `revenue`, `net_income`, `operating_cash_flow`, `capex`, `depreciation`, `total_equity`, `cash`, `lt_debt_current`) keeps today's pick (newest year, then longest series, list order on ties) and only extends it with other tags' years: a hole inside its span (a whole run or nothing) or a contiguous run directly below its earliest year, each added year ending 350–380 days from its neighbour (52/53-week filers pass, a fiscal-year-end change or a calendar-key collision stops the chain); a year the pick holds is never replaced, nothing is added above its newest year, a gap is never bridged by that backfill (the add-only revenue tag below may still add non-adjacent years, never at the newest pair), and the first listed tag whose end fits wins an added year; quarterly series are not merged; the bank top line (#450) is TWO new fields that are never revenue, one tag each: `revenue_net_of_interest_expense` (`RevenuesNetOfInterestExpense`) and `interest_and_dividend_income` (`InterestAndDividendIncomeOperating`) — two fields and not one because the tags differ by the whole interest expense line, so a single field would let the #246 backfill take a year from the other tag and store a series that steps on a tag change (the rule the two revenue tax tags already follow); neither field adds a record (`_NEVER_A_RECORD`), so record periods are unchanged for every filer; a record simply has no such key when the filer reports neither tag (`_OMIT_WHEN_ABSENT`, the only fields with that shape, so "reports none" never looks like a stored zero) while a reported `0.0` keeps its key; an absent key therefore means either "the filer reports neither tag" or "the row predates #450", because `edgar_financials` has no TTL and no re-extraction script exists, so the first reader of these fields must bump `EXTRACT_VERSION` **together with** a re-extraction (which is why this change leaves it at 2); neither field reaches `period_ends`, so neither can move `fiscal_year.period_end` and the staleness and adjacency gates behind a card are byte-identical; the quarterly path carries them from the same extractor with no quarterly-specific code, including the YTD-derived quarters, and they are deliberately out of `_NON_NEGATIVE_FLOWS` because `RevenuesNetOfInterestExpense` can be genuinely negative, so a negative derived quarter is kept and carries no `_suspect` flag; no reader of `revenue` looks at either (a bank record therefore yields no EV/Revenue in the comps engine) and no board shows them |
| `core/liquidity.py` | `liquid_assets(record)`: cash plus short-term investments, the one definition AG (net debt, runway) and Fundamentals (net debt / EBITDA) share | Investments come from the record that supplies the cash, never from an older year; a missing investments figure counts as 0 only when cash is present; investments never stand in for missing cash; noncurrent and long-term investments are never counted; ROIC stays on cash only |
| `unusual_whales.py` | The main `UWClient` | Endpoint paths are built correctly, and — the substantive one — an empty `flow_recent` does **not** silently fall back to market-wide option trades |

## `module_earnings_radar/` — the earnings decision engine

The heart of the system, and the most heavily tested. A calendar event becomes a `TradeCard` via
per-rule scorers, a composite, and a renderer.

**`composite.py`** turns rule scores into a decision. Its tests are the ones to read first, because
they encode the scoring philosophy: all-unavailable is 0; missing rules do *not* drag the average
toward zero; coverage is weighted; disagreement between rules is penalized; and every `select_action`
branch is pinned — veto forces no-trade, small direction or low coverage becomes watch, and the
options-candidate gate sits at volatility edge 50. `test_tuning.py` covers the v1.0.1 changes
specifically.

**`classifier.py`** decides which sector cluster R3 scores a ticker against, via a cascade: explicit
override → direct sector map → keyword match → broad match → `unmapped`. The tests walk every step,
including keyword priority order and the sectors that deliberately fall through to broad match
(lowercase "technology", real estate, materials, utilities).

**The rule engines** (`rules/rule2..rule8`) are table-driven with pinned thresholds:

| Rule | Scores | Key pins |
|---|---|---|
| R2 guidance | Beat/miss track record | Bias buckets (+30 >5%, +15 >2%, −30 <−5%); beat-rate (+15 ≥75%, −15 <40%); <3 quarters → unavailable |
| R3 financial | Sector-adaptive quality | Coverage gate (<2 signals → unavailable); −10 per cluster red flag; per-cluster metrics for SaaS / hardware / crypto / marketplace / energy; z-divergence is antisymmetric and `None` below min length |
| R4 volatility | Options edge | Short gamma boosts, long gamma dampens |
| R5 positioning | Institutional flow, in-sector squeeze | Unavailable is None with no flow AND no short data; imbalance-scaled flow (±20×); renormalized peer Z buckets; <2 peers falls back to absolute SI and never marks available; retail euphoria removed (R8 owns it) |
| R6 macro | Veto only | Unavailable is None with no beta; `|β|>4` vetoes, `>2` is a reason only; `regulatory_risk='low'` is not a signal; yield_beta needs ≥20 paired returns |
| R7 accounting | Signed Sloan | `> +10% → −30`, `> +5% → −10`, else +20; cash-rich (negative) is not penalized; no DR term; unavailable is None |
| R8 sentiment | Retail call share | `>65% → +20` unscaled (momentum; `R8_SIGN` default `+1`); no data → None; coverage 1.0 or 0; LM/small-lot kwargs optional, not passed by pipeline |

Every one of them has an explicit unavailable-not-zero case. That repetition is the point.

**`pipeline.py`** is covered from several angles: `test_nrc.py` pins the NRC tiers (A ≥70 / B 55–69 /
C 40–54 / Ignore <40) and the quality<12 cap at 49; `test_earnings_detail.py` covers the drawer's
quarter history, including that a missing implied move stays `None` rather than being reused from a
prior quarter; `test_trade_summary.py` covers liquidity labels, edge ratio, trade read, and
attention.

**`report.py`** is the largest test file. Beyond formatting helpers, the substantive assertions are
the **earnings anchor rules** — BMO anchors on the *previous* close, AMC on the same-day close, other
timings default to PM, and the anchor falls back to the latest close when the earnings date isn't in
the OHLC series — plus dashboard structure: exactly 30 columns, excluded cards rendering in their own
section and *not* in the tier sections.

## `module_*` screener modules — the four scanners

All four share a shape: a broad US universe, a cheap prefilter, pure gate functions, then a ranked
payload and a self-contained HTML board. The pure functions are where the tests concentrate.

**`module_asymmetric_growth/`** — 3 signals × 3 sub-criteria for a 0–9 score, plus R&D and dilution
overlays. Tests cover the full 9-point pass, partial scoring, non-US and foreign-exchange exclusion,
both overlays, and universe fetching with provenance recorded.

**`module_fundamentals/`** — three gates (Rule of 40 / Quality / Momentum). The interesting test is
`test_guardrail.py`: a `[DISTORTED]` candidate (|growth| > 200% or TTM revenue < $10M) must be
**flagged but not dropped** from the payload. That's a deliberate product decision the test locks in.

**`core/screen_common.py`** — the scan body Swing and Fundamentals share (#147). `core/tests/test_screen_common_147.py`
drives it with fake boards (exclusion, shortlist cap, persist on Fetch only, counted drops);
`test_screen_parity_147.py` compares both boards' Fetch and Refresh payloads and persisted rows against a golden
captured from `main` before the refactor (`fixtures/screen_parity_147.json`, scenario in `screen_parity.py`);
`test_screen_u5_147.py` covers a short SPY series and a ticker that raises.

**`module_sector_rotation/`** — the most test-heavy screener, because the RRG dashboard is largely
client-side JS embedded in rendered HTML. Tests cover quadrant classification with strict boundaries,
snapshot persistence and weekly Wednesday backfill, the early-turn detector, the money-flow
leaderboard, the UW → FMP → yfinance holdings fallback chain, and then a long tail of rendered-output
assertions: embedded self-contained holdings, dynamic axis range, hover tooltips, leader lines,
holdings carets absent for non-equity groups, and the parallel set for the trail view.

**`module_swing_potential/`** — Minervini / Weinstein / Qullamaggie gates. `test_gates.py` pins the
prefilter (price ≥ $5, dollar volume ≥ $250K, within 25% of the 52w high, above the 50d SMA; the rule itself
lives in `core/screen_common.prefilter`) and the
`rules[]` checklist contract; `test_swing.py` covers indicators, percentile ranks, composite weights,
and ATR execution levels. Note `flow.py` is manual-only by default — the automated pipeline never
calls UW flow.

## `module_spx_gamma/` — SPX critical thresholds

A self-contained pipeline: fetch → normalize → chart → render, with a SHA-256 audit trail. Tests
follow that pipeline stage by stage.

The guarantees worth knowing: the UW client **never returns `{}` on failure** — it raises a typed
`UWApiError` or returns a payload with a validated `data` key, and it retries on 429. Snapshots carry
SHA-256 digests and a mandatory-endpoint failure aborts the whole snapshot rather than producing a
partial one. On the analysis side, a charm *peak* must never be mislabelled as a *pin*, far-OTM
levels are excluded from the chart, and the rendered report uses neutral wording — no
"resistance"/"support" claims — which `test_render.py` enforces on the output text.

## `module_sec_13f` / `module_leaps` — institutional footprint

**`module_sec_13f/`** is the authoritative institutional-ownership source. `engine.py` ingests SEC Form 13F
bulk ZIPs into raw.duckdb and derives the per-ticker totals from them (`recompute_totals`, store only), so the
board is always purely SEC-sourced (no third-party institutional feed is mixed in).

The tests encode the data-integrity rules: ingest is idempotent, a ZIP missing its infotable is skipped
*without* being marked ingested, amendments supersede originals, notice filings are excluded, and an
existing OpenFIGI mapping wins over name matching — so QoQ changes are always computed across genuinely
comparable SEC periods.

**`module_leaps/`** detects institutional accumulation via far-dated call OI, IV, and sweep footprints.
The classifier tests are the ones to read: accumulation requires far-dated *call* dominance (raw OI
growth alone is ambiguous — it could be short hedges), and a near-term earnings event is treated as a
**tag, not a drop**, so event-hedging doesn't masquerade as accumulation.

## `app/` — dashboard and job queue

`jobs.py` is a single-worker in-process queue that serializes module runs — that's not incidental, it
prevents UW rate-limit collisions and write races on the stable `latest.html` files, so
`test_jobs.py` asserts sequential execution explicitly.

`test_dashboard.py` covers routes and runner wiring. The substantive assertions are that the
`refresh_universe` and `no_fetch` flags thread correctly through the asymmetric_growth, swing, and fundamentals
runners, and that the shared universe job is registered for all three boards — flag-threading bugs
here mean a board silently scans stale data.

## `module_financials/` — Fin Research engines

DCF, three-statement, comps, credit capacity, sensitivity, and Quick Remarks, all driven off the
shared synthetic `financials` fixture. The load-bearing test is that the three-statement model
**balances**:

```
ΔCash + ΔAR + ΔInventory + ΔPP&E = ΔAP + ΔDebt + ΔEquity
```

with a revolver plug closing any cash gap. If that assertion fails, the model is wrong regardless of
what the other numbers say.

---

## What green does *not* tell you

Worth knowing so you calibrate how much a passing suite proves.

- **Reports are checked structurally, not visually.** Assertions are on substrings and structure. A
  page can pass every test and still be laid out wrong. Golden-file comparison is scaffolded
  (`golden_dir`) but unused.
- **Real network paths are never exercised here.** Every fetcher's request construction is tested;
  its behaviour against a live endpoint is not. The e2e `--live` tier covers schema drift.
- **A green unit run does not mean a board works.** Fetch → store → render → refresh is only
  exercised by [`e2e_testing.md`](e2e_testing.md). Regressions that broke whole tabs have passed
  this suite in full.
- **Browser JavaScript is untested anywhere.** Dashboard JS is asserted only as served source.
- **Orchestration is thin.** `app/runners.py` beyond flag-threading, `module_financials/run.py` and `cli.py`,
  `module_spx_gamma/run.py`, and `module_earnings_radar/terminal.py` have no direct tests.
- **There is no coverage measurement.** CI (`.github/workflows/tests.yml`, #103) runs the unit suite
  and the e2e tier on every PR and on `main`. Gaps are tracked by hand on the GitHub project
  issue tracker — e.g. end-to-end coverage is tracked as its own issue — not as a live
  percentage.
