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
  boards read one set: the spine's Tiers A and B, no market-cap floor, every
  security type** via `core/universe.py::classification_universe` (PR 2, avo
  30 Oct 2026). The type is data: a board that screens operating companies asks
  for them (`operating_only=True`), which leaves out the identity-master types
  in `identity.types.EXCLUDED_TYPES` (funds / ETFs, SPACs, preferred / warrants /
  units; Identity R4, #87); `BOARD_OPERATING_ONLY` is each board's default
  (Relatedness and Value Chain: operating only). A name with no type, or an
  `unknown` / `other` one, stays even then and is counted in the funnel note.
- Prune rule (single): spine + reference dictionaries + fact tables + per-ticker
  scores KEPT (`ticker_payload_cache`, `edgar_financials`, `watchlist`); board
  artifacts restricted to the classification universe.
- EDGAR official/free only (bulk zips + per-document `.htm`); no npm/sec-api/GPL.
- Relatedness profiles: **thinking-high**; theme naming: **thinking-low** (off-peak).
- Themes: NMF, **nested** (fine ~150 -> medium = max(2, n_fine // 2), halved from the realised fine count (#433) -> coarse ~25 via agglomerative
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
theme. Boards iterate `classification_universe()` (Tiers A and B; with
`operating_only=True`, operating companies only).

Reference dictionaries (never pruned): `sec_company_tickers` + `sec_company_sic`
(**ticker-keyed** — the CIK-parent/`cusip_map` merges are deferred, see Phase 1);
`sec13f_*`; `sec13f_security_meta` (PK = ticker). 13F "latest" tie-break: newest
`period`, then newest `filing_date`.

### Phase 8a — NMF shadow
- Normalize tags (`max_df 0.5`; synonyms in `module_relatedness/themes.py:82`).
- Tag x ticker TF-IDF matrix.
- Choose k by **elbow + stability >= 0.90 (5 seeds) + min size >= 20**; min size
  wins ties.
- Agglomerative nested merge -> medium (the medium level is halved from the realised
  fine count: ``k_medium = max(2, n_fine // 2)``; the clamp below ``n_fine`` is only a
  safety net; the naming step reuses a fine child's name when a medium theme ends with a
  single fine child, #433) / coarse (~25) with `parent_id`.
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

## Pins (no invented policy)

| Item | Pin |
|---|---|
| `max_tokens` (profiles) | 8000; retry cap 1 (2x, billed) |
| $ hard-stop | ~1.5x expected (~$8) |
| Value Chain precision | P >= 0.70 on a labeled 50; max 40 edges/ticker; Refresh <= 30 s |
| Zip SIC coverage floor | >= 90% |
| DF threshold / synonyms | `max_df 0.5`; `module_relatedness/themes.py:82` |
| `theme_runs` | `id, created_at, k_fine, k_medium, k_coarse, jaccard, is_current` |
| Pending TTL | 6 h |
| Job mutex | host-level file lock (crawler, 13F, guidance) |
| Cache key | structured (sorted keys), `input_hash` over exact prompt bytes |
| 13F latest | newest `period`, then newest `filing_date` |
| Backup retain-N | `raw.duckdb` keep 5; `edgar.duckdb` keep 2 |

