# Plan v2 — Classification: shared universe, EDGAR store, Relatedness, NMF themes

Supersedes `plan_rest.md` + `plan_rest_hardening.md` (kept as history). Merges the
AI audits `plan_rest_review.md` / `plan_rest_hardening_review.md`. This file is the
single work order.

## Reviewer's guide (read first)

- Python app, no npm. `.store/` is DURABLE and holds exactly two files:
  `raw.duckdb` (all structured raw + derived) and `edgar.duckdb` (EDGAR content:
  `edgar_documents` metadata, `edgar_facts`, `edgar_financials`, `chain_*`). Raw
  10-K/20-F HTML is **files** under `edgar_docs/{cik}/{accession}.html.gz`, not a
  BLOB. `.cache/` is regenerable; `edgar_bulk/*.zip` are downloaded, ingested,
  then deleted.
- Boards: Fetch = network + store update; Refresh = store only, **zero network**
  (e2e-asserted). Single writer per table; hot-path fields are typed columns.
- Sector SSoT = `core/gics_map.py`. Theme SSoT after Phase 8 = `ticker_themes`
  (runtime) over a pinned `theme_run_id`; `core/taxonomy.py` is a thin loader.
- SEC <= 10 req/s shared; stable User-Agent. DeepSeek off-peak only.
- Review question: same as `plan_rest.md` §5 (audit Phases 4/6/8 with file:line
  evidence; blockers/should-fix/nits; no code).

## Locked decisions

