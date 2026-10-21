# Glossary — dataset & field renames

Single old→new map for the Classification data-model refactor
(`docs/classification_design.md`, Phase 1). Keep this file in sync with
`core/rawstore.py` `_RENAMES` and `MANIFEST`.

## Tables

| Old | New | Note |
|---|---|---|
| `asymmetric_universe` | `equity_universe` | spine: membership (the SEC exchange file's Nasdaq / NYSE / CBOE rows) + GICS |
| `asymmetric_universe_meta` | `equity_universe_provenance` | |
| `ticker_store` | `ticker_scores` | AG per-ticker scores |
| `ticker_cache` | `ticker_payload_cache` | raw per-ticker payload |
| `sec_facts` | `edgar_financials` | per-ticker JSON (`annual`/`quarterly`/`meta`) in `edgar.duckdb`; the `(cik, concept, period, form)` fact spine is `edgar_facts` |
| `index_members` | `index_constituents` | reference; **not pruned** |
| `ticker_registry` | `index_tickers` | becomes a **view** |
| `cusip_ticker` + `sec13f_cusip_names` | `cusip_map` | **deferred** — optional read-only VIEW later; no physical merge |
| `sec_company_tickers` + `sec_company_sic` | `sec_company` (+ child) | **deferred** — keep ticker-keyed; use read-time `_cik_map()` |
| `universe_meta` + `sec13f_universe_meta` | `sec13f_security_meta` | PK = ticker (applied) |
| `etf_holdings_cache` | `etf_holdings` | |
| `swing_info` | **(kept)** | plan's "already inside `ticker_payload_cache`" premise is **false** |

## New tables (this work)

| Table | Role |
|---|---|
| `run_registry` | runtime pins (profile mode, current theme run id, migration markers) |
| `edgar_documents` (in `.store/edgar.duckdb`) | filing **metadata** (accession/form/dates); raw HTML is `edgar_docs/{cik}/{accession}.html.gz` |
| `ticker_themes(ticker, theme_id, theme, level, theme_run_id)` | runtime theme SSoT |
| `relatedness_profiles`, `relatedness_attributes`, `relatedness_scores` | Relatedness |
| `relatedness_themes`, `relatedness_theme_tags`, `relatedness_theme_members`, `theme_runs` | NMF themes |
| `fundamentals_scores`, `swing_scores`, `fundamentals_gates`, `swing_gates` | per-ticker scores + gates |

## Status

- **Pure renames APPLIED** (Phase 1a, `rawstore._RENAMES`): `equity_universe`,
  `equity_universe_provenance`, `ticker_scores`, `ticker_payload_cache`,
  `edgar_financials`, `index_constituents`, `etf_holdings`. Real `.store/raw.duckdb`
  migrated (7,403 / 5,313 / 7,461 / 6,880 / 9,302 / 34 rows preserved).
- **Compat VIEWs RETIRED** (Phase 1 P2): `rawstore._apply_compat_views` removed;
  `_RENAMES`/`_apply_renames` kept so an older store still migrates. Old names are
  gone (not aliased).
- **Merges APPLIED**: `sec13f_security_meta` (`rawstore._MERGES`, union of
  `universe_meta` + `sec13f_universe_meta`; 2,271 rows) and the `index_tickers`
  VIEW (`rawstore._VIEWS`, `index_constituents` ⨝ `equity_universe`; 9,302
  rows). Old `universe_meta` / `sec13f_universe_meta` / `ticker_registry`
  dropped. `_RENAMES` is the 1:1 hook; `_MERGES`/`_VIEWS` cover the 2:1 / view
  cases.
- **`ticker_themes` APPLIED** (Phase 1, `core/ticker_themes.py`): runtime theme
  SSoT; legacy keyword themes materialized from `ticker_scores.themes` with
  `theme_run_id='legacy-keyword'`, `level='legacy'` (659 tickers / 758 rows).
  Backfill is idempotent (`python -m RevenueChaser.core.ticker_themes --backfill`).
- **Deferred (deliberate — changes reader/writer semantics):**
  - `cusip_map`: no physical merge, no partial-upsert. Optional read-only VIEW
    later (must use `COALESCE(json_extract_string(entry,'$.ticker'), json_extract_string(entry,'$'))`
    — 3,405 of 5,199 `entry` values are bare JSON strings, not objects).
  - `sec_company` PK=`cik` + child: deferred; `sec_company_tickers` already maps
    ticker→cik (10,403). Use `sec_edgar._cik_map()` / a `cik_for(ticker)` helper.
    A stored `equity_universe.cik` is only viable once **both** `replace_all`
    writers (`_save_universe`, formerly also `backfill_universe_gics`, removed in R3) re-attach `cik` on every
    write (Phase 3/4).
  - `swing_info`: **kept**. `ticker_payload_cache` holds yfinance financial
    statements, **not** `.info`; dropping `swing_info` would lose
    `longBusinessSummary` / `beta` for Swing + Fundamentals.
  - Stop writing GICS on `ticker_scores` (4b): deferred to Phase 3
    (`company_profile` will force the join; mirror columns stay meanwhile).
- `run_registry` and `edgar_documents` exist now (Phase 0 / first gate).
- **Phase 2 APPLIED**: `fundamentals_scores` / `fundamentals_gates` (writer
  `module_fundamentals/scores.py`) and `swing_scores` / `swing_gates` (writer
  `module_swing_potential/scores.py`); single writer, `replace_all`, typed gates
  `(ticker, gate, rule_id, passed, actual, unit)`.
- **Phase 3 APPLIED**: `company_profile` VIEW (`rawstore._VIEWS`) — one row per
  `equity_universe` ticker, scalar joins to `ticker_scores` / `fundamentals_scores`
  / `swing_scores`; no N-valued joins, no scalar theme.
- **Phase 4 (option A) PARTIAL**: `edgar_facts(cik, concept, period, form, value,
  unit)` added — the bulk `companyfacts.zip` fact spine, separate from the
  per-ticker `edgar_financials` JSON the financials boards read. `core/edgar_bulk.py`
  downloads/ingests the official zips (then deletes them) and covers SIC from
  `submissions.zip`. `core/job_lock.py` = host-level mutex. **Applied:** the
  parallel 10-K/20-F document fetcher, the live bulk download, and the ≥$300M
  crawl (Phase 8 live run — filings in `edgar.duckdb` + `edgar_docs/`).
  **Still open:** the Value Chain precision ≥0.70 gate.
- **Phase 6 code APPLIED**: `module_relatedness/store.py` — durable profiles
  (`relatedness_profiles` + `relatedness_attributes`), structured cache key,
  mode stamp in `run_registry`; engine is CIK-grained with an 8-worker LLM pool
  and persists `relatedness_scores`. The live pilot/full run is pending off-peak.
- **Phase 4 (planned, locked 2026-09-20)**: raw 10-K/20-F HTML becomes **files**
  at `edgar_docs/{cik}/{accession}.html.gz` (durable, `RC_EDGAR_DOCS_DIR`);
  `edgar_documents` becomes **metadata-only** (no BLOB); `.store/edgar.duckdb`
  holds EDGAR content (`edgar_documents` metadata, `edgar_facts`,
  `edgar_financials`, `chain_*`); `sec_company_tickers`/`sec_company_sic` stay in
  `raw.duckdb` (identity/GICS spine). New-filing sync via per-CIK
  `submissions.recent`; trigger mechanism deferred to P4c.