- Canonical per-domain tables + derived `company_profile` **VIEW**.
- Gates/signals in typed `*_gates` tables (filterable).
- `equity_universe` = only membership + sector source; **all three classification
  boards read one set: market_cap >= $300M (~3,634)** via
  `core/universe.py::classification_universe`, minus the identity-master security
  types that are not operating companies (`identity.types.EXCLUDED_TYPES`: funds /
  ETFs, SPACs, preferred / warrants / units; Identity R4, #87). A name with no
  type, or an `unknown` / `other` one, stays and is counted in the funnel note.
- Prune rule (single): spine + reference dictionaries + fact tables + per-ticker
  scores KEPT (`ticker_payload_cache`, `edgar_financials`, `watchlist`); board
  artifacts restricted to the classification universe.
- EDGAR official/free only (bulk zips + per-document `.htm`); no npm/sec-api/GPL.
- Relatedness profiles: **thinking-high**; theme naming: **thinking-low** (off-peak).
- Themes: NMF, **nested** (fine ~150 -> medium ~70 -> coarse ~25 via agglomerative
  merge), LLM-named; replaces the taxonomy theme vocabulary; GICS untouched;
  assignment hybrid (profile weights, else lexicon, else sector fallback).

## Target data model

Spine: `equity_universe` (ticker PK, name, market_cap, price, sector,
industry, industry_group, sub_industry) — the only identity + sector source (since #265 sector and industry are UW's
two Morningstar levels; `industry_group` and `sub_industry` are NULL).
**CIK is NOT stored here** (2026-09-20 decision): resolve it at read time from
`sec_company_tickers` (`sec_edgar._cik_map()`); both universe writers use
`replace_all` and would erase a stored column.

Per-ticker scalar (own metrics only, no identity/GICS):
- `ticker_scores` (AG), `fundamentals_scores`, `swing_scores`,
  13F latest (from `sec13f_totals`/`enrichment`).

N-valued typed children:
- `ticker_themes(ticker, theme_id, theme, level, theme_run_id)` — runtime theme
  SSoT; `theme_id` is the join key, `theme` is display-only. Phase 1 legacy rows
  carry `theme_run_id='legacy-keyword'`, `level='legacy'`.
- `index_constituents(slug, ticker)` — reference; `index_tickers` = view.
- `comov_*`, `chain_filings`/`chain_mentions`.
- `relatedness_profiles(ticker, cik, model, prompt_version, fetched_at, cache_key,
  status)` + `relatedness_attributes(ticker, field, value TEXT, value_num DOUBLE)`
  + `relatedness_scores(ticker, peer, score)`.
- `relatedness_themes(theme_id, level, parent_id, label, size)` +
  `relatedness_theme_tags(theme_id, tag, rank)` +
  `relatedness_theme_members(ticker, theme_id, weight)` + `theme_runs`.
- `fundamentals_gates` / `swing_gates(ticker, gate, rule_id, passed, actual, unit)`.
- `edgar_financials` (per-ticker JSON: `annual`/`quarterly`/`meta`; not pruned)
  and `edgar_facts` (the fact spine, grain `(cik, concept, period, form)` with
  typed value columns) — both in `edgar.duckdb`.

Derived VIEW: `company_profile` = **one row per `equity_universe` ticker** (7,403
at plan date; the invariant is `|equity_universe|`), scalar joins only, no scalar
theme. Boards iterate `classification_universe()` (operating companies at or above the
floor).

Reference dictionaries (never pruned): `sec_company_tickers` + `sec_company_sic`
(**ticker-keyed** — the CIK-parent/`cusip_map` merges are deferred, see Phase 1);
`sec13f_*`; `sec13f_security_meta` (PK = ticker). 13F "latest" tie-break: newest
`period`, then newest `filing_date`.

## Rename map

equity_universe · equity_universe_provenance · ticker_scores · ticker_payload_cache
· edgar_financials · index_constituents · index_tickers(view) · sec13f_security_meta
· etf_holdings · ticker_themes. Deferred: cusip_map, sec_company(+child).
Kept: swing_info.

## Phases

### Phase 0 — Safety
- `.store` checkpoint.
- Idempotent `ALTER TABLE RENAME` migration recorded in `migration_manifest`.
- `docs/glossary.md`.
- `run_registry` table (one place for migration / profile-mode / theme pins).

### Phase 1 — Data-model refactor
- Apply the rename map.
- Merges: two `*_universe_meta`; `cusip_ticker` + `sec13f_cusip_names` ->
  `cusip_map`; `sec_company_tickers` + `sec_company_sic` -> `sec_company`
  (**PK = cik**, child `sec_company_tickers(cik, ticker, name)`). On a duplicate
  `cik` keep one row: `name` from the SEC tickers file, `sic` from the SIC file,
  extras logged to a reject table.
- **DECISION (2026-09-20, store-verified):** the physical `cusip_map` merge and
  the `sec_company` PK=`cik` redesign are **deferred** — they change reader/writer
  semantics for no query that needs them today. `sec13f_security_meta` (union) is
  applied. If a single CUSIP name is ever needed, add a read-only VIEW (not a
  second writer): `entry` is heterogeneous (1,794 objects + 3,405 bare JSON
  strings), so use `COALESCE(json_extract_string(entry,'$.ticker'), json_extract_string(entry,'$'))`.
  For CIK, use the existing ticker→cik map (`sec_company_tickers`, via
  `sec_edgar._cik_map()`), not a stored `equity_universe.cik` — a stored column is
  erased by both `replace_all` universe writers unless they re-attach it.
- **`swing_info` is KEPT.** The plan's "already inside `ticker_payload_cache`" is
  **false**: `ticker_payload_cache` holds yfinance financial statements, not
  `.info`. Dropping it loses `longBusinessSummary`/`beta` for Swing + Fundamentals.
- **`ticker_themes` materialization APPLIED** (`core/ticker_themes.py`): legacy
  keyword themes from `ticker_scores.themes` -> `theme_run_id='legacy-keyword'`,
  `level='legacy'` (659 tickers / 758 rows), idempotent.
- **Stop writing GICS on `ticker_scores` (4b): deferred to Phase 3.** The mirror
  columns stay (rawstore never drops columns); `company_profile` forces the join.
- **Wire `core/universe.py::classification_universe`** (already written) into
  Co-movement / Value Chain / Relatedness; `SEED` / `PILOT_N` are test overrides
  only.
- **No index prune** (`index_constituents` is reference-like); keep
  `edgar_financials` (fact table) and `watchlist` (spine).
- Old-name **compatibility views** for one cycle, then dropped (P2: remove
  `rawstore._apply_compat_views`; keep `_RENAMES` for older stores).
- Fix `core/index_registry.py` docstring (`entry` holds only `{"index": [...]}`).
- Update `POPULATION` keys + the 14 theme test files + `data_architecture.md`,
  `AGENTS.md`.
- **Gate: full pytest green.**

### Phase 2 — Fundamentals / Swing persistence
- New writers: `fundamentals_scores`, `swing_scores` (single writer, `replace_all`).
- New typed `fundamentals_gates`, `swing_gates` (`actual DOUBLE` + `unit TEXT`).
- `POPULATION` tests. **Gate.**

### Phase 3 — `company_profile` VIEW
- One row per `equity_universe` ticker; scalar joins only; no N-valued joins; no
  scalar theme.
- DoD tests: `count(*) = count(distinct ticker) = |equity_universe|`; boards
  iterate `classification_universe()`.

### Phase 4 — EDGAR store (files + `edgar.duckdb` content)

**Locked decisions (2026-09-20):**
- **Raw HTML = files, not a BLOB.** The durable record is
  `edgar_docs/{cik}/{accession}.html.gz` (dedicated dir, env `RC_EDGAR_DOCS_DIR`,
  gitignored), keyed by **`cik` + `accession`** (dual-class dedup; keeps 10-K/A +
  prior years). `.store/` stays DuckDB-only.
- `edgar_documents` is **metadata-only** (no `content_gzip`): `accession, cik,
  tickers, form, filed, fiscal_year, doc_url, path, sha256, bytes_raw,
  bytes_gzip, fetched_at`.
- `.store/edgar.duckdb` holds EDGAR **content**: `edgar_documents` (metadata),
  `edgar_facts`, `edgar_financials`, `chain_filings`, `chain_mentions`,
  `chain_meta`.
- `.store/raw.duckdb` keeps the **identity/GICS spine** — `sec_company_tickers`,
  `sec_company_sic` stay here (NOT moved) — plus everything non-EDGAR.
- Detection: per-CIK `data.sec.gov/submissions/CIK{10}.json` → `filings.recent`,
  newest `10-K`/`20-F`, over the classification-universe CIKs.
- Sync **trigger: DEFERRED** — finalize at P4c (entry point stays
  mechanism-agnostic: a `--sync-filings` CLI).

**Prereqs (already done):** `test_store_layout` allow-list += `edgar.duckdb`
(+ sidecars); `scripts/backup_store.py` backs up BOTH DBs under one stamp
(retain-N: 5 for `raw.duckdb`, 2 for `edgar.duckdb`); `core/job_lock.py`
(host-level lock across crawler / 13F / guidance).

**Status (2026-09-20): P4a–P4d DONE, P4e code done.**
- P4b: `rawstore` routes by `MANIFEST["db"]` (`_db_of`/`_db_path`,
  `connect(db=)`, `query(db=)`, `transaction(db=)`); `edgar_documents`,
  `edgar_facts`, `edgar_financials`, `chain_*` live in `edgar.duckdb`
  (real store migrated: 6,880 / 185 / 187 / 3 rows). `edgar_store` delegates to
  `rawstore`; paths resolve dynamically off `DB_PATH` (test isolation).
- P4c: `module_value_chain/sync.py` + `--sync-filings` CLI; newer-accession only,
  idempotent, shares the throttle + `job_lock`. `edgar_text.fetch_document` added.
- P4d: `edgar_bulk.companyfacts_rows` filters to `sec_edgar.CONCEPTS` (~10× smaller).
- P4e: `module_value_chain/precision.py` (precision ≥ 0.70 gate + max-40-edges).
- **Remaining (manual/network):** the live ≥$300M crawl and the human-labeled
  `module_value_chain/fixtures/labeled_50.json`. **Sync trigger mechanism still
  deferred** (entry point `--sync-filings`). Full suite green (1708).

**P4a — document files + `edgar_store` refactor (offline) — DONE (2026-09-20)**
- `core/edgar_docs.py`: `path_for`, `write_html` (gzip, atomic), `read_html`,
  `exists`, `count`; `{cik}/{accession}.html.gz`; CIK normalised.
- `core/config.py`: `EDGAR_DOCS_DIR` (`RC_EDGAR_DOCS_DIR`, default `BASE_DIR/edgar_docs`);
  `conftest` redirects it; `.gitignore` += `edgar_docs/`.
- `core/edgar_store.py`: `edgar_documents` is now **metadata-only** (`tickers`,
  `path`, `sha256`, `bytes_raw`, `bytes_gzip`; no `content_gzip`); content via
  `edgar_docs`.
- `scripts/backup_store.py`: `snapshot_edgar_docs()` (zip + `--keep-docs`, default 2).
- `test_store_layout`: guard that `EDGAR_DOCS_DIR` is outside `.store/`/`.cache/`.
- Tests: `core/tests/test_edgar_docs.py`; updated `test_edgar_store.py`.
  **Gate: full pytest green (1699).**

**P4b — `edgar.duckdb` content + rawstore routing (offline)**
- `rawstore`: `DB_PATHS={raw, edgar}`, `connect(db=...)`, `"db":"edgar"` per
  MANIFEST entry, `_table_db()`, `query(sql, db=...)`, `transaction(db=...)`,
  `ensure_all_tables()` per DB.
- Move `edgar_documents`, `edgar_facts`, `edgar_financials`,
  `chain_filings`/`chain_mentions`/`chain_meta` to `db="edgar"`; update callers
  that use `rawstore.query(... FROM chain_filings/edgar_*)`.
- One-time copy+drop migration → `migration_manifest`. No `ATTACH` needed (no
  EDGAR↔non-EDGAR SQL joins; VC `transaction()` is intra-file). Consolidate
  `edgar_documents` into rawstore routing (retire the separate DDL).
- **Gate: full pytest.**

**P4c — new-filing sync (network, no LLM)**
- `--sync-filings`: CIKs = `equity_universe` ⨝ `sec_company_tickers`; compare the
  stored latest accession to `submissions.recent`; on newer → download → file +
  metadata → re-slice → upsert; idempotent, shares the 10 req/s throttle
  (`sec_edgar._throttle`) + host job lock. Parallel `ThreadPoolExecutor(8)`;
  form-aware slicer (10-K Item 1 / 20-F Item 4 / 40-F; a 10-K/A wins only when
  its extracted Item 1 is non-empty and its section hash differs from the
  original); fallback filing `index.json` → complete `.txt`.
- `chain_filings` written **only** through the Value Chain writer
  (`module_value_chain/engine.py`).
- Tests (mocked): newer→fetch, same→no write, 10-K/A rule, resume.
- **⏰ Decide the trigger mechanism here.**

**P4d — bulk zips (official, free)**
- `companyfacts.zip` — https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip
- `submissions.zip` — https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip
- → `.cache/edgar_bulk/`, ingest, then **delete**.
- `companyfacts` → `edgar_facts`, grain `(cik, concept, period, form)`, typed
  `value DOUBLE` / `unit TEXT`, **filtered to the ~18 used concepts**
  (`sec_edgar.CONCEPTS`); `submissions` → `sec_company_sic` (coverage ≥ 90%).

**P4e — live crawl + gate (long, network)**
- **Fixture gate (10 CIKs):** throttle, resume, hash, Refresh zero-network,
  single writer. Then the ≥ $300M crawl into `edgar_docs/` + `edgar.duckdb`.
- Refresh re-slices only when `item_text` is null / `quality` is bad; same
  writer; ≤ 30 s.
- Value Chain quality gate: precision ≥ 0.70 on
  `module_value_chain/fixtures/labeled_50.json`; max 40 edges/ticker.

**Docs/invariants**
- Rewrite the stale "one database file, not one per domain" paragraph in
  `docs/data_architecture.md`; update store-layout in `AGENTS.md`; `glossary.md`;
  backup docs.

**Sizes:** `edgar_docs/` ~0.5–1.3 GB gzip; `edgar.duckdb` small (metadata +
extracted, facts filtered); **no BLOB duplication**.

### Phase 5 — UI / navigation
- **DONE:** single-level collapsible nav tree (`app/dashboard.py::_nav_groups`,
  `templates/dashboard.html`): caret, `aria-expanded`, `localStorage`
  (`rc.nav.collapsed`), active group auto-opens via `showTab`; existing tab keys
  unchanged. Value Chain default focal = most-connected ticker in the visible
  universe (`report._default_focal`, deterministic tie-break).
- **TODO:** vendored Plotly at `/static/vendor/plotly.min.js` (SPX Gamma currently
  uses `include_plotlyjs="cdn"`); shared token set + board shell for the three
  boards; `impeccable` then `design-review` + `audit`.

### Phase 6 — Relatedness full scale
- `llm_cache`, `relatedness_profiles`, `relatedness_attributes`,
  `relatedness_scores` = **raw.duckdb tables** (durable).
- Cache key: structured encoding of `model`, `thinking`, `effort`, `prompt_version`,
  `ticker`, `input_hash` (over exact prompt bytes). Mode stamp in `run_registry`.
- Prompt = `name` + `chain_filings.item_text` => **depends on Phase 4**.
- thinking-high only; `max_tokens = 8000`; retry cap 1; $ hard-stop ~1.5x expected;
  log $/ticker; pending TTL 6 h.
- Refresh = cached profiles + skip the rest (zero HTTP).
- **Profile job grain = `cik`**: dual-class (`BRK.A`/`BRK.B`) shares one
  `item_text`, so key on `cik + input_hash` and copy the profile to each ticker of
  that CIK (no double spend).
- 8 LLM workers, a **separate pool** from the SEC pool.
- `core/offpeak.py` docstring notes Chinese public holidays are treated as peak
  (conservative: we skip cheap hours, never bill peak).
- Pipeline: `PILOT_N=50` gate -> 3,634 (no 7,403 in the first job).

### Phase 8a — NMF shadow
- Normalize tags (`max_df 0.5`; synonyms in `module_relatedness/normalize.py`).
- Tag x ticker TF-IDF matrix.
- Choose k by **elbow + stability >= 0.90 (5 seeds) + min size >= 20**; min size
  wins ties.
- Agglomerative nested merge -> medium (the medium cut is clamped below the realised
  fine count, which merges at least one pair of fine themes; most medium themes keep one
  fine child, and the naming step reuses that child's name instead of asking the model,
  #433) / coarse (~25) with `parent_id`.
- Coarse option (#108, avo's card 2026-10-16 "Sektörden coarse"): `finalize_run(coarse="sector")` /
  `--coarse sector` parents each medium to the modal Morningstar sector of its members (`sector_coarses`,
  ids `<run>:sec-<slug>`), replacing the agglomerative coarse + `rebalance_coarses`. The default stays `nmf`
  until the cutover card.
- LLM naming (off-peak, thinking-low). A name already used in the run takes a suffix from
  its rarest tags (`Name (Tag)`), else `Name (2)`; a medium theme with a single fine child
  reuses that child's name with no model call (#433).
- Write `relatedness_themes` / `relatedness_theme_tags(theme_id, tag, rank)` /
  `relatedness_theme_members` / `theme_runs` **beside** the old vocabulary; emit a
  stability report.
- Membership = ticker x theme_id weight (normalize per ticker; keep >= 0.10 else
  top-3; drop empty themes).
- Sector fallback formula (used by 8b): each sector's most common **medium** theme
  among profiled tickers in that sector, else a named neutral default.

### Phase 8b — Cut-over
- If the report passes, pin `theme_runs.is_current`.
- Materialize ALL rows (profiled NMF + no-profile lexicon + sector fallback) into
  `ticker_themes` under that run.
- `core/taxonomy.py` becomes a thin loader over the pinned run; `assign_themes` is
  a SELECT.
- **Consumer contract:** every reader filters
  `theme_run_id = (SELECT id FROM theme_runs WHERE is_current) AND level = 'medium'`
  (the canonical level); `theme_id` is the join key, `theme` display-only.
- Update the 14 theme test files; delete leftover theme lists in ER v2 /
  Co-movement config; `ROTATION_ETFS` theme group stays ETF labels (decoupled).

### Phase 7 — Verification
- Full pytest + e2e: Classification Refresh with network blocked; `company_profile`
  row-count; store allow-list = `{raw.duckdb, edgar.duckdb}`; dual-DB backup.
- `design-review`; dashboard restart.

## Definition of done

- Full unit suite green; e2e for the new/modified boards (offline refresh, restart).
- GICS consistent everywhere (NVDA + one 20-F + one dual-class examples).
- `company_profile` = one row per `equity_universe` ticker; boards iterate the
  classification universe.
- `.store` = `{raw.duckdb, edgar.duckdb}` (+ sidecars); `backup_store.py` covers both.
- One theme object (`ticker_themes`) on a pinned `theme_run_id`.

## Pins (no invented policy)

| Item | Pin |
|---|---|
| `max_tokens` (profiles) | 8000; retry cap 1 (2x, billed) |
| $ hard-stop | ~1.5x expected (~$8) |
| Value Chain precision | P >= 0.70 on a labeled 50; max 40 edges/ticker; Refresh <= 30 s |
| Zip SIC coverage floor | >= 90% |
| DF threshold / synonyms | `max_df 0.5`; `module_relatedness/normalize.py` |
| `theme_runs` | `id, created_at, k_fine, k_medium, k_coarse, jaccard, is_current` |
| Pending TTL | 6 h |
| Job mutex | host-level file lock (crawler, 13F, guidance) |
| Cache key | structured (sorted keys), `input_hash` over exact prompt bytes |
| 13F latest | newest `period`, then newest `filing_date` |
| Backup retain-N | `raw.duckdb` keep 5; `edgar.duckdb` keep 2 |

## Mechanical inventory (repo search, old names)

`asymmetric_universe` 43 refs / 17 files · `ticker_store` 99 / 26 ·
`ticker_cache` 73 / 15 · `sec_facts` 24 / 9 · `ticker_registry` 21 / 9 ·
`index_members` 19 / 8 · `universe_meta` 19 / 9.

Reproduce with:
`rg -n "\b(asymmetric_universe|ticker_store|ticker_cache|sec_facts|index_members|ticker_registry|cusip_ticker|sec13f_cusip_names|sec_company_tickers|sec_company_sic|universe_meta|sec13f_universe_meta|etf_holdings_cache|swing_info|THEMES|THEME_KEYWORDS|classify_themes|assign_themes|SECTOR_THEME_FALLBACK|SORTED_THEMES)\b"`.

## Theme-touching test files (13)

`app/tests/test_dashboard.py`, `core/tests/test_db_persistence.py`,
`core/tests/test_index_registry.py`, `core/tests/test_rawstore_atomicity.py`,
`core/tests/test_taxonomy.py`, `core/tests/test_ticker_store.py`,
`module_asymmetric_growth/tests/test_asymmetric_growth_hunter.py`,
`module_comovement/tests/test_comovement.py`,
`module_fundamentals/tests/test_fundamental.py`,
`module_fundamentals/tests/test_guardrail.py`,
`module_sector_rotation/tests/test_rotation_store.py`,
`module_swing_potential/tests/test_swing.py`,
`scripts/tests/test_migrate_datasets.py`.

## Remaining work — correct order

**Done (all full-suite green):**
- Phase 0/1 — renames; selected merges (`sec13f_security_meta`, `index_tickers`
  view); `ticker_themes` + legacy backfill; compat views retired.
- Phase 2 — `fundamentals_scores`/`_gates`, `swing_scores`/`_gates` + writers.
- Phase 3 — `company_profile` VIEW.
- Phase 4 (A) offline — `edgar_facts`, `core/edgar_bulk.py`, `core/job_lock.py`,
  parallel Value Chain fetcher (shared throttle).
- Phase 6 schema — `llm_cache`, `relatedness_profiles`, `relatedness_attributes`,
  `relatedness_scores`.
- **Phase 6 code** — `module_relatedness/store.py` (durable profiles, structured
  cache key, mode stamp in `run_registry`); engine migrated off `.cache` JSON,
  CIK-grained, 8-worker LLM pool; `run` persists `relatedness_scores`.

**Order (code first, spend last):**

```
6-live   optional validation: 50-name pilot, then 3,634        [off-peak; NOT urgent]
         (path is durable + off-peak-gated + $-capped + CLI; see llm_offpeak.md)
8a       NMF shadow (depends on 6 profiles)                    [off-peak naming]
8b       taxonomy cut-over (pin theme_runs.is_current)         [gate: pytest]
4        EDGAR store P4a->P4b->P4c->P4d->P4e (files + edgar.duckdb)  [P4a/P4b gate: pytest]
5        UI / navigation (offline; may overlap any time)
7        full verification + design-review + restart + dual-DB backup
```

Notes:
- The **live pilot is validation only** (parse / $/ticker / cache-key), never a
  prerequisite for code. Off-peak = weekends 24h + weekday hours outside
  01:00–04:00 / 06:00–10:00 UTC, so it is never time-critical.
- Phase 8 depends on Phase 6 profiles; Phase 5 and Phase 4-live are independent
  and may be interleaved.
- `edgar_financials` stays per-ticker JSON; the fact spine is `edgar_facts`
  (Phase 4 decision A) — do not repurpose `edgar_financials`.

## Sequencing (original, for reference)

```
0 -> store-layout + backup PR (empty edgar.duckdb)   [gate]
  -> 1 (compat views, no index prune)                 [gate: pytest]
  -> 2                                                [gate]
  -> 3
  -> 4 (fixture 10 CIKs -> $300M crawl)               [gate: fixture]
  -> 5 (may overlap)
  -> 6 (cache-key freeze -> 50-name -> 3,634)         [gate: pilot]
  -> 8a (shadow + stability report)
  -> 8b (taxonomy cut-over)
  -> 7 (full suite)
```

## Risks

- Phase 1 touches ~300+ references -> compatibility views + tests.
- Phase 4 first crawl (dual DB + `chain_filings` writer + 10 r/s) -> fixture gate
  first.
- Phase 8 cut-over -> shadow-first, pinned `theme_run_id`.
- Do not alter `equity_universe.quote` (AG `technical_from_quote` reads it).
- thinking-high profiling is ~4x slower than non-thinking (~hours sequentially);
  8 LLM workers (separate pool) keep it inside off-peak + the 6 h pending TTL.
- `edgar.duckdb` size (~2-4 GB gzip) and dual-DB backup time.
