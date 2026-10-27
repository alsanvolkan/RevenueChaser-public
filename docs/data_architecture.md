# Data Architecture — RevenueChaser

This document describes the data pipeline and where data lives at each stage.
Companions: [`data_retrieval.md`](data_retrieval.md) for how data gets in and
out (Fetch vs Refresh), [`e2e_testing.md`](e2e_testing.md) for how that is
verified.

The guiding principle: **three DuckDB files for all structured raw/derived data**
(`raw.duckdb` + `edgar.duckdb` + `prices.duckdb`, the daily price tables of #156; a fourth,
`history.duckdb`, for result history, is
backed up already; schema 11 declares its 17 result-history tables, and the first `core/history.record_run` creates the file), **clearable `.cache/` for
regenerable outputs, and files only where files are intrinsically better**
(immutable audit artifacts, downloaded binaries and the raw `edgar_docs/` HTML tree).

The Discord archive was a fourth store file until October 2026. It is now its own app
(`discord-archive`), with its own database, schema version and backup, because nothing here ever
read it for analysis.

## The two data folders — read this first (for future maintainers & AI)

| Folder | Semantics | May you delete it? |
|---|---|---|
| **`.store/`** | **DURABLE.** Everything in `.store` is the source of truth. It holds exactly **three** DuckDB files today (a fourth, `history.duckdb`, appears with the first recorded board run, #278; see its row below) (plus tiny `.rc-proc-<pid>` presence markers and `*.lock` job locks, see Concurrency model): `.store/raw.duckdb` (structured raw + derived data, and the identity/GICS spine incl. `sec_company_*`), `.store/edgar.duckdb` (EDGAR **content**: `edgar_documents` metadata, `edgar_facts`, `edgar_financials`, `chain_*`) and `.store/prices.duckdb` (the daily price tables: `price_bars_daily`, `price_actions`, `price_checks`, and the SPX daily levels `index_levels_daily`). Raw 10-K/20-F HTML is the one file-based durable artifact, under `edgar_docs/{cik}/{accession}.html.gz`. They grow over time and CANNOT be regenerated from the app — losing them loses what only accumulates (the option-flow prints, one Rotation snapshot per date, one Co-movement run per as-of date), the ingested 13F data and the raw filings. | **NO.** Treat like database files. Back them up (`scripts/backup_store.py` covers the DB files, the `edgar_docs/` tree as a zip, and the SPX Gamma tree as an incremental mirror); never `rm -rf`. |
| **`.cache/`** | **ARTIFACTS.** Fully regenerable outputs: `*_latest.html` dashboards, `*_latest.json` payloads, PNGs, timestamped archives, and downloaded SEC 13F source zips. | **YES.** Delete any time; re-running the relevant module regenerates everything. |

Rule of thumb: if deleting it loses *history* or *ingested* data → it belongs in
`.store/`. If deleting it only costs a re-run → it belongs in `.cache/`.

### Test and harness isolation

Both roots are **relocatable by environment variable**, read once in
`core/config.py`:

| Env var | Overrides | Default |
|---|---|---|
| `RC_STORE_DIR` | `DATA_DIR` (and therefore `rawstore.DB_PATH` + the sibling `edgar.duckdb`, `history.duckdb` and `prices.duckdb`) | `<repo>/.store` |
| `RC_CACHE_DIR` | `OUTPUT_DIR` / `CACHE_ROOT` | `<repo>/.cache` |
| `RC_EDGAR_DOCS_DIR` | `EDGAR_DOCS_DIR` (raw 10-K/20-F HTML tree) | `<repo>/edgar_docs` |

They are read **at import**, and that is deliberate rather than incidental. Some
twenty module-level constants derive from these roots — `app/runners.py`'s
`*_LATEST`, `core/rawstore.py`'s `DB_PATH`, every module config's paths — and
several modules re-bind them into their own namespace, so monkeypatching the
origin after import reaches only some of them. Setting the variable before the
first RevenueChaser import is the only redirect that reaches all of it.

Consequences worth knowing:

- The pytest suite sets both in `pytest_configure`, so **no test can touch your
  real `.store/` or `.cache/`**. Before this, a route test hitting `?refresh=1`
  would unlink real rendered boards.
- The end-to-end harness sets them per test and runs the dashboard as a
  subprocess (see [`e2e_testing.md`](e2e_testing.md)).
- Unset, everything resolves to the repo-local defaults exactly as before.

If you add a path constant, derive it from `OUTPUT_DIR`/`DATA_DIR` — never from
`BASE_DIR` directly, or it will escape the redirect. `core/tests/test_store_layout.py`
enforces this.

## Classification (sector and industry) — single source of truth

`core/gics_map.py` is the **only** place sector labels are defined. Since Phase 1 (#265, #266) classification is
**UW's two levels**: the sector is one of UW's own 11 Morningstar names (Basic Materials, Communication Services,
Consumer Cyclical, Consumer Defensive, Energy, Financial Services, Healthcare, Industrials, Real Estate,
Technology, Utilities) and the industry is the provider's text of the same row. The GICS tree (groups,
sub-industries), `resolve_gics`, the industry alias table and `core/identity/gics_tree.py` are gone.

- `canonical_sector(raw)` maps a vendor or older spelling onto the Morningstar name through `SECTOR_ALIASES`
  (the old GICS names `Information Technology`, `Health Care`, `Financials`, `Consumer Discretionary`,
  `Consumer Staples`, `Materials` are adapter keys now); anything else reads `Unclassified`.
  `module_sec_13f/engine.py` re-exports it for legacy imports.
- `sector_path(sector, industry) -> [sector, industry]` is the filter path (`filter_fields()["gics"]`);
  `GicsPath(sector, industry)` is what `core.universe.spine_gics()` / `gics_of()` return. Everything that *reads* a
  stored value treats `Other` and `Unclassified` alike and emits `Unclassified` (`gics_map.UNCLASSIFIED`,
  `is_unclassified`).
- **No other module may define its own sector map.** Vendor strings are adapter keys inside `gics_map` and must
  not be stored or displayed as a sector.
- Non-equity instruments (crypto, indices, macro) are **not** sectors: the 13F universe meta
  (`module_sec_13f/universe_info.py`) carries an asset class for them and their sector is unknown
  (`Unclassified`).
- Every board reads `sector` + `industry` on its cards; `equity_universe` carries them as typed columns
  (`ticker_scores` keeps its old `industry_group` / `sub_industry` columns, written NULL), and the
  `index_tickers` VIEW exposes `name`/`sector` by joining `index_constituents` to `equity_universe`. Screener tabs
  expose a cascading **Sector → Industry** filter through the shared filter bar
  (`core/static/rc_filters.js`, `FILTERS_JS` / `FILTERS_CSS` in `core/reporting_common.py`), which has the two
  levels.
- Rotation board: sector ETF rows (named like the Morningstar sectors) expand to a cap-weighted
  sector → industry tree built from `equity_universe` (`sector_etf_trees`), not a 10-name ETF snapshot;
  theme/region/bond rows keep their ETF holdings.
- The shared universe's **members** are the Nasdaq / NYSE / CBOE rows of the SEC
  `company_tickers_exchange.json`, pulled fresh on every universe fetch (#264); the Yahoo screener only joins
  market data (cap, price, `quote`) onto that list by ticker, and a listed name it misses is kept with NULL
  cap, price and quote.
- The shared universe carries **no vendor sector** — Yahoo's screener response
  omits `sector`/`industry` (verified). Since R3 (#86) the **identity master**
  (`security_master`, built by `core/identity/refresh.py`) is the single writer
  of the classification in `equity_universe`; since Phase 1 (#265) that is
  **UW's two levels**: `sector` is one of UW's own 11 Morningstar names (Basic
  Materials, Communication Services, Consumer Cyclical, Consumer Defensive,
  Energy, Financial Services, Healthcare, Industrials, Real Estate, Technology,
  Utilities; `docs/openapi.yaml`, "Market General Sector"; Yahoo's `.info` uses
  the same names) and `industry` is the provider's industry text from the same
  row. Since #272 the sector is decided in this order, and `security_master.sector_source` names the step:
  `manual` (the corrections table), `sp1500` (the S&P 1500 Wikipedia lists, for the names on them only, written
  through `core.gics_map.canonical_sector` so the column keeps the 11 Morningstar names), `provider_uw`,
  `provider_yf` (Yahoo, only where UW has no usable sector), `sic_explicit` (the 263-code SIC table in
  `core/identity/sic_sector.py`, same Morningstar names), `type_rule`, then `Unclassified`. The industry stays
  UW's text for every name (Yahoo's where UW has none); the lists' GICS sub-industry names and the SIC text are
  never written to `industry`, and the SIC ranges and the GICS rename stay gone.
  `core/identity/apply.py` copies the master's sector and industry over
  after every identity refresh and again after every universe fetch
  (`_save_universe` carries the stored values forward, then the master goes
  back on top), plus UW's `next_earnings_date` from the last good `uw_sector`
  cache row. `industry_group` and `sub_industry` stay as columns (no schema
  bump) but are written NULL. A level the sources could not fill is
  `Unclassified`; a universe ticker the master has no row for keeps its stored
  values (never blanked). The SIC fallback reads the code from the SEC submissions cache first, else from
  `sec_company_sic` (`core/sic_registry.py`; `python -m RevenueChaser.core.sic_registry --refresh`), and gives a
  sector only for the 263 listed codes (an unlisted code gives nothing, no range guess). The raw SIC itself
  (`sic`, `sic_description`, `sic_group2`, `sic_source`) is written on `security_master` for every ticker that has one,
  from caches the identity job already holds, whatever decided the sector.

Schema version `12` (`_SCHEMA_VERSION` in `core/rawstore.py`; v3 added
`industry_group` / `sub_industry`, and `equity_universe` also gained
`industry`; v6 added the identity master tables `security_master`,
`identity_conflicts`, `identity_source_cache` and `identity_runs`, issue #85, and `board_settings`, issue #40; v7 added
`relatedness_profiles.attempts`, issue #114; v8 added the `job_runs` table, issue #124 PL5 (its writer is `app/jobs.py`, issue #282, see Jobs below),
`edgar_financials.fetched_at`, issue #161, the `price_bars_daily` table, issue #156, `catalyst_events`, issue #3,
`orphan_designations` / `orphan_lookups`, issue #190, and the shared `forecast_ledger`, issue #128 —
manifest only, no writers yet; #164; v9 added the four SIC columns of `security_master`, issue #272; v10 added the 22 G1 tables of #292 (manifest only, no writers yet); v11 is the history redesign C: 8 `<table>_changes` tables in `raw.duckdb` (the store appends a rule-A row in the same transaction as every write to `equity_universe`, `security_master`, `identity_source_cache`, `index_constituents`, `watchlist`, `board_settings`, `catalyst_events` and `orphan_designations`; the first call seeds them), `result_runs` plus 16 run-keyed row tables in `history.duckdb` (`core/history.record_run`), the key rebuild of `price_bars_daily` (`date` becomes `session_date`) and `orphan_lookups` (key `(ticker, session_date)`), and new columns on `theme_runs` and `leaps_flow_prints`; v12 expands the 13F ingest store to non-lossy INFOTABLE rows, adds COVERPAGE/SUMMARYPAGE fields to `sec13f_submissions`, makes `sec13f_holdings` keyed on `(accession, line_no)` and adds the derive-side VALUE-units rescale (#208)). Adding manifest columns auto-migrates via
`ALTER TABLE ... ADD COLUMN`; the whole manifest (renames, merges, views,
column additions, compressed-column rebuilds) is applied on each process's
first store call — see "Schema on first use" below. `equity_universe.next_earnings_date TEXT` (Phase 1,
#265) is such a column, and so are the six FIGI columns of `security_master` (#267, no bump), its four SIC
columns (#272, the v9 bump), its four identifier columns (`cusip`, `cusip_source`, `isin`, `isin_source`, #127, no bump) and `fetched_at DOUBLE` on `chain_meta`, `leaps_cache`, `rotation_holdings`, `sec13f_security_meta`, `swing_info` and `ticker_themes` (#127, no bump; NULL on rows written before the column reads as unknown, never as age 0): they are added by that `ALTER TABLE` on the first store call; the version is a label
written to `migration_manifest`, and the bump is what makes the change a gated one (manual `.store` checkpoint, see
below).

Each store file carries its own schema stamp (#26 PL7-a): a `store_schema` row, key `schema_version`, equal to `_SCHEMA_VERSION`. The ensure path writes it when the row is absent or older than the code, and leaves a newer stamp as stored; an upgrade of an older stamp is a warning and keeps the old value in the same table (`upgraded_from`, `upgraded_at`), so a restored older file leaves a trace. `rawstore.store_schema_mismatch()` lists those (`db`, `file`, `stored`, `code`); a read logs a warning and does not raise. An existing file with no stamp is stamped with the current version on that first ensure — an already-wrong file would be marked current; `schema_drift()` remains the backstop for a retyped column, a changed key or an extra column. The table is additive (`CREATE TABLE` plus one row) in each of `raw`, `edgar`, `prices` and, when that file exists, `history`. It does not bump `_SCHEMA_VERSION` and does not need a Drive checkpoint. `history.duckdb` is stamped by `ensure_history`, not created just to hold a stamp.

Refusing writes on a mismatch is off (#26 PL7-b). `SCHEMA_REFUSE_WRITES` in `core/store_schema_refuse.py` is `False`. Set `RC_STORE_SCHEMA_REFUSE=1` to refuse a data write when that file's stamp is ahead of this code: newer, or not a version number. A file with no stamp is not refused: the ensure stamps it current. The same check runs before the ensure, so an old checkout never renames, merges or ALTERs a newer file; the ensure then logs one warning and the writes are refused. A refused write is also a skipped `job_runs` row (the run log goes quiet while jobs fail), so read the Server log first. `store_schema_mismatch()` opens the files read-write, so run the gate check with the dashboard stopped or on the fresh backup copy. Readers still only warn. `RC_STORE_SCHEMA_OK=1` is the escape for a deliberate migration: writes are allowed and the warning is suppressed. The exception is `StoreSchemaMismatch`; it names the file, the stored version, the code version and `python -m RevenueChaser.scripts.restore_store --latest <raw|edgar|history|prices>`. It does not run at import. Leave the switch off unless that refusal is intended.

## Storage layout

| Path | Contents | Clearable? |
|---|---|---|
| `.store/raw.duckdb` | Structured raw + derived data (tables below) + the identity/GICS spine (`sec_company_*`) | No — durable source of truth (see `.store` semantics above) |
| `.store/edgar.duckdb` | EDGAR **content**: `edgar_documents` (metadata only), `edgar_facts`, `edgar_financials`, `chain_filings`/`chain_mentions`/`chain_meta` | No — durable |
| `.store/history.duckdb` | Result history (#287, 2.6-1; schema 11): `result_runs` (one header row per board run: session, run time, quality, model version, params, funnel, payload) and 16 run-keyed row tables (`ag_scores_history`, `earnings_cards_history`, …). Written only by `core/history.record_run`, in one transaction; a session board keeps one whole run per session (`session_snapshot`), a run board keeps every run. Read views `result_sessions` and `<table>_by_session`. Longer backup retention than `raw.duckdb` (5 nightly snapshots, and an off-machine copy; its weekly copy was dropped in #342 — it grew ~6.55 GB/yr against raw's 603 MB). **The file does not exist until the first recorded run** (`rawstore.ensure_history()`); `rawstore.history_db_path()` and `_db_path("history")` resolve it, and the nightly backup and `restore_store` skip it while it is missing. `forecast_ledger` stays in `raw.duckdb`. The 8 change tables live in `raw.duckdb` and ride its backups | No — durable |
| `.store/prices.duckdb` | Daily prices (#156, moved here before the fill, 2026-10-09): `price_bars_daily`, `price_actions` and their watermarks `price_checks` (the shape of `source_checks`, which stays in raw for the identity stamps), and the SPX daily levels `index_levels_daily` (D03, #293, moved here before its writer, 2026-10-10). One file, so a chunk's bars, actions and watermarks commit in one transaction (`rawstore.atomic(..., db="prices")`); about 2.4 GiB after the `period="max"` fill, kept out of raw's weekly copies. Made by the first store call; a store from before has the two tables (and the price rows of `source_checks`; from #293 also the empty `index_levels_daily`) moved over once by that call: copied, checked row by row (`EXCEPT`), then dropped from raw; each table is added to the `migration_manifest` row `price_tables_to_prices_db` in the transaction that removes it from raw. A conflicting row stops that table's move with a warning and overwrites nothing. Two processes starting together may both run the move: what the other one already moved is left alone and logged at INFO. No `_SCHEMA_VERSION` bump (the #72 precedent): an older checkout recreates the empty raw tables and ignores this file. Backup: 2 nightly snapshots, no weekly copy (append-only, so the newest holds every row); off this machine once a week, 1 copy | No — durable |
| `edgar_docs/{cik}/{accession}.html.gz` | Raw 10-K/20-F HTML (the durable raw-document record; keyed by CIK + accession). Dir via `RC_EDGAR_DOCS_DIR` | No — durable; backed up as a tree |
| `.cache/sec_13f/*_form13f.zip` | Downloaded SEC 13F source archives (re-downloadable from SEC) | Yes — deletable after ingest (tracked in `ingested_zips`) |
| `.cache/` | All regenerable outputs: `*_latest.json`, `*_latest.html` (flat and nested), PNGs, timestamped archives | Yes — delete anytime; re-run regenerates |
| `module_spx_gamma/data/` (override `SPX_GAMMA_DATA_ROOT`) | SPX Gamma data tree: the UW GEX and greek files per date under `raw/<date>/`, `brier.json`, `spx_gamma_gex_history.json`, parsed files, charts and reports. Inside the repo (git-ignored `data/`), not under `.store/`. UW serves only recent days, so the raw day files cannot be fetched again | No — durable. Mirrored by the nightly backup (see Backups); #179 moves it into DuckDB later |

## Pipeline stages

### Stage 1 — External acquisition (network)
Providers pull from Unusual Whales, yfinance, SEC EDGAR (XBRL companyfacts, 13F
ZIPs), FRED, OpenFIGI. Nothing is persisted at this
stage except caches (Stage 2).

### Stage 2 — Raw caches (written at fetch time)
Cached in `.store/raw.duckdb` (and in `.store/edgar.duckdb` for EDGAR content)
so re-scans reuse data instead of re-hitting rate-limited APIs. Each row carries
a `fetched_at` timestamp; readers apply a TTL and refetch on expiry.
`no_fetch=True` (model-only mode) reads regardless of age and never touches the
network.

| Table | Content | DB | TTL |
|---|---|---|---|
| `ticker_payload_cache` | Per-ticker yfinance payload (financial statements) | raw | 1 day (`CACHE_TTL_DAYS`) |
| `edgar_financials` | EDGAR companyfacts (annual/quarterly/meta) per ticker | edgar | none (immutable) |
| `edgar_documents` | EDGAR filing metadata (accession, form, dates). Append: the first write of an accession wins, a later one is a no-op (#299, D16); the daily sync and the Value Chain Fetch store a downloaded 10-K/20-F through one path (`module_value_chain/edgar_text.document_slice`), a stored doc is re-sliced, never downloaded or written again. `source` 'sec_archives', `run_at` | edgar | none (immutable) |
| `edgar_facts` | EDGAR fact spine `(cik, concept, period, form)` | edgar | none (immutable) |
| `swing_ohlc` | 2y daily OHLCV per ticker (tabular columns); one writer `module_swing_potential/ohlc_cache.write` (C8), which only the Swing/Fundamentals SPY fallback still calls: Co-movement and the Relatedness peer market read D01 since #379 | raw | none today |
| `catalyst_events` | Biotech catalysts (trial readouts, PDUFA, AdComs, …) keyed `(source, source_event_id)`: a slipped date updates the row, never adds one (the store records the change in `catalyst_events_changes`); `event_date` + `date_precision` (day/month/quarter/half); writers arrive with #186-#192 | raw | per fetch |
| `orphan_designations` / `orphan_lookups` | FDA orphan designations per ticker (OOPD) / the outcome of each ticker's lookup. **Writer rules (#190):** skip and report rows whose `date_designated` is None (one NULL key aborts the whole batch); group OOPD's one-record-per-approval rows by key into `approvals`; store `error` as the provider's code; `query` is the query the lookup ended on: when OOPD caps a page (`error` `page_capped`) the provider retries once with a narrower query, so a ticker's stored `query` can differ between sessions only because of that day's cap, and the broad query first asked (`retry_from` in the provider result) is not persisted; readers gate on `orphan_lookups.match` and, since the key is `(ticker, session_date)` (schema 11), take each ticker's latest session (upsert keeps designations that vanish upstream) | raw | per fetch |
| `price_bars_daily` | Daily bars per `(ticker, session_date)` (schema 11; was `(ticker, date)`): complete New York sessions only (`session_date` is the NY session date, never local/UTC), written from 16:30 ET (`core/calendar.py`); the first write of a key wins and a bar is never rewritten; OHLC nominal as served at `fetched_at` (a price ≤ 0 is not stored; `dropped_no_close` counts bad bars seen, not rows lost), later splits and dividends applied on read from `price_actions` (`core/prices.daily_bars`); `volume` is the value served at write time (#156 R1). Writer `core/prices.ensure_daily_bars`, job `daily_bars`, task RC-Daily-Bars 22:35. Readers (`daily_bars`, windowed by SQL, K8a): Swing and Fundamentals (#383 R3), Asymmetric Growth (nominal: momentum from the last 252/50/63/10 sessions, one read per 500-name batch, #380 S4), the forecast outcomes, and Co-movement plus the Relatedness peer market (adjusted closes over the last 2 years, `module_comovement/ohlc.py` `price_frame`, #379 D01 switch; their Fetch downloads nothing); and, since #372 E5e, the Earnings Fetch's reaction window (`module_earnings_radar/stored_inputs.ohlc_bars`, one windowed nominal read; UW's `etf_ohlc` is asked only for a ticker whose newest stored bar is not the latest complete session, or, from 09:30 to 16:30 New York, for a name reporting today or on the previous trading day, before or after its open) | prices | none (event table, never pruned) |
| `swing_info` | yfinance `.info` per ticker. `fetched_at DOUBLE` (#127) is the fetch time; a row from before the column is NULL (unknown, never 0) | raw | `OHLC_TTL_DAYS` |
| `leaps_cache` | LEAPS latest snapshot (Earnings reads it) + the older snapshots list, frozen since #377 L3 (carried unchanged, never appended). `fetched_at DOUBLE` (#127); NULL on older rows is unknown, never 0 | raw | none today (latest only) |
| `rotation_holdings` | Per-ETF holdings + RS rows for today. `fetched_at DOUBLE` (#127); NULL on older rows is unknown, never 0 | raw | same-day |
| `tradewhisperer_*` | Legacy TradeWhisperer caches — board removed 2026-09-30; rows kept untouched, no longer in the MANIFEST | raw | — |
| `equity_universe` | Shared US universe (members = the SEC exchange file's Nasdaq / NYSE / CBOE rows, with Yahoo market data joined on, #264) + provenance snapshot | raw | refreshed on scan |
| `sec_company_tickers` | SEC CIK map (ticker → cik/name); **retired** (#301 D21): nothing writes it, and the readers use `sec_exchange_file` first — it stays as the CIK map's second source, ahead of a live `company_tickers.json` read | raw | frozen |
| `sec_company_sic` | SIC per CIK — the fallback behind the SEC submissions code for the SIC sector step (#272) | raw | until refetched |
| `comov_runs` / `comov_nodes` / `comov_members` / `comov_lag` | Co-movement board: dated return-cluster runs, clusters, members; groups formed a quarter earlier with their held-out-quarter test | raw | per Fetch run (latest as_of wins) |
| `chain_filings` / `chain_mentions` / `chain_meta` | Value Chain board: latest 10-K/20-F Item 1/4 text + supplier/customer edges. `chain_meta.fetched_at DOUBLE` (#127) is the fetch time; NULL on older rows is unknown, never 0 | edgar | per fetch (resumable by ticker) |
| `llm_cache` | DeepSeek (and, for a `--provider grok` run, SuperGrok: #388) prompt/response cache (Relatedness + Value Chain) | raw | none (immutable) |
| `ticker_themes` | Runtime theme SSoT `(ticker, theme_id, theme, level, theme_run_id)`. `fetched_at DOUBLE` (#127) is when the row was written; NULL on older rows is unknown, never 0 | raw | per theme run |
| `sec13f_security_meta` | Shared ticker→{name,sector} across dashboards (merged from old `universe_meta` + `sec13f_universe_meta`). `fetched_at DOUBLE` (#127); NULL on older rows is unknown, never 0 | raw | manual |
| `sec13f_enrichment` | 13F enrichment (price/short/insider); writer retired (#300), no reader since #375 F2 (the board reads `ownership_snapshot` and D01); the rows stay | raw | none |
| `sec13f_edgar_backfill` | Retired: the EDGAR backfill is gone and nothing reads it; no writer either, apart from the legacy import `scripts/migrate_rawdata_to_duckdb.py`, so the entry is flagged MANIFEST `"retired": true` (#289) and scheduled to leave the manifest at the next bump (the table leaves the manifest in the 8→9 bump) | raw | none |
| `etf_holdings` | Rotation ETF holdings, latest per ETF (top 10, the provider that answered, the fetch time); written by the holdings job, read by the Rotation Fetch (#303, D25); its history is `etf_holdings_changes` | raw | manual (Fetch holdings) |
| `watchlist` | Watchlist tickers. Writer: the LEAPS pin's `POST /api/watchlist/toggle/<t>` through `rawstore.toggle_watchlist`, which adds or removes that one ticker in one transaction (#306); every add and removal is kept in `watchlist_changes` | raw | manual |
| `price_actions` | Key `(ticker, ex_date, action)`. `ticker`, `ex_date DATE`, `action`, `value DOUBLE`, `session_date DATE`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`; writer `core/prices.py` (#156): yfinance dividends (amount), splits (ratio, 4.0 = 4-for-1) and capital gains (amount); first write wins | prices | none |
| `index_levels_daily` | D03 (#293, M-SPX-0 #376): writer `core/index_levels.py` (yfinance `^GSPC`, `auto_adjust=False`, symbol `^GSPC`, source `yfinance`), two entry points under one lock: the best-effort last step of RC-Daily-Bars (`scripts/daily_bars.py --nightly --index-levels`; at most 1 yfinance request a night, none when the latest complete session is stored or before the fill, a per-session marker under `.cache/index_levels/`, no retry; its failure never changes the exit code) and the attended one-time `period="max"` fill (`scripts/daily_bars.py --index-fill --confirm`, one request, needs SPY's D01 fill). Append, the first write of a key wins; complete sessions only: from 2025 a calendar trading day up to the latest complete session, before 2025 a date of SPY's stored D01 bars or any `^GSPC` date before SPY's first (rule C). The SPX Gamma snapshot reads its 8-week candle window from it (`module_spx_gamma/candles.py::stored_spx_candles`) and makes no candle request of its own (D03), and the Settings / Data health board reports newest session vs latest complete, row count and source. In prices.duckdb beside `price_bars_daily` since #293 (avo 2026-10-10; a store from before has the empty raw table moved over by the first call). Key `(symbol, session_date)`. `symbol`, `session_date DATE`, `open DOUBLE`, `high DOUBLE`, `low DOUBLE`, `close DOUBLE`, `volume DOUBLE`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | prices | none |
| `quote_snapshot` | G1 (#292); writers `core/quotes.py` (#294): one row per ticker per complete New York session per source, append (first write of a key wins), a row only with a positive cap, nothing written inside the session or by a run that crosses a session boundary. `yfinance` rows from the universe pull (Phase A screener quotes, Phase B `fast_info` fills), `uw` rows from the universe job (UW screener, batches of 100, when a UW key verifies). Boards still read `equity_universe` (#279 D6). Key `(ticker, session_date, source)`. `ticker`, `session_date DATE`, `source`, `market_cap DOUBLE`, `price DOUBLE`, `shares_outstanding DOUBLE`, `week_52_high DOUBLE`, `week_52_low DOUBLE`, `payload BLOB`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `short_interest` | D10 (#296): writer `module_earnings_radar/inputs/short_interest.py`, task RC-Earnings-Inputs (Tue-Sat 01:30, after RC-Daily-Bars; `core/nightly_inputs.py`); read by 13F Short% since #375 F6 (`module_sec_13f/enrich.load_short_interest`: newest non-null `si_float` per ticker, shown as a percent) and, since #372 E5c, by Earnings R5 (`module_earnings_radar/stored_inputs.short_rows`, the newest session inside `MAX_LAG` 3, any source; the row is parsed with `UWClient._parse_short_row`, so the live answer and the stored one give the same inputs, and UW's `/short_screener` is called only for the tickers the store misses). The calendar tickers then the 13F tickers inside the UW cap; source `uw_short_screener`, then `uw_interest_float`, then `yfinance` (si_float and total_float only); values as served (si_float a fraction); first write per session wins. Key `(ticker, session_date)`. `ticker`, `session_date DATE`, `market_date DATE`, `short_interest DOUBLE`, `si_float DOUBLE`, `total_float DOUBLE`, `days_to_cover DOUBLE`, `fee_rate DOUBLE`, `rebate_rate DOUBLE`, `short_shares_available DOUBLE`, `payload JSON`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `vendor_financials` | G1 (#292). Writer `core/vendor_data.record` (#297, D12): the statements an AG/universe Fetch (yfinance, names without EDGAR years), an Earnings Fetch (UW `/financials`, annual and quarterly as served) or a Financials Fetch (yfinance) fetched; change history (rule A): a row is appended only when the typed fields or the payload differ from the newest row of its key (UW `inserted_at`/`updated_at` ignored), `valid_from` = the run's session, `status` 'present'; a period leaving the vendor's window is not closed. `operating_income` is yfinance's `ebit`. Read by `core/vendor_data.read_financials` (#385, FIN-5): the statements a store-only Financials Refresh models from (`source` `yfinance`, `report_type` `annual`, `status` 'present', the newest row per statement and period, merged per fiscal year) — zero network, and the read writes nothing. Key `(ticker, source, statement, report_type, period_end, run_at)`. `ticker`, `source`, `statement`, `report_type`, `period_end DATE`, `valid_from DATE`, `status`, `revenue DOUBLE`, `gross_profit DOUBLE`, `operating_income DOUBLE`, `ebitda DOUBLE`, `net_income DOUBLE`, `operating_cash_flow DOUBLE`, `total_assets DOUBLE`, `inventory DOUBLE`, `receivables DOUBLE`, `deferred_revenue DOUBLE`, `rpo DOUBLE`, `total_debt DOUBLE`, `interest_expense DOUBLE`, `payload JSON`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `vendor_valuation` | G1 (#292). Writer `core/vendor_data.record` (#297, D12): the yfinance `.info` of every AG/universe name a Fetch asked, and of the Financials ticker and its comps peers; one row per session, written only by a complete-session run (from 16:30 ET; an intraday run writes none), first write of a session wins. Read by `core/vendor_data.read_financials` (#385, FIN-5): the `.info` half of a store-only Refresh's model inputs (newest session first) — a ticker an intraday Fetch left no row for takes its market snapshot from the raw payload cache instead. Key `(ticker, source, session_date)`. `ticker`, `source`, `session_date DATE`, `enterprise_value DOUBLE`, `ebitda DOUBLE`, `total_revenue DOUBLE`, `net_income_to_common DOUBLE`, `trailing_pe DOUBLE`, `beta DOUBLE`, `currency`, `long_business_summary`, `payload BLOB`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `earnings_history` | G1 (#292). Writer `module_earnings_radar/earnings_store.record` (#298, D13): the two UW answers an Earnings Fetch already reads per scored ticker (`/earnings/{ticker}`, `/stock/{ticker}/earnings`; `endpoint` names which), captured by the scoring workers and flushed after the run with the job's id; a print is appended only when its typed fields or payload changed (change history, daytime runs too), a failed read closes nothing. Key `(ticker, endpoint, report_date, run_at)`. `ticker`, `endpoint`, `report_date DATE`, `valid_from DATE`, `status`, `report_time`, `actual_eps DOUBLE`, `eps_estimate DOUBLE`, `surprise_pct DOUBLE`, `expected_move_perc DOUBLE`, `post_earnings_move_1d DOUBLE`, `fiscal_period_end DATE`, `report_type`, `payload JSON`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `estimate_snapshots` | D14 (A6, #323): writer `module_earnings_radar/inputs/estimates.py` (yfinance `earningsTrend`, one read per calendar ticker), task RC-Earnings-Inputs (Tue-Sat 01:30, after RC-Daily-Bars; `core/nightly_inputs.py`); up to four periods per ticker, a field Yahoo does not give stays NULL; first write per session wins. Read by Earnings R1 since #372 E6 (`module_earnings_radar/stored_inputs.estimates`, the `0q` row of the newest session inside `MAX_LAG` 5; the rule is logged only, weight 0). Key `(ticker, session_date, period)`. `ticker`, `session_date DATE`, `period`, `eps_est_analysts DOUBLE`, `eps_est_avg DOUBLE`, `eps_est_low DOUBLE`, `eps_est_high DOUBLE`, `eps_est_year_ago DOUBLE`, `eps_est_growth DOUBLE`, `rev_est_analysts DOUBLE`, `rev_est_avg DOUBLE`, `rev_est_low DOUBLE`, `rev_est_high DOUBLE`, `rev_est_year_ago DOUBLE`, `rev_est_growth DOUBLE`, `eps_trend_current DOUBLE`, `eps_trend_7d DOUBLE`, `eps_trend_30d DOUBLE`, `eps_trend_60d DOUBLE`, `eps_trend_90d DOUBLE`, `eps_rev_up_7d DOUBLE`, `eps_rev_up_30d DOUBLE`, `eps_rev_down_7d DOUBLE`, `eps_rev_down_30d DOUBLE`, `payload JSON`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `identity_cusip` | G1 (#292). Writer `core/identity_cusip.py` `record` (#207): OpenFIGI's CUSIP answers from the 13F map step, change history (rule A: a changed answer appends a row, a lost match one `absent` row, nothing is updated or deleted), `source` `openfigi`. Key `(cusip, run_at)`. `cusip`, `valid_from DATE`, `status`, `ticker`, `figi`, `share_class_figi`, `share_class`, `name`, `exch_code`, `security_type`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `ownership_snapshot` | G1 (#292). Writer `core/ownership.record` (#300, D20): source `yfinance`, the shares outstanding, insider % and institutional % of the `.info` an AG/universe Fetch already gets for the D12 valuation; source `uw_info`, UW `/stock/{t}/info` `outstanding` of every 13F totals ticker, asked by the 13F Fetch after its board render (one call each, only for tickers not stored yet this session, at most `UW_MAX_CALLS_PER_FETCH` = 2,500 per Fetch, never-stored tickers first, then the oldest stored). One row per session, written only by a complete-session run (a weekend or pre-open run maps to the last complete session, an intraday run writes none, and the 13F Fetch then asks nothing), first write of a session wins; percentages are fractions as served, a field the source did not give is NULL. No board reads it yet. Key `(ticker, session_date, source)`. `ticker`, `session_date DATE`, `source`, `shares_outstanding DOUBLE`, `insider_pct DOUBLE`, `institutional_pct DOUBLE`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `sec_mf_file` | D8 (#301 F0): writer `core/sec_files.py` (SEC `company_tickers_mf.json`, one GET a New York weekday night, no retry), the best-effort first step of RC-Daily-Bars (`scripts/daily_bars.py --nightly --sec-files`); change history (rule A): a row when `cik`, `series_id` or `symbol` change, one 'absent' row (key and stamps) when a class leaves, `valid_from` the run's session, source `sec_mf`; a file under 10,000 classes writes nothing. No board reads it. Key `(class_id, run_at)`. `class_id`, `valid_from DATE`, `status`, `cik`, `series_id`, `symbol`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `sec_exchange_file` | D21 (#301 F0): writer `core/sec_files.py` (SEC `company_tickers_exchange.json` through `sec.exchange_map`, one GET a New York weekday night, no retry), the best-effort first step of RC-Daily-Bars (`scripts/daily_bars.py --nightly --sec-files`); change history (rule A): a row when `cik`, `name` or `exchange` change, one 'absent' row (key and stamps) when a ticker leaves, `valid_from` the run's session, source `sec_exchange`; a file under 3,000 Nasdaq / NYSE / CBOE rows writes nothing. The universe and identity jobs still pull the file themselves. Key `(ticker, run_at)`. `ticker`, `valid_from DATE`, `status`, `cik`, `name`, `exchange`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `earnings_calendar` | D26 (#304): writer `module_earnings_radar/inputs/earnings_calendar.py` (UW calendar, the trading days of the next four weeks, premarket and afterhours), task RC-Earnings-Inputs (Tue-Sat 01:30, after RC-Daily-Bars; `core/nightly_inputs.py`); source `uw`; first write per session wins. Read by the Earnings Fetch since #372 E5a (`module_earnings_radar/stored_inputs.calendar_items`, the newest session inside `MAX_LAG` 1): a covered `(earnings_date, report_session)` pair is rebuilt from `item` and UW is called for the pairs the sweep did not reach, and always for today's and the next trading day's pairs. Key `(session_date, earnings_date, ticker, report_session)`. `session_date DATE`, `earnings_date DATE`, `ticker`, `report_session`, `sector`, `full_name`, `marketcap DOUBLE`, `expected_move_perc DOUBLE`, `street_mean_est DOUBLE`, `pre_earnings_close DOUBLE`, `post_earnings_close DOUBLE`, `report_time`, `item JSON`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `macro_series` | D27 (#305, FRED part): writer `module_earnings_radar/inputs/macro.py` (DGS2/5/10/20/30, T10Y2Y, T10YIE, DCOILWTICO; 8 FRED calls), task RC-Earnings-Inputs (Tue-Sat 01:30, after RC-Daily-Bars; `core/nightly_inputs.py`); source `fred`, `session_date` the NY session on or before `obs_date`; first write of an observation wins. Read by Earnings R6 since #372 E5d (`module_earnings_radar/stored_inputs.dgs10_map`, the newest 80 stored DGS10 rows while the newest is at most 3 trading sessions old; FRED is called when that map is empty or stale). Key `(series_id, obs_date, source)`. `series_id`, `obs_date DATE`, `source`, `value DOUBLE`, `frequency`, `proxy_for`, `session_date DATE`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `vol_index_daily` | D28 (#305, Cboe part; #371): writer `module_earnings_radar/inputs/vol_index.py`, task RC-Earnings-Inputs (Tue-Sat 01:30, after RC-Daily-Bars; `core/nightly_inputs.py`); Cboe's public daily-price CSV per symbol (VIX, VIX9D, VIX3M, VIX6M, VIX1Y, VVIX, OVX; 7 calls, `source` `cboe`) plus MOVE from yfinance (`^MOVE`, 1 call, `source` `yfinance`); the newest 60 rows per symbol a night, a symbol's first run keeping its whole file (`FIRST_RUN_LIMIT` = None); `session_date` the row's own trading date as served, `operator` NULL; first write of a key wins. Read by SPX Gamma since D28 (`module_earnings_radar/stored_inputs.vol_index_rows`, the VIX-family closes of the newest session inside `MAX_LAG` 3, keyed with the caret the live payload uses): the snapshot asks yfinance only when that session is missing, and the manifest's `input_sources` says which it used. The Earnings board itself reads neither series (R6 is the 10Y-yield-beta veto); the sweep writes them for SPX Gamma. Rotation still reads live. Key `(symbol, session_date, source)`. `symbol`, `session_date DATE`, `source`, `open DOUBLE`, `high DOUBLE`, `low DOUBLE`, `close DOUBLE`, `operator`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `cftc_cot` | D29 (#305, CFTC part; #371): writer `module_earnings_radar/inputs/cftc_cot.py`, task RC-Earnings-Inputs (Tue-Sat 01:30, after RC-Daily-Bars; `core/nightly_inputs.py`); CFTC Socrata Traders-in-Financial-Futures, the newest 4 rows of the E-mini S&P 500 (1 call, `source` `cftc`), parsed by `core/providers/cftc.py::map_row`; `report_type` `TFF`, `market` `E-MINI S&P 500`, `release_date` the report date + 3 days unless the source states one, `session_date` the New York session on or before `report_date`, `raw_row` the source row; first write per report wins. Read by SPX Gamma since D29 (`module_earnings_radar/stored_inputs.cot_row`, the newest session inside `MAX_LAG` 10 — the COT is weekly, published Friday for the Tuesday before, so a current row is already ~6 sessions old): the snapshot reads CFTC live only when that session is missing, and the manifest's `input_sources` says which it used. The Earnings board itself reads neither series. Key `(report_type, market, report_date)`. `report_type`, `market`, `report_date DATE`, `release_date DATE`, `open_interest DOUBLE`, `asset_mgr_long DOUBLE`, `asset_mgr_short DOUBLE`, `asset_mgr_change_long DOUBLE`, `lev_money_long DOUBLE`, `lev_money_short DOUBLE`, `lev_money_change_long DOUBLE`, `dealer_long DOUBLE`, `dealer_short DOUBLE`, `dealer_change_long DOUBLE`, `raw_row JSON`, `session_date DATE`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `economic_calendar` | D37 (#307): writer `module_earnings_radar/inputs/economic.py` (UW economic calendar, this week and next, 1 call), task RC-Earnings-Inputs (Tue-Sat 01:30, after RC-Daily-Bars; `core/nightly_inputs.py`); `event_ts` UW's UTC time as epoch seconds; first write per session wins. Read by Earnings R6 since #372 E5d (`module_earnings_radar/stored_inputs.rate_event_overlap`): an FOMC, or a report whose text names CPI, inside the report date ±1 trading day vetoes an extreme yield beta whose t-stat is at least 2; the event's day is its New York date, the newest session whose own rows span the window answers (the writer keeps every session's sweep, so a moved release keeps its old date in the older ones), and a window no one session spans gives `None` (no veto). Key `(session_date, event_ts, event, type)`. `session_date DATE`, `event_ts DOUBLE`, `event`, `type`, `forecast`, `prev`, `reported_period`, `raw_row JSON`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `source_checks` | Key `(dataset, source, key)`. `dataset`, `source`, `key`, `checked_at DOUBLE`, `run_id`; the generic per-key "last checked" stamp (rule A); `core/quotes.py` (#294): `quote_snapshot`/`yfinance`/`__rate_limited__` = Phase B's last stop on Yahoo's rate limit; the price watermarks moved to `price_checks` (#156) | raw | none |
| `price_checks` | Key `(dataset, source, key)`, the shape of `source_checks`; writer `core/prices.py` (#156), in the chunk's transaction: `price_actions`/`yfinance` = per-ticker actions watermark; `price_bars_daily`/`yfinance_max` = fill marker | prices | none |
| `sec13f_filers` | G1 (#292), manifest only, no writers yet. Key `(accession)`. `accession`, `cik`, `period`, `name`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | raw | none |
| `edgar_companyfacts` | G1 (#292), manifest only, no writers yet. Key `(cik, taxonomy, concept, unit, period_start, period_end, accn)`. `cik`, `taxonomy`, `concept`, `unit`, `period_start`, `period_end DATE`, `accn`, `value DOUBLE`, `form`, `fy INTEGER`, `fp`, `filed DATE`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | edgar | none |
| `sec_submissions` | G1 (#292). Writer `core/sec_submissions.record` (#299, D15): the daily EDGAR sync (`edgar_sync`, RC-ValueChain-Sync 13:00) asks `submissions/CIK*.json` once for every CIK of the whole `equity_universe` and stores each `filings.recent` row once per accession (append, first write wins); a failed CIK writes nothing. A Refresh writes nothing. `source` 'sec_submissions', `cik` without leading zeros. Read by the Fundamentals board since #386 F5 (`module_fundamentals/events.corporate_events`, windowed reads bounded to 2000 CIKs per statement and all of them inside one `rawstore.session()`, so one connect per store file per call): a card whose filer filed an 8-K carrying item 1.02, 2.01, 2.06, 3.01, 4.01, 4.02, 5.01 or 5.03 in the last 365 days shows those item codes — display only, never scored, ranked or frozen. Key `(cik, accession)`. `cik`, `accession`, `form`, `filing_date DATE`, `primary_document`, `items`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | edgar | none |
| `sec_submissions_cik` | G1 (#292). Writer `core/sec_submissions.record` (#299, D15), from the same answers: change history (rule A), a row only when name, SIC, SIC text, entity type, tickers or exchanges differ from the CIK's newest row; `valid_from` = the run's session, `status` 'present'; a CIK the SEC answers 404 gets one 'absent' row. Each answered CIK also gets a `source_checks` watermark (dataset 'sec_submissions', key = CIK) for Data Health. Key `(cik, run_at)`. `cik`, `valid_from DATE`, `status`, `name`, `sic`, `sic_description`, `entity_type`, `tickers JSON`, `exchanges JSON`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE` | edgar | none |

### Stage 3 — Normalized / derived stores
Computed from raw caches and persisted in `raw.duckdb`.

| Table / VIEW | Derived from | Notes |
|---|---|---|
| `forecast_ledger` | Frozen forecasts of every board (#128 / #62 D-3) | **append-only, first write wins** (nothing is ever updated; don't call `update_columns`/`replace_all`/`delete`/`clear` on it); `forecast_date` = the NY session of the newest complete bar used; `target_date` = the session the forecast resolves on (the forecast date for picks). Outcomes live in the derived `forecast_outcomes` table (62b-1, #380 S0) |
| `forecast_outcomes` | `forecast_ledger` picks + D01 bars (`price_bars_daily` / `price_actions`, nominal basis) | **derived, keyed upsert** (`latest_cache`): recomputed from the ledger and the bars, never a fact of its own. Writer `core/outcomes.py` (CLI `scripts/forecast_outcomes.py`, task RC-Forecast-Outcomes 23:45, after RC-Daily-Bars); one row per pick and horizon (30 and 90 calendar days). See "The forecast-outcomes writer" below |
| `forecast_baselines` | the `equity_universe` snapshot of a freeze date (`equity_universe_changes`) + D01 bars (nominal outcomes, adjusted closes for the volatility) | **derived, keyed upsert** (`latest_cache`, #25 HR-1, added with no schema bump): what the same day's universe did, so a family's hit rate can be compared with something. Writer `core/baselines.py` (CLI `scripts/forecast_baselines.py`, task RC-Forecast-Baselines 23:50, after RC-Forecast-Outcomes); one row per freeze date, horizon (30 and 90) and basis (`universe`, `vol_decile_1` … `vol_decile_10`) |
| `index_constituents` | UW screener (sp500), DIA holdings (dow), ticker-exchanges (nasdaq); sp600 from the last accepted S&P 1500 snapshot in `identity_source_cache` (#302) | replace per slug; written by `index_registry.refresh_lists` (job `index_lists`, task RC-Index-Lists Saturday 10:00, CLI `scripts/index_lists.py`); a list under its floor (`MIN_INDEX`: 400 / 25 / 1,000 / 480) is not written; the first sp600 fill needs the user's OK for that run; joins and leaves are in `index_constituents_changes`. sp600 shows as "S&P SmallCap 600" on the badge and in the filter once stored (#214); `fetch_index` never fills it (Common K1) |
| `index_tickers` (VIEW) | `index_constituents` ⨝ `equity_universe` | adds `name`/`sector`; replaces the old `ticker_registry` |
| `ticker_scores` | Asymmetric Growth cards (ex-SQLite `tickers.db`) | single writer = market-cap scan; others read-only |
| `fundamentals_scores` / `fundamentals_gates` | Fundamentals scan | typed gates `(ticker, gate, rule_id, passed, actual, unit)` |
| `swing_scores` / `swing_gates` | Swing Potential scan | same gate shape |
| `company_profile` (VIEW) | `equity_universe` + `ticker_scores` / `fundamentals_scores` / `swing_scores` | one row per universe ticker; scalar joins only |
| `relatedness_profiles` / `relatedness_attributes` / `relatedness_scores` | Relatedness LLM run | CIK-grained profiles + similarity |
| `relatedness_themes` / `relatedness_theme_tags` / `relatedness_theme_members` / `theme_runs` | NMF over Relatedness | nested fine/medium/coarse themes; pinned run in `run_registry` |
| `leaps_flow_prints` / `leaps_daily_scores` | LEAPS flow prints + daily scores (ex-SQLite `leaps/radar.db`) | PK `id` / (ticker, date) dedup; schema-versioned |
| `rotation_snapshots` | Rotation engine cross-section (up to 78 symbols live, 67 backfilled; D01 weekly closes since #374 M-ROT-2) | one row per ETF per session (time series; `as_of_date` is the session, #283) |
| `sec13f_submissions` / `sec13f_holdings` / `sec13f_cusip_names` / `sec13f_ingested_zips` | Parsed 13F ZIPs (ex-SQLite `sec13f.db`) | non-lossy ingest store; zips deletable after ingest; `sec13f_holdings` is keyed on `(accession, line_no)` and keeps every INFOTABLE row (puts, PRNs and non-equity rows included); the derive filters long equity; a zip's submissions row, names and marker land together, after its streamed holdings |
| `sec13f_totals` | Per-ticker quarterly totals + per-holder metrics | rebuilt from the ingest store, store only (`engine.recompute_totals`; the Rebuild totals button, or every Fetch) |
| `cusip_ticker` | CUSIP → ticker: a cache of `identity_cusip`'s current map plus the legacy entries whose issuer prefix (first 6 characters) OpenFIGI maps to the same ticker and whose issue number is not 90-99 (#207 option B minus 9x, `engine.legacy_fallback`; no issuer-name matching) | replaced (old rows deleted) only when the derive with the new map passes the guards (`engine.recompute_totals`); a refused derive keeps the stored map and totals |
| `run_registry` | runtime pins | profile mode, current theme run id, migration markers |

**One session date (#283, ADR-3).** `core/calendar.py` is the only place that turns a run time into a session
date; no table or board does its own `date.today()` or UTC-date cut.
- `session_for(run_at)` (rule B) gives the New York session and a quality: on a trading day from 09:30 ET it is that
  day, `intraday` before 16:30 ET and `complete` from 16:30; before 09:30, on a weekend or on a holiday it is the
  previous trading day, `complete`. A session-keyed writer stores nothing for `intraday` (first write wins on
  `complete`); a run-keyed table keeps every run.
- `to_session_date(value)` is the read helper for old run-time columns (epoch, ISO text, `date`): a value with a time
  goes through `session_for`, a date-only value becomes the latest session on or before it. Naive text is read as New
  York time. `provenance.session_of` is the never-raising wrapper for labels.
- Rule C: the holiday table stays 2025-2028 and raises outside it. Sessions before 2025 come from the stored price
  bars (D01 / D03 fill), not from this module. Early-close days count as full days (conservative).
- Not in this slice: Rotation's weekend and holiday rows (wave 5, with #182) and migrating old tables.
- Open items, kept out of this slice so no board display changes: `provenance._day` still reads epoch as the UTC
  date; the move to `provenance.session_of` happens in wave 5 (#182, Rotation and Co-movement), because it changes
  data-date labels (a Saturday fetch would read as Friday). Early-close days (13:00 ET) stay full days on purpose
  (conservative: 13:00-16:30 reads `intraday`); revisit only if a writer needs the early-close time.

**The forecast-ledger writer (#128).** `core/ledger.py` is the only code that writes `forecast_ledger`,
through `rawstore.write_rows` alone. A board freezes its shown picks at the end of a **Fetch** (`no_fetch=False`;
a Refresh never freezes), from `app/runners.py` (`run_swing`, `run_fundamentals`, `run_asymmetric_growth`) into
the module's own `freeze.py`, which maps the board to ledger rows. The rule, applied by `ledger.freeze_board`:
- the data is one complete New York session D, and D is the latest complete session (`core/calendar.py`:
  30 minutes after the 16:00 ET close; early-close days use 16:00, which is conservative);
- the run is outside the US session window (09:30–16:30 ET);
- the payload is real (not a fixture, not "no data", not empty; Swing and Fundamentals need SPY not stale, AG needs
  at most 1% scoring errors);
- Swing and Fundamentals: each shown pick's last D01 bar (`price_bars_daily`, the bars the board scored from since
  #383 R3 C12/K8b) is D and was written after D's close + 30 min (`fetched_at`); SPY's too, read from D01 first and
  from `swing_ohlc.cached_at` only when D01 lacks SPY's D bar (the board's SPY fallback). **All or nothing:** one
  shown pick without such a bar refuses the whole board for the day (a thinner board would not be the forecast the
  user saw). A scored name whose last D01 bar is before SPY's newest D01 session (a halt, a Daily Bars gap) drops
  at the scan as `stale_bars` (a data-gap step in the funnel), so it never becomes a pick (#434 review S1); when more
  than 5% of the names lack that bar it is a failed Daily Bars run, none drops and the freeze refuses the day. The note
  names the picks in board rank order, split by cause: *need their D bar* (D01 has no D bar of SPY yet: Daily Bars
  has not run; only this cause says a later Fetch can complete the day, once Daily Bars has run) and *no D bar after
  the close* (SPY has its D bar and the pick has none: a halted name or a Daily Bars gap; a later Fetch will not
  change it unless Daily Bars is run again). D01 is append-only, so a pick's own bar always carries its own
  session's write time and cannot tell the two apart (#434 review S2).
  AG: SPY's D01 bar for that session, written after its close + 30 min (`ledger.d01_has_session`, #380 S4); not the
  universe snapshot behind the quote prices, and not `ohlc_gaps` (one name's gap must not refuse the board);
- first write wins per (source, day), not just per row: the key includes `subject`, so the writer checks the whole
  (source, day) under one lock before writing, and a second Fetch the same day writes nothing.

`forecast_date` and `target_date` are `datetime.date` (`rawstore._dedupe` keys on the Python value). `rank` is the
1-based position on the board; `model_version` is `"<SELECTION_VERSION>+<hash of the board's thresholds>"`, pinned by
a golden test per board. `run_id` is the job id (`jobs.current_job_id()`). A freeze outcome (frozen or skipped) is
a *note* (`ledger.add_note`) that `jobs.execute` puts in the job's own `notes` list (`/api/jobs`), never in its
`message`: the dashboard shows `message` in a modal `alert()` (`templates/dashboard.html`, `poll()`), which a Fetch
that freezes every day must not open. `ledger.freeze_board` also logs each outcome as a `forecast ledger: ...` line,
which the Logs tab shows in the Server log. A note is never a `JobMessage`, because `run_universe_refresh` runs three
boards in a row. A job that fails after a board froze appends its notes to its `error` text
(`... (before the error: Froze 4,251 AG rows for 2026-10-02.)`), because the dashboard shows `error` for a failed job.

**The forecast-outcomes writer (#62 62b-1, #380 S0).** `core/outcomes.py` is the only code that writes
`forecast_outcomes`, through `rawstore.write_rows` alone; it reads `forecast_ledger` and never writes it, and it makes
no network call (`prices.daily_bars` reads the store). For every ledger row with `target_type` 'pick' it writes one row
per horizon (`HORIZONS` = 30 and 90 calendar days), keyed by the ledger's key plus `horizon_days`:
- the window is the sessions after `forecast_date` up to the last session on or before `forecast_date` + N
  (`window_end`); the forecast day itself is never in it;
- `ref_price` is the open of the first session after `forecast_date` (card O-1's default: the first price the pick
  could be bought at), from the same `daily_bars(basis="nominal")` series as the bars, so a split inside the window
  changes nothing; when that session's bar is missing or has no usable open, a later open never stands in for it: the
  row has no reference and stays `open`, so the nightly retries it;
- `mfe` = max(High) / ref - 1 and `mae` = min(Low) / ref - 1 over the sessions seen so far (`sessions_seen`);
- `status` is `closed` once the row has a reference and the ticker has a bar on or after the window's last session,
  `no_bars` when a session has completed since `forecast_date` and the ticker has no bar in the window, else `open` (a
  ticker whose bars stop inside the window stays `open`);
- `status` is `split_check` instead when a bar's open (else its close, when the open is unusable) is 2.5 times (or
  1/2.5 of) the last usable close before it, from the forecast day's close to the window's end (`SPLIT_JUMP`): a split with no action in `price_actions`, so the bars mix
  two bases (a reverse split between the freeze and the next open showed 0.14 → 2.12 on the PC store). The row keeps its
  values but is not final; once D01 records the split, the nightly recomputes it on one basis and it closes;
- a `closed` row of the current `MODEL_VERSION` is final and the nightly run (`--nightly`) skips it, selecting the picks
  still to do in SQL rather than loading the whole ledger; `--full` recomputes every row (the first fill, or after a
  rule change bumps `MODEL_VERSION`); `--dry-run` writes nothing;
- with no daily bar stored at all (a missing or empty `prices.duckdb`) while a pick has a completed session, the run
  refuses before any write (exit 1), so final rows never turn into `no_bars`.

**The baselines beside it (#25 HR-1).** `core/baselines.py` writes `forecast_baselines` and nothing else, from the
store alone (no provider call, no ledger write). A hit rate on its own says nothing — 41% means a different thing in a
market where the average name rose 20% — so for every date the ledger froze a pick on it records what the same day's
universe did, over the same windows (`outcomes.outcome` is reused, so the rules cannot drift):
- `basis` `universe`: every ticker in that date's `equity_universe` snapshot, read at the end of the date from its
  change history (`rawstore.changes_as_of`). Never today's membership: a name that has since left still counts on the
  day it was in it. A date before the store recorded membership has no snapshot, and the rows say `n = 0`;
- `basis` `vol_decile_1` … `vol_decile_10`: the same names split into ten groups by trailing realised volatility (the
  standard deviation of the last 20 daily log returns of the **adjusted** closes up to and including the date,
  annualised; 1 the calmest). A name with fewer than 21 closes is in the universe baseline and in no decile. Fewer
  than ten members with a trailing vol have no deciles at all (ten groups cannot be made of fewer than ten names, and
  a "decile" of one name is not a peer group): the ten decile rows are still written, with `n = 0`, and those names are
  counted in the universe row alone. `vol_lo` / `vol_hi` are the group's boundaries, so a pick's own trailing vol maps
  to one basis without a second table;
- the **two bases are a stated choice**: the outcomes (MFE/MAE) are on the **nominal** basis — the prices the board
  froze on, and the basis `forecast_outcomes` measures the pick itself on — while the trailing volatility is on the
  **adjusted** one. Nominal already carries every split the store has recorded, so a split is the reason for neither;
  the dividend adjustment is, because an ex-dividend date is a one-day fall of the printed price that is not a move of
  the stock and one such day distorts a 20-session volatility, while a 30- or 90-day MFE/MAE is not distorted by it;
- `n` counts the members whose MFE is measurable; a `split_check` name is left out (a previous close → open ratio at or
  past `SPLIT_JUMP` on that one nominal series: a split the store has no action for, so its window is not one series —
  the same exclusion the hit-rate gates make on the picks), and a name with no reference open is out too.
  `mfe_mean` averages those members; `mae_mean` averages their MAE, which a bar can lack when its high is there and
  its low is not, so it can rest on fewer members than `n`;
- `ge20` / `ge10` / `ge5` are the cumulative counts a baseline hit rate is read off, `mfe_mean` / `mae_mean` the means
  ("up 5% on average" means something else with a −40% drawdown);
- all eleven bases are written for every date, so the table's key set never changes shape; `status` is `open` until
  the horizon's window has ended **and** the store holds a bar on or after that end (the rule the outcomes writer
  closes a pick on), the run past both moments writes `closed`, and the following nights recompute
  only the rows still open (a date whose 30-day window has ended keeps its 30-day rows and is recomputed for its
  90-day ones; `full` recomputes the date, after a bar backfill or a rule change). A night whose Daily Bars failed
  therefore leaves the rows open instead of freezing them on a short window.

The task RC-Forecast-Outcomes runs daily at 23:45, chained on RC-Daily-Bars (the bars it reads), after
RC-Screeners-Daily (weekdays from 23:00), the other `raw.duckdb` writer of the night. The task RC-Forecast-Baselines
runs daily at 23:50, chained on RC-Forecast-Outcomes (the same night's other `raw.duckdb` writer, so the two never
overlap) and writes `forecast_baselines` alone; its `--full` is the attended first fill. A dated target (an SPX pin)
gets one row with `horizon_days` = `target_date` - `forecast_date` from its own writer later; S0 writes picks only.

**Daily-bar cache and the freeze.** To let a post-close Fetch meet the rule above, `load_ohlc`
(`module_swing_potential/indicators.py`) treats a cached series as stale, besides its TTL, when its last bar is D or
older and it was written before D's close + 30 min (`ledger.needs_session_refetch`, comparing plain dates; a clock
past the holiday table leaves the TTL alone to decide). Two shapes: bars that end before D (a series written
23 h 57 min before a 17:00 ET run counted as fresh and ended on D-1) and a D bar that a mid-session Fetch cached
(a partial bar; **option A of #128, chosen by avo on 2026-10-03**). The refetch runs when the board loads its bars,
before it is scored, so the board the user sees and the picks the ledger freezes come from the same complete bars; it
touches only the tickers the Fetch loads whose bars are partial (Co-movement's mid-session bulk Fetch can leave
thousands of partial D bars, and only the ones a Swing or Fundamentals scan loads are refetched). A mid-session run
(D is yesterday) leaves today's partial bar alone. A name written after the close that still has no D bar (halted) is
not refetched, and a Refresh (`no_fetch=True`) never evaluates the rule or touches the network. If a refetch fails
(a rate limit), the stale cache is served (#205), the pick stays incomplete and the whole board is refused for that
day with the ticker named; a retry Fetch after the close refetches only the tickers still partial.

### Stage 4 — Rendering / outputs (regenerable)
Modules render HTML dashboards and JSON payloads. These are fully regenerable,
so they live in the clearable **`.cache/`** folder (`CACHE_ROOT` in
`core/config.py`):
- `.cache/{module}/{module}_latest.json` and `.cache/{module}_latest.html`
- Nested renders: `.cache/tw/tw_latest.html`, `.cache/sec_13f/sec_13f_latest.html`
- Timestamped archives (e.g. `.cache/earnings/revenue_chaser_{ts}.html`)

### Stage 5 — Serving
The Flask dashboard serves the HTML/JSON from `.cache/` and reads `raw.duckdb`
(plus `edgar.duckdb` for the EDGAR-backed boards) via short-lived per-request
read handles for API endpoints (rotation dates/latest, ticker store, watchlist
toggle, holdings bulk cache). Served pages never hold a DB handle across
requests.

**Refresh ignores the cache and recomputes from the store.** Full contract,
per-board table and the async job model: [`data_retrieval.md`](data_retrieval.md).
This is the general rule for every board backed by downloaded data (SEC 13F, Sector
Rotation, Swing Potential, Fundamentals, Asymmetric Growth, LEAPS, Financials,
Co-movement, Value Chain, Relatedness):
Refresh drops the board's `.cache/` html artifact and re-derives it from the
store with **zero network calls** (Value Chain / Relatedness read
`edgar.duckdb`). `Fetch`, by contrast, updates the raw data on the store over the
network and then recomputes + re-caches. Boards that genuinely need live data and
hold no downloaded cache (Earnings Radar) keep their "click Fetch to fetch" empty
state. Because the `.cache/` artifact is always regenerable this way, deleting any
of it costs only a re-run — the durable source stays `.store/` (all DuckDB files
+ the `edgar_docs/` tree).

## Concurrency model
DuckDB allows one *process* to hold a database file at a time (unlike SQLite in
WAL mode, a second process cannot even read it meanwhile — DuckDB's own `.wal`
file is a crash journal, not a concurrency mechanism), but any number of
connections **within** one process — as long as they all use the same
configuration. The scheduled tasks (the nightly backup, Relatedness off-peak and the Value Chain
filing sync, listed in `core/automations.py`; `register_tasks.ps1` installs only the backup) and
the dashboard both touch the store files, and inside the dashboard the `app/jobs.py`
worker thread writes while Flask request threads read. So:

- **Files by write-pattern, not one per domain.** (`prices.duckdb`, #156, is
  append-mostly third-party text with its own backup retention; it never joins
  raw tables. `prices.duckdb`, #156, is the append-only daily price history,
  large, with its own backup rule; its writer touches no other file.) `raw.duckdb` (structured
  data + the identity/GICS spine) and `edgar.duckdb` (EDGAR content) are split by
  *how they are written and backed up*, not per board: `edgar.duckdb` is
  write-once/read-many and large, so it is backed up less often (retain 2) while
  the hot structured store is backed up more (retain 5). We do **not** split
  further into per-domain files — a universe refresh, a LEAPS snapshot and a 13F
  ingest still share the one writer process, and same-transaction writes /
  cross-table joins stay within a single file. The only cross-file dependency
  (SIC → `equity_universe` sector backfill) is a Python-level two-step, not a SQL
  join. Raw HTML is the one file-based durable artifact (`edgar_docs/`).

- Every read/write is `connect → do work → close` per operation, **except inside
  `rawstore.session()`**, which shares one handle across a burst of operations
  on one thread. The dashboard opens a session per request (lazily — a request
  that never touches the store never locks it) and closes it when the request
  ends. No handle is held across a job or between requests. Opening the file
  costs ~100 ms on the live store; an operation on an open one < 1 ms, so a
  board render with ~30 reads drops from ~1 s to ~70 ms.
- **Peers wait, strangers fail fast.** Every RevenueChaser process that opens a
  store drops `.rc-proc-<pid>` beside it (removed at exit; a crashed process's
  file is ignored once its PID is gone). A lock held by one of those peers — the
  dashboard, a scheduled off-peak job, the nightly backup — is waited out with
  the full ~29 s budget, for reads *and* writes, because a peer only ever holds
  the file for one operation or chunk. A lock held by anything else (a VS Code
  DuckDB window, a notebook) fails fast (~0.35 s) with `StoreLockedError` naming
  the remedy, because that holder will not let go.
- **Every handle is opened read-write.** `rawstore.connect(read_only=True)`
  selects the longer retry budget only — it does *not* reach DuckDB. Mixing
  read-only and read-write handles in one process raises
  `ConnectionException: Can't open a connection to same database file with a
  different configuration`, which is how a scan running in the background used
  to 500 the dashboard.
- **Writes are atomic.** `write_rows`, `replace_all` and `transaction()` each
  run in one DuckDB transaction, so a refresh that fails partway can never
  leave a table half-emptied by the `DELETE` side of a replace. Use
  `replace_all` (not `clear()` + `write_rows`) for a full refresh, and
  `transaction()` when several tables must land together.
- **Every dashboard job leaves a `job_runs` row (#282).** `app/jobs.py` upserts one row per job id (`run_id` =
  `job_id` = the id `/api/jobs` shows) when the job starts running and when it ends as `done`, `error` or
  `cancelled` (`_record`, `_set`; a job cancelled while queued gets its row too). An end status is stored
  *before* `/api/job` reports it, so a reader in another process that sees `done` finds the row written and the
  file free. Times are epoch seconds, `params` is the job's parameters as a JSON object, `pid` the dashboard
  process. `app/jobs.py:run_id()` returns the running job's id (else a fresh one); it is per thread, so a writer
  captures it once on the runner thread and passes it down. `app/jobs.py` is the only writer of a dashboard job's
  row, and an upsert sets every column, so a value for `trigger`, `source`, `registry_name` or `counts`
  must travel through the job dict (#83), not be written to the table directly. The write waits at
  most ~0.35 s for a store another process holds (`rawstore.brief_wait`); a skipped or failed write is a
  `job_runs: could not record` warning in the Server log and never changes the job's outcome. The same
  skip can happen while the Settings board is collected: `inventory.collect()` holds one read-write handle per
  store file for about 2 s (`rawstore.session()`, so the ~150 queries open each file once), and in that window
  another process's `brief_wait` block gives up after ~0.35 s instead of waiting the full peer budget. Ordinary
  peer reads and writes are unaffected (they wait up to ~29 s), and so is a scheduled task's own `job_runs` row,
  which is written without the brief wait (`core.automations._record_run`). What can be lost is a peer's
  best-effort read of result history (`app/runners.py`, `core/reporting_common.py`, `module_leaps`,
  `module_sector_rotation`): each already falls back to a warning line, a missing history panel or the board file. So a row can
  stay `running` when the server stopped mid-job, when an end write was skipped, or when the job was evicted
  from the registry first. `app/jobs.reconcile_lost_runs` (#82) closes such a row as `lost`
  (no `finished_at`, outcome unknown) when its `pid` names no process, or when it is older than its bound
  (the module's `deadline_s`, else the scheduled task's time limit, else 6 h) and the pid is gone or reused;
  it runs at dashboard start and at the start of a scheduled writer. The reader is the **Runs** tab
  (`app/runview.py`, `/view/runs`, `GET /api/runs`, #26 PL5-b); it derives nothing from `pid`, and a row
  from before #83 shows no `trigger`.
  Every job now opens the store twice, including jobs that never used it (Money flow, queued after each
  Rotation Fetch).
- **A scheduled task that writes tables leaves a `job_runs` row too (#129 A1).** `core/automations.run_task` runs
  the task's command as a subprocess, so the dashboard never sees it; it upserts the run's own row (`run_id` =
  `job_id`, a fresh 12-hex id; `trigger` `scheduled`; `module`, `source` and `registry_name` the registry name;
  `params` the command's arguments; `pid` the task runner) when the command starts (`running`) and when it ends
  (`done` on exit 0, else `error` with `exit <code>`; a step its chain blocked ends `error` with
  `chain blocked: <why>` and no `started_at`). The command gets the id in `RC_RUN_ID` and the registry name in
  `RC_RUN_SOURCE` (`rawstore.task_run`), so `ledger.new_run_id`, `app/jobs.py:run_id()` and the store's change
  capture stamp that id, and the source, on everything the run writes. A task that writes no table (the backup)
  records no row and gets no id, so it leaves the store as it copied it. A row that cannot be written (the store
  is held past the normal lock wait) is a `job_runs: not recorded` line in the task's log, never a different exit
  code.
- **Cancel and deadline stop a job only at a checkpoint (#243).** Only a job registered `cancellable` (the universe
  run and the AG, Swing and Fundamentals Fetches) accepts a cancel while it runs; any other running job answers 409
  and runs to the end. Cancel of a running job sets its stop token
  (`core/job_control.py`); the runner ends `cancelled` at its next checkpoint: a board boundary in the universe
  run, or the next ticker in the AG and Swing/Fundamentals pools, which then cancel the tickers not yet started and
  return nothing to persist or freeze. A job's deadline (`jobs.register(deadline_s=...)`, overridable per `submit`;
  none by default, cancellable jobs only) ends it `error` at the next checkpoint once passed. Nothing is interrupted mid-write, so every
  `write_rows` batch stays whole.
- **One universe run at a time (#244).** `run_universe_refresh` holds `universe_write_lock()` for the whole run, on
  the dashboard job and on the CLI (`python -m RevenueChaser.app.universe_cli`, exit 3 when busy). A second run
  ends "another universe run is in progress, pid N" (or "the universe data is being written by another job, pid N"
  while Phase B holds the writer lock) before any call or write. The single-board Fetches (AG, Swing,
  Fundamentals) are refused while a universe run holds it (the `universe_run` marker), not while Phase B does; `/run/universe` answers 409 with the queued or running job's id.
  The reverse holds too (#356): a board Fetch holds a `board_fetch_<board>` marker while it runs, and a universe run
  that starts meanwhile ends "a Swing Fetch is in progress, pid N; this universe run did not start". Each side takes
  its own lock and then checks the other's, so two that start together never both go ahead. A Refresh takes no marker.
- **A reused PID does not keep a job lock (#355).** A `*.lock` file holds `<pid> <token> <start>`, `<start>` being
  the holder's process start time (`core/procs.process_start_time`: the creation time on Windows, `/proc` on Linux).
  A lock is live only while its PID runs and that process started at `<start>`; a lock with the current process's
  PID only while its token is one this process holds. So a crash or a reboot followed by PID reuse, the restarted
  dashboard included, no longer leaves `universe.lock` or `universe_run.lock` held. When a start time is unknown (a
  lock written before #355, access denied) the PID alone decides, as before.
- **An empty `replace_all` is refused (#284).** `replace_all(table, [])` would
  delete every row and write none, which is what a degraded run (no peers, no
  edges, a fetch that returned nothing) produces by accident. It raises
  `RawStoreError` unless the caller passes `allow_empty=True` (keyword only):
  `save_watchlist` does, because removing the last ticker is the user's
  choice, and so does the legacy migration. A writer whose run produced nothing
  keeps the stored rows and logs why: Relatedness `save_scores` and Value Chain
  `build_edges` do (the latter returns what the store holds).
- **History tables refuse rewrites (#286).** A MANIFEST entry with
  `"history": true` holds the past (the forecast ledger, session snapshots,
  change history). For such a table `replace_all`, `update_columns`, `delete`
  and `clear` raise; raw SQL that rewrites it raises too (`execute`,
  `transaction().execute`, `query`). DuckDB itself reads the SQL (#308): its
  parser types each statement (`extract_statements`) and its binder names the
  operator and the table it writes (`json_serialize_plan`, on the connection
  that runs the text), so no spelling of the name and no comment, literal or
  character can hide a statement from the check. Refused: whatever DuckDB plans
  as `DELETE` (and `TRUNCATE`), `UPDATE`, `DROP`, `CREATE OR REPLACE TABLE`, or a
  merge with an UPDATE or DELETE branch (`MERGE INTO ... THEN UPDATE/DELETE`,
  `INSERT ... ON CONFLICT ... DO UPDATE` and `INSERT OR REPLACE`), and any text
  that names a flagged table but that DuckDB cannot parse or plan (fail
  closed; DuckDB plans every statement of one call before any runs, so a
  statement that needs a table created earlier in the same text is sent as its
  own call). A parameter in a select list must be cast (`?::VARCHAR`): an
  untyped `?` there makes the planner fail, so the text is refused unreadable.
  Measured on DuckDB 1.5.6
  (`.hermes/plans_claude/arch/2026-10-04_308_sql_guard_parser.md`). `write_rows`
  keeps the first row written for a key, whatever the table's `write` mode. The
  flag needs a `key` that is the table's `PRIMARY KEY`. Outside the guard, on
  purpose and listed here: `ALTER`, and a caller that opens its own
  `rawstore.connect()` handle. `forecast_ledger` carries the flag (#286), as do
  the schema-11 change and history tables; its writer (`core/ledger.py`) only
  appends, so the flag changes no write.
- **Every table declares a history mode (#281).** Each MANIFEST entry carries
  `"history_mode"`, one of `event` (dated facts, kept), `session_snapshot` (one
  row per key per NY session), `change_history` (every change of a slow value,
  with `valid_from`), `latest_cache` (latest value only, for data that can be
  fetched or rebuilt again) or `operational` (run stamps and pointers of the
  store itself). `rawstore.HISTORY_MODES` lists the five. It is a label only:
  rawstore does not read it, so it changes no write; the `"history": true` flag
  above is what enforces. Of the 69 tables from before G1: 26 event, 7
  session_snapshot, 15 change_history, 21 latest_cache (the proposal of
  `w4_manifest_results.md` Part A, with `catalyst_events` and `orphan_designations` moved to change_history and
  `orphan_lookups` to event by 06 §4 in schema 11); the 22 G1 tables carry theirs from #292, and the 25 schema-11
  tables plus `forecast_ledger` (#286) are the ones flagged `"history": true`.
  `core/tests/test_manifest_history.py` fails on an entry with no mode, pins
  each pre-G1 table to its agreed mode, and pins each of the 22 G1 entries,
  whole, to `core/tests/ddl292_frozen.py` (a frozen copy of the reviewed
  `ddl_292.md` entries). Changing a G1 table's DDL means changing that copy in
  the same commit, on purpose.
- **A table with no writer is marked planned or retired (#289).** The first
  store call creates it like any other table, so without a flag an empty one
  reads as "0 rows" — the same as a table whose writer broke. `"planned": true`
  means a writer is still to come. `"retired": true` means the only writer is
  in the past. Both mean an empty table is expected; they differ in the word a
  future listing prints. A label, as `history_mode` is: rawstore does not read
  either, so neither changes DDL or a write and neither needs a
  `_SCHEMA_VERSION` bump. Planned today: `catalyst_events` (the fetchers of
  #186-#192), `orphan_designations` and `orphan_lookups` (#190), `sec13f_filers`
  and `edgar_companyfacts` (G1 #292, manifest only). Retired today:
  `leaps_daily_scores` and `sec13f_ticker_names`, which only the legacy import
  `scripts/migrate_rawdata_to_duckdb.py` fills, and `sec13f_edgar_backfill`,
  which that same import fills and which is scheduled to leave the manifest at
  the next bump. The review that raised this also named `price_bars_daily`,
  `forecast_ledger` and `job_runs`; each has a writer now (`core/prices.py`,
  `core/ledger.py`, `app/jobs.py` and `core/automations.py`), so none of the
  three is marked. Nothing renders either flag yet: the Settings inventory
  lists its own dataset rows and none of these tables is among them, so showing
  "planned" or "retired" there instead of a row count is a template change for
  when such a listing arrives. `core/tests/test_manifest_planned_289.py` pins
  the flagged set — the thing to edit, together with the entry, when a writer
  lands — and pins the DDL a fresh store creates for those tables, so neither
  flag can ever reach the create path.
- **Contention is retried, at two levels.** DuckDB signals it four different
  ways and `core/rawstore.py` handles each:
  | Where | What DuckDB raises | Retried by |
  |---|---|---|
  | another process holds the file | `IOException: could not set lock on file` | `_connect_with_retry` |
  | same-process config clash | `ConnectionException: ... different configuration` | `_connect_with_retry` |
  | two threads attaching at once | `BinderException: unique file handle conflict` | `_connect_with_retry` |
  | two writers on the SAME row | `TransactionException: failed to commit` / `ConstraintException: duplicate key ... violates primary key` | `_retry_write` (operation-level — the loss happens after connect) |

  Anything else fails fast. `transaction()` cannot retry for you: the caller
  owns the block. Use `rawstore.atomic(fn)` instead — it runs `fn(tx)` in one
  transaction and re-runs it on a lost commit race (the Co-movement run writer
  and the Relatedness profile writer use it). Never split one logical write
  into several `write_rows`/`execute` calls: each is its own transaction.
- Long ingests stay chunked so the writer lock is never held for minutes;
  `ingest_zips` clears a zip's own accessions before re-inserting them, so an
  interrupted run resumes without duplicating holdings.
- The registered scheduled tasks run at 04:00 (backup), 12:30 (Relatedness off-peak), 13:00
  (Value Chain filing sync) and 22:35 (daily price bars, #156; first the SEC exchange and fund files, #301; last the
  `^GSPC` daily levels, #293), plus Saturday 10:00 weekly (index lists,
  RC-Index-Lists, #302), weekdays not before 23:00 (the screeners Fetch RC-Screeners-Daily, chained on the
  daily price bars, #129), daily not before 23:45 (the forecast outcomes RC-Forecast-Outcomes, chained on the daily
  price bars, #380 S0), daily 23:50 (the forecast baselines RC-Forecast-Baselines, chained on the outcomes run, #25
  HR-1) and Tuesday to Saturday not before 01:30 (the Earnings inputs RC-Earnings-Inputs, chained on the
  daily price bars, E0 #372), plus daily 09:30 (the open-issues registry check RC-Open-Issues-Check, #74:
  `scripts/open_issues_sync.py --check`, which reads the GitHub API, writes nothing and is deliberately not
  chained on a store writer) and daily 09:15 (the IBKR Gateway login check RC-IBKR-Login-Check, #162:
  `scripts/ibkr_login_check.py`, one TCP connection and the TWS API version handshake, which writes no table and
  no store file, only its own record under `.cache/ibkr_login/`, and exits 0 whatever it finds — a session that
  expired on its weekly cycle is not a failed job), `core/automations.py`: the Relatedness and Value Chain rows
  are 30 min apart.

### Full-refresh vs. incremental tables
The JSON files these tables replaced were rewritten wholesale, so the tables
that mirror a complete snapshot use `replace_all` (an empty batch needs
`allow_empty=True`, #284) — a key absent from the new
payload is **removed**, not left behind: `sec13f_totals`, `ticker_scores`,
`fundamentals_scores`/`_gates`,
`swing_scores`/`_gates`. `watchlist` is no longer one of them: the dashboard
adds or removes one ticker per click (`rawstore.toggle_watchlist`, #306). Per-ticker caches (`ticker_payload_cache`,
`edgar_financials`, `swing_info`, `leaps_cache`) stay incremental upserts, and
`index_constituents` / `swing_ohlc` / `rotation_snapshots` / `oipd_chain` /
`relatedness_theme_etfs` / `comov_nodes` / `comov_members` (per `as_of` run) /
`relatedness_attributes` (per ticker) replace one group at a time (`group_by` in
the manifest). A `"replace"` table **without** `group_by` only clears the exact
keys present in the new batch — rows that vanished from the batch survive — so
any table written one whole run/entity at a time must name that grain in
`group_by`. (`ticker_registry` is now the `index_tickers` VIEW, so it is no
longer a replace-all table.)

### Schema on first use
`ensure_all_tables()` (renames → tables/columns → merges → views → EDGAR-table
move → price-table move to `prices.duckdb`) runs automatically on the first store call of every process — dashboard,
CLI, scheduled job — once per store path. Before this, nothing outside tests
called it, so a fresh or restored store never got its `company_profile` /
`index_tickers` views and those readers silently returned `[]`. A failure (the
store is locked) is logged and retried at most every 30 s, never raised into a
read. The unit suite turns it off (`RC_STORE_AUTO_ENSURE=0`, conftest) because
every test gets a fresh DB; the e2e dashboard turns it back on. A rename whose
new name already exists but is **empty** (a write self-healed it first) now drops
the empty shell and renames; if both hold rows it logs and leaves both.

### Write performance
- Batches of ≥ 64 rows are inserted through a registered DataFrame and one
  `INSERT … SELECT` (with `CAST` to each column's type) instead of
  `executemany`: 50k rows go from ~15 s to ~0.2 s. Values are passed as text and
  cast in SQL, which reproduces exactly what binding the Python value did
  (tested for parity in `core/tests/test_rawstore_storage.py`). Duplicate keys in
  one batch are collapsed the way row-by-row execution resolved them (upsert:
  last wins; append: first wins). Large `update_columns` batches use one
  `UPDATE … FROM`.
- `_ensure_table` is memoised per (file, table) for the life of the process
  (it cost ~40 ms per write).

## `.gitignore`
`.store/`, `.cache/`, `output/`, and `.venv/` remain ignored (code-only repo).

## Storage format inside the store (schema evolution)

The JSON-file legacy data is **not** stored as opaque TEXT blobs. Each raw table
declares real DuckDB column types in `core/rawstore.py`'s MANIFEST
(`docs/plan_store_schema_evolution.md`):

- **Selectable scalar columns** — key/timestamp fields and any field a query
  actually filters on (e.g. `equity_universe.market_cap DOUBLE`,
  `sector TEXT`, `price DOUBLE`, `name TEXT`). These filter in ~ms without
  materialising row payloads.
- **Native `JSON` columns** — the nested `quote` struct and the per-record
  `payload`/`entry` columns of the cache/map tables. Serialized once as JSON on
  write (DuckDB's own Python converter mangles floats inside mixed-type arrays),
  returned by `get`/`get_all` as Python dicts/lists/scalars, and reachable from
  SQL with path expressions (`payload.market_cap::DOUBLE`).

Why not fully typed STRUCT columns everywhere? The per-ticker caches are
genuinely heterogeneous — `ticker_payload_cache`'s `payload` has a different
field set per ticker, and only ~8% of ticker files even carry `market_cap`.
Forcing those through `read_json_auto` yields a `MAP`/`JSON` column anyway, so
they stay one JSON value column per row. Only row-uniform sources become typed
columns; the runtime-written ones keep a JSON column so no provider field is ever
silently dropped. The universe provenance that used to be a synthetic `__meta__`
payload row now lives in `equity_universe_provenance` (one keyed row) so the
universe table itself can be typed.

### The hot-path rule (governing principle)

A field that is **filtered, scanned, or joined across records is a column, never
a JSON blob**:

- **A hot path must never live in JSON.** A query that touches every row (a
  universe scan, a CUSIP→ticker join feeding an aggregation, a
  `max(fetched_at)`) must run on typed scalar columns. If a cold entry *becomes*
  hot (a new board filters on it, a join needs it, a table crosses into "scanned
  often"), promote it to a column — that is an expected, normal migration, not a
  redesign (`_ensure_table` self-heals the column by emitting
  `ALTER TABLE … ADD COLUMN`).
- **Reading a whole JSON table into memory is fine when the table is small**
  (per-record caches, the 13F map files — a few hundred to a few thousand rows,
  built once and keyed-looked-up afterwards). It is **not** fine for big tables
  that are scanned across records; those must be flattened so the scan/join runs
  in DuckDB. Rough rule: if a table is `get_all()`-ed and then filtered/joined
  in Python *and* it is large or frequently read, it should be typed columns.
- **Verifying hot paths is the AI's job at implementation time.** When adding a
  feature that reads the store, check the access pattern: if the new reader
  scans/filters/joins on a field that currently sits in a JSON blob, raise it to
  a column in the same change — do not ship a reader that does the scan in
  Python over JSON. (This document's per-table table is the checklist.)
- **Schema versions ship a downloadable checkpoint.** When `_SCHEMA_VERSION`
  is bumped (a store-format change), the new store must be produced from the
  source tree and published as a downloadable checkpoint (google drive). There is currently no automation for this part, the AI must warn to do this manually. 

### Per-table column format (typed vs JSON)

The source of truth is `core/rawstore.py`'s MANIFEST. **Typed** = real scalar
DuckDB columns (`TEXT` / `DOUBLE` / `INTEGER`), filterable in SQL without
parsing the row. **JSON** = one native `JSON` value column per row (opaque to
schema, reachable with path expressions like `payload.market_cap::DOUBLE`).

| Table | Typed (selectable) columns | JSON column(s) |
|---|---|---|
| `equity_universe` | `ticker`, `market_cap DOUBLE`, `sector`, `industry`, `industry_group`, `sub_industry`, `price DOUBLE`, `name`, `next_earnings_date` (UW's date text; `industry_group` / `sub_industry` are NULL since #265) | `quote` (Yahoo screener quote; NULL for a listed name the screener missed) |
| `equity_universe_provenance` | `key`, `generated_at`, `refreshed_by` | — |
| `ticker_payload_cache` | `ticker`, `fetched_at DOUBLE`, `fetched_at_iso` (hoisted out of the payload) | `payload` (rest of the yfinance dict) |
| `edgar_financials` (edgar DB) | `ticker`, `fetched_at DOUBLE` | `meta`; `annual`, `quarterly` are **zlib-compressed JSON** (`BLOB`, see `compress`) |
| `edgar_documents` (edgar DB) | `accession`, `cik`, `tickers`, `form`, `filed`, `fiscal_year`, `doc_url`, `path`, `sha256`, `bytes_raw`, `bytes_gzip`, `fetched_at`, `source`, `run_at` (#299; added to an existing store by `ALTER TABLE ADD COLUMN`, no bump) | — |
| `chain_filings` (edgar DB) | `ticker`, `cik`, `form`, `filed`, `accession`, `primary_doc`, `quality`, `fetched_at` | `item_text` is **zlib-compressed text** (`BLOB`) |
| `chain_mentions` / `chain_meta` (edgar DB) | `src`, `dst_name`, `dst_ticker`, `relation`, `snippet`, `filed`, `share_pct DOUBLE` (the share of the filer's sales or purchases a concentration sentence states; NULL on a keyword edge; #387 D7, added by ALTER at the first call) / `key`, `value`, `fetched_at DOUBLE` (#127; NULL on older rows is unknown, never 0; added by ALTER, no bump) | — |
| `comov_runs` / `comov_nodes` / `comov_members` / `comov_lag` | `as_of` + run config (`n_factors`, `null_json`, `oos_json`) + the Fetch's `run_id`, `session_date DATE` (rule B on `as_of`) and `dropped_json` (the run's drop lists; #379 D2, additive, older runs read NULL; each Fetch also writes one header-only `result_runs` row naming every run it stored or served) / `(as_of, cut, cluster_id)` metrics incl. `baseline_r`, `persist_*`, `oos_*` / `(as_of, cut, ticker)`, `core` (was in the matching group a quarter ago; `stability` legacy, no longer written) / `(as_of, cut, ticker)` lagged group + `oos_r`, `oos_base`, `held` | — |
| `sec_company_sic` | `ticker`, `cik`, `sic`, `sic_description`, `fetched_at` | — |
| `edgar_facts` (edgar DB) | `cik`, `concept`, `period`, `form`, `value`, `unit` | — |
| `swing_info` | `ticker`, `cached_at DOUBLE` (hoisted out of the payload), `fetched_at DOUBLE` (#127; NULL on older rows is unknown, never 0) | `payload` (`.info` dict) |
| `leaps_cache` | `ticker`, `fetched_at DOUBLE` (#127; NULL on older rows is unknown, never 0) | `payload` (`{latest, history[]}`; `history` is frozen since #377 L3: carried unchanged, never appended, and the classifier reads its baselines from `leaps_footprint_history`) |
| `rotation_holdings` | `ticker`, `date` (hoisted out of the payload), `fetched_at DOUBLE` (#127; NULL on older rows is unknown, never 0) | `rows` (holdings + RS list) |
| `index_constituents` | `slug`, `ticker` | — |
| `index_tickers` (VIEW) | `slug`, `ticker`, `name`, `sector` | — |
| `ticker_themes` | `ticker`, `theme_id`, `theme`, `level`, `theme_run_id`, `fetched_at DOUBLE` (#127; NULL on older rows is unknown, never 0) | — |
| `fundamentals_scores` / `swing_scores` | per-ticker metric columns (see MANIFEST) | — |
| `fundamentals_gates` / `swing_gates` | `ticker`, `gate`, `rule_id`, `passed`, `actual`, `unit` | — |
| `relatedness_profiles` | `ticker`, `cik`, `model`, `prompt_version`, `fetched_at`, `cache_key`, `status` | — |
| `relatedness_attributes` | `ticker`, `field`, `value` (TEXT holding a JSON list), `value_num` | — |
| `relatedness_scores` | `ticker`, `peer`, `score` | — |
| `relatedness_themes` / `relatedness_theme_tags` / `relatedness_theme_members` / `relatedness_theme_etfs` | `theme_id` (run id is its prefix), `level`, `parent_id`, `label`, `size` / `tag`, `rank` / `ticker`, `weight` / `etf`, `overlap`, `rank` | — |
| `theme_runs` | `id`, `created_at`, `k_fine`, `k_medium`, `k_coarse`, `jaccard`, `is_current`, `session_date DATE`, `run_at DOUBLE` (schema 11) | — |
| `llm_cache` | `cache_key`, `ticker`, `cik`, `model`, `thinking`, `effort`, `prompt_version`, `input_hash`, `fetched_at`, `status` | `response` |
| `job_runs` | `run_id`, `job_id`, `module`, `lane`, `trigger`, `source`, `pid`, `registry_name`, `queued_at DOUBLE`, `started_at DOUBLE`, `finished_at DOUBLE`, `status` (`queued`/`running`/`done`/`error`/`cancelled`/`lost`, the `app/jobs.py` vocabulary), `error`, `message` | `params`, `counts` |
| `run_registry` | `key`, `value` | — |
| `sec13f_security_meta` | `ticker`, `fetched_at DOUBLE` (#127; NULL on older rows is unknown, never 0) | `entry` (`{name, sector, type}`) |
| `sec13f_enrichment` | `ticker` | `entry` (`{price{period: close}, …}`) |
| `sec13f_totals` | `ticker` | `entry` (`{period: metrics}`) |
| `sec13f_edgar_backfill` | `cik`, `period`, `accession`, `fetched_at` | — |
| `sec13f_ticker_names` | `ticker` (rows as migrated; no writer or reader since #207 retired the name match) | `entry` (`{name, ticker, figi}`) |
| `cusip_ticker` | `cusip` | `entry` (string `"RTX"` or `{ticker, …}` for rows written before #207; `{ticker, figi, share_class_figi, source, run_at}` from `identity_cusip`; `{ticker, source: "legacy"}` for a legacy entry the prefix fallback kept) |
| `etf_holdings` | `ticker`, `provider` (`uw`/`yfinance`), `fetched_at DOUBLE` (#303) | `entry` (holdings list) |
| `swing_ohlc` | `ticker`, `ts`, `open`/`high`/`low`/`close`/`volume DOUBLE`, `cached_at` | — |
| `price_bars_daily` | `ticker`, `session_date DATE` (NY session date), `open`/`high`/`low`/`close`/`adj_close`/`volume DOUBLE`, `split_ratio DOUBLE` (always NULL from #156 on; splits are rows of `price_actions`), `adj_source`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, session_date)`; append (first write of a key wins; v10 key was `(ticker, date)`, rebuilt by the first v11 call with the rows kept) | — |
| `catalyst_events` | `ticker`, `event_date DATE`, `date_precision`, `kind`, `title`, `source`, `source_event_id`, `source_url`, `confidence`, `nct_id`, `fetched_at DOUBLE`. Key `(source, source_event_id)` | — |
| `orphan_designations` | `ticker`, `generic_name`, `designation`, `date_designated DATE`, `designation_status`, `approval_status`, `marketing_approval_date DATE` / `trade_name` (the earliest approval), `sponsor`, `fetched_at DOUBLE`. Key `(ticker, generic_name, designation, date_designated)` | `approvals` (every `{marketing_approval_date, trade_name}`) |
| `orphan_lookups` | `ticker`, `session_date DATE`, `sec_name`, `query`, `match` (`matched`/`unmatched`/`no_hits`/`failed`), `provider_status`, `n_rows INTEGER`, `error`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, session_date)` (schema 11; was `(ticker)`) | `candidates` |
| `equity_universe_changes` | Change history of `equity_universe` (schema 11, raw.duckdb; filled by the store in the write's transaction): `ticker`, `name`, `sector`, `industry`, `industry_group`, `sub_industry`, `next_earnings_date`, `valid_from DATE`, `status`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, run_at)`; append-only | — |
| `security_master_changes` | Change history of `security_master` (schema 11, raw.duckdb; filled by the store in the write's transaction): `ticker`, `cik`, `cik_source`, `exchange`, `name`, `name_source`, `security_type`, `type_source`, `sector`, `sector_source`, `industry_group`, `group_source`, `industry`, `industry_source`, `sub_industry`, `sub_source`, `figi`, `composite_figi`, `share_class_figi`, `exch_code`, `security_description`, `figi_source`, `sic`, `sic_description`, `sic_group2`, `sic_source`, `cusip`, `cusip_source`, `isin`, `isin_source`, `type_asof`, `gics_asof`, `valid_from DATE`, `status`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, run_at)`; append-only. The four identifier columns are tracked since #127 (a changed CUSIP or ISIN appends a row; added by ALTER, no bump) | — The first identity apply after #127 (23 Oct 2026) appended one change row per spine ticker (about 7,000) for the new `cusip`/`isin` columns: readers of this history exclude that run (its `run_id` is the identity run that first filled the columns). |
| `identity_source_cache_changes` | Change history of `identity_source_cache` (schema 11, raw.duckdb; filled by the store in the write's transaction): `source`, `ticker`, `row_status`, `security_type`, `security_type2`, `sector`, `industry`, `sub_industry`, `cik`, `sic`, `is_etf BOOLEAN`, `forms`, `shell_closed BOOLEAN`, `valid_from DATE`, `status`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(source, ticker, run_at)`; append-only | ``payload`` |
| `index_constituents_changes` | Change history of `index_constituents` (schema 11, raw.duckdb; filled by the store in the write's transaction): `slug`, `ticker`, `valid_from DATE`, `status`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(slug, ticker, run_at)`; append-only | — |
| `watchlist_changes` | Change history of `watchlist` (schema 11, raw.duckdb; filled by the store in the write's transaction): `ticker`, `valid_from DATE`, `status`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, run_at)`; append-only | — |
| `board_settings_changes` | Change history of `board_settings` (schema 11, raw.duckdb; filled by the store in the write's transaction): `board`, `key`, `value DOUBLE`, `valid_from DATE`, `status`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(board, key, run_at)`; append-only | — |
| `catalyst_events_changes` | Change history of `catalyst_events` (schema 11, raw.duckdb; filled by the store in the write's transaction): `source`, `source_event_id`, `ticker`, `event_date DATE`, `date_precision`, `kind`, `title`, `source_url`, `confidence`, `nct_id`, `valid_from DATE`, `status`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(source, source_event_id, run_at)`; append-only | — |
| `orphan_designations_changes` | Change history of `orphan_designations` (schema 11, raw.duckdb; filled by the store in the write's transaction): `ticker`, `generic_name`, `designation`, `date_designated DATE`, `designation_status`, `approval_status`, `marketing_approval_date DATE`, `trade_name`, `sponsor`, `valid_from DATE`, `status`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, generic_name, designation, date_designated, run_at)`; append-only | ``approvals`` |
| `etf_holdings_changes` | Change history of `etf_holdings` (#303, D25; raw.duckdb; filled by the store in the write's transaction, no schema bump): `ticker`, `entry JSON`, `provider`, `valid_from DATE`, `status`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. A row when an ETF's list (tickers, names or weights) or its provider changes; a rerun with the same answer adds none. Key `(ticker, run_at)`; append-only | — |
| `result_runs` | Result history (run header; schema 11, history.duckdb): `board`, `run_id`, `session_date DATE`, `run_at DOUBLE`, `quality`, `quality_note`, `session_snapshot BOOLEAN`, `model_version`, `error`, `payload BLOB`. Key `(board, run_id)`; append-only | ``params``, ``context``, ``funnel``, ``row_counts``, ``warnings``, ``provider_health`` (the run's `ProviderHealth.health()` dict, or one per provider; PH-1, #23) |
| `ag_scores_history` | Result history (mirror of `ticker_scores`; schema 11, history.duckdb): `run_id`, `ticker`, `name`, `sector`, `industry`, `industry_group`, `sub_industry`, `price DOUBLE`, `market_cap DOUBLE`, `ps_ratio DOUBLE`, `rev_growth DOUBLE`, `gross_margin DOUBLE`, `fcf DOUBLE`, `runway_years DOUBLE`, `rd_intensity DOUBLE`, `dilution_yoy DOUBLE`, `earnings_date DOUBLE`, `total_points INTEGER`, `tech_breakthrough INTEGER`, `dilution_warning INTEGER`, `us_listed INTEGER`, `source`, `inst_holders INTEGER`, `inst_new INTEGER`, `inst_exited INTEGER`, `inst_buy_flow DOUBLE`, `inst_sell_flow DOUBLE`, `inst_shares DOUBLE`, `inst_shares_delta_pct DOUBLE`, `inst_ownership_pct DOUBLE`, `themes`, `signals`, `tier INTEGER`, `flags`, `model_version`, `anchor_fy INTEGER`, `prior_fy INTEGER`, `period_ends` (the last six: #380 AG W1, followed from `ticker_scores` with no schema bump; older rows NULL). Key `(run_id, ticker)`; append-only | — |
| `swing_scores_history` | Result history (mirror of `swing_scores`; schema 11, history.duckdb): `run_id`, `ticker`, `multiplier INTEGER`, `quality DOUBLE`, `rs_rating DOUBLE`, `price DOUBLE`, `change_pct DOUBLE`, `change_1w DOUBLE`, `change_1m DOUBLE`, `entry DOUBLE`, `target DOUBLE`, `stop DOUBLE`, `rr DOUBLE`, `atr DOUBLE`, `ma_alignment`, `direction_ok INTEGER`, `m_pass INTEGER`, `w_pass INTEGER`, `q_pass INTEGER` (the last four: #384 SW-S1, followed from `swing_scores` with no schema bump). Key `(run_id, ticker)`; append-only | — |
| `swing_gates_history` | Result history (mirror of `swing_gates`; schema 11, history.duckdb): `run_id`, `ticker`, `gate`, `rule_id`, `passed INTEGER`, `actual DOUBLE`, `unit`. Key `(run_id, ticker, gate, rule_id)`; append-only | — |
| `fundamentals_scores_history` | Result history (mirror of `fundamentals_scores`; schema 11, history.duckdb): `run_id`, `ticker`, `period`, `multiplier INTEGER`, `distorted INTEGER`, `quality_pass INTEGER`, `momentum_pass INTEGER`, `ro40 DOUBLE`, `rev_growth_ttm DOUBLE`, `fcf_margin DOUBLE`, `ttm_revenue DOUBLE`, `gross_margin DOUBLE`, `roic DOUBLE`, `net_debt_ebitda DOUBLE`, `pe DOUBLE`, `pb DOUBLE`, `dist_52w_pct DOUBLE`, `rs_6m DOUBLE`, `above_200d INTEGER`, `ro40_basis TEXT`, `anchor_period_end TEXT`, `price DOUBLE`, `market_cap DOUBLE`. Key `(run_id, ticker)`; append-only | — |
| `fundamentals_gates_history` | Result history (mirror of `fundamentals_gates`; schema 11, history.duckdb): `run_id`, `ticker`, `gate`, `rule_id`, `passed INTEGER`, `actual DOUBLE`, `unit`. Key `(run_id, ticker, gate, rule_id)`; append-only | — |
| `relatedness_scores_history` | Result history (mirror of `relatedness_scores`; schema 11, history.duckdb): `run_id`, `ticker`, `peer`, `score DOUBLE`. The peer scores of each Relatedness Fetch, the same rows `relatedness_scores` gets (`store.score_rows`); the board payload rides on `result_runs.payload`. Written by every Fetch, dashboard or the off-peak CLI (#388, `module_relatedness/history.py`, from `engine._run`); a peak-boundary stop, an auth or transient stop, or a run that scored no pair makes it `partial`. Key `(run_id, ticker, peer)`; append-only | — |
| `value_chain_edges_history` | Result history (mirror of `chain_mentions`; schema 11, history.duckdb): `run_id`, `row_no INTEGER`, `src`, `dst_name`, `dst_ticker`, `relation`, `snippet`, `filed`, `share_pct DOUBLE` (#387 D7). The edges `chain_mentions` holds after each write: a Value Chain Fetch (the board payload rides on `result_runs.payload`) and an EDGAR sync (the dashboard job and `RC-ValueChain-Sync`, no payload). Written through `engine.record_history` (#387, `module_value_chain/history.py`); kept edges (none came out of the corpus), failed tickers or an SEC stop make the run `partial`. Key `(run_id, row_no)`; append-only | — |
| `oipd_chain_history` | Result history (mirror of `oipd_chain`; schema 11, history.duckdb): `run_id`, `row_no INTEGER`, `ticker`, `option_symbol`, `expiry`, `option_type`, `strike DOUBLE`, `bid DOUBLE`, `ask DOUBLE`, `last_price DOUBLE`, `volume DOUBLE`, `open_interest DOUBLE`, `iv DOUBLE`, `delta DOUBLE`, `tape_time`, `fetched_at`. The chain each OIPD Fetch fitted, the same rows `oipd_chain` gets (`engine.chain_rows`); a partial chain, which `oipd_chain` does not save, is kept here and its run is `partial`. Written by an OIPD Fetch (#382, `module_oipd/history.py`). Key `(run_id, row_no)`; append-only | — |
| `earnings_cards_history` | Result history (row table; schema 11, history.duckdb): `run_id`, `ticker`, `earnings_date DATE`, `earnings_time`, `fiscal_quarter`, `final_action`, `direction_score DOUBLE`, `volatility_edge_score DOUBLE`, `confidence_score DOUBLE`, `r2_score DOUBLE`, `r3_score DOUBLE`, `r4_score DOUBLE`, `r5_score DOUBLE`, `r7_score DOUBLE`, `r8_score DOUBLE`, `bias_label`, `conviction`, `macro_veto BOOLEAN`, `expected_move_pct DOUBLE`, `data_completeness DOUBLE`, `pre_score DOUBLE`, `filter_status`, `filter_reasons`, `trade_read`, `attention_score DOUBLE`, `edge_ratio DOUBLE`, `nrc DOUBLE`, `nrc_tier`, `nrc_growth DOUBLE`, `nrc_acceleration DOUBLE`, `nrc_quality DOUBLE`, `nrc_beat DOUBLE`, `watchlist BOOLEAN`, `card BLOB`, `slots JSON`, `inputs BLOB`. `card` is the whole card (JSON, compressed), `inputs` the UW answers it was scored on, `slots` the live rule and NRC slots. Written by an Earnings Fetch (#278, `module_earnings_radar/history.py`); a run with a degraded UW input is stored `partial` with its rows and the reason in `result_runs.quality_note`; `inputs` holds the UW answers incl. term structure, spot exposures, EPS series, short row and a year of bars (since E4 the 60-bar reader slice and a copy of the print series are not stored twice; `history.packet_ohlc` and `packet_r2_series` rebuild them). Key `(run_id, ticker)`; append-only | — |
| `leaps_footprint_history` | Result history (row table; schema 11, history.duckdb): `run_id`, `ticker`, `name`, `label`, `score INTEGER`, `flow_score INTEGER`, `far_call_oi DOUBLE`, `far_put_oi DOUBLE`, `far_call_share DOUBLE`, `far_oi_growth_1d_pct DOUBLE`, `far_oi_growth_q_pct DOUBLE`, `far_call_sweep_pct DOUBLE`, `iv_rank DOUBLE`, `iv_rank_slope DOUBLE`, `near_term_event BOOLEAN`, `as_of DATE`, `frequency INTEGER`, `notional DOUBLE`, `net_notional DOUBLE`, `concentration DOUBLE`, `concentration_mult DOUBLE`, `ask_share DOUBLE`, `itm_conc DOUBLE`, `otm_conc DOUBLE`, `fetch_status`, `inputs BLOB`. One row per watchlist ticker per LEAPS Fetch, whatever its label: the footprint, the flow-print metrics merged into it, `fetch_status` (`ok`, `provider_failed` when one of its UW calls failed, `error: <exception class>`, never its text, #107) and `inputs` (the three UW answers, the two baselines and the flow candidate, JSON, compressed). Written by a LEAPS Fetch (#377, `module_leaps/history.py`); one run per session; an empty watchlist, a footprint not fetched whole or failed UW calls make the run `partial` (header only). Since #377 L3 the classifier reads its baselines here (`module_leaps/history.baselines`): the day baseline is the newest row from a session strictly before today, the quarter baseline the newest row at or before the last quarter end (O8), and growth is over the expiries present in both snapshots, far being decided at the baseline's date (O9). `leaps_cache.history` is frozen: carried unchanged, never appended (D1). Key `(run_id, ticker)`; append-only | — |
| `oipd_fit_history` | Result history (row table; schema 11, history.duckdb): `run_id`, `ticker`, `expiry DATE`, `as_of`, `spot DOUBLE`, `risk_free DOUBLE`, `horizon_days INTEGER`, `n_contracts INTEGER`, `dte INTEGER`, `t_years DOUBLE`, `forward DOUBLE`, `n_strikes INTEGER`, `atm_iv DOUBLE`, `g_min DOUBLE`, `g_min_window DOUBLE`, `clipped_pct DOUBLE`, `min_var DOUBLE`, `density_flag BOOLEAN`, `density_note`, `density_g_min_shown DOUBLE`, `rmse DOUBLE`, `mean DOUBLE`, `p05 DOUBLE`, `p10 DOUBLE`, `p25 DOUBLE`, `median DOUBLE`, `p75 DOUBLE`, `p90 DOUBLE`, `p95 DOUBLE`, `prob_below_spot DOUBLE`, `prob_above_spot DOUBLE`, `p_up10 DOUBLE`, `p_down10 DOUBLE`, `curve BLOB`, `smile BLOB`. One row per fitted expiry of an OIPD Fetch: the fit's inputs and statistics, the density verdict the board shows (window g-min, clipped mass, min variance and `opd_math.density_check`'s flag, note and shown g-min; the six density columns were added by #382, no schema bump), its ATM greeks, the SVI parameters (`a`, `b`, `rho`, `m`, `sigma`) and the curve and smile the board draws (JSON, compressed); `n_contracts` counts that expiry's contracts. Written by an OIPD Fetch (#382, `module_oipd/history.py`); every run is kept; a partial or failed chain, no fitted expiry or failed UW calls make the run `partial`. Key `(run_id, ticker, expiry)`; append-only | ``greeks``, ``svi`` |
| `rotation_holdings_history` | Result history (row table; schema 11, history.duckdb): `run_id`, `etf`, `holding`, `group_name`, `position INTEGER`, `name`, `weight DOUBLE`, `close DOUBLE`, `rs_1w DOUBLE`, `rs_1m DOUBLE`, `rs_3m DOUBLE`. The top holdings of every theme ETF with their SPY-relative strength, as the Rotation board embeds them (sector rows show the industry tree, `sector_tree_history`; no sector rows since M-ROT-1, ROT-4) (`position` from 1, the ETF's order; a holding listed twice keeps its first row). Written by a Rotation Fetch (#374, `module_sector_rotation/history.py`, from `app/runners.run_rotation_latest` after the board file); one run per session; no ETF returned, no ETF with holdings RS or failed UW calls make the run `partial` (header only). Key `(run_id, etf, holding)`; append-only | — |
| `rotation_snapshots_history` | Result history (mirror of `rotation_snapshots`; history.duckdb, added by #374 after schema 11, no bump): `run_id`, `row_no INTEGER` and every `rotation_snapshots` column. The RRG points a live Rotation Fetch saved (`store.save_snapshot`'s rows, `source` `live`), in the same run as `rotation_holdings_history` and `sector_tree_history`; one run per session. A range Fetch (stored snapshots) and the holdings job record none. Key `(run_id, row_no)` (`rotation_snapshots` has no PRIMARY KEY); append-only | — |
| `sector_tree_history` | Result history (row table; schema 11, history.duckdb): `run_id`, `sector`, `industry`, `ticker`, `etf`, `name`, `market_cap DOUBLE`, `weight DOUBLE`. The sector -> industry -> member tree of every sector ETF row, as the Rotation board embeds it (`weight` = share of the industry's market cap). Written by the same Rotation Fetch and run as `rotation_holdings_history` (#374). Key `(run_id, sector, industry, ticker)`; append-only | — |
| `money_flow_history` | Result history (row table; schema 11, history.duckdb): `run_id`, `side`, `rank INTEGER`, `etf`, `name`, `flow_score DOUBLE`, `as_of DATE`. The ranked inflows and outflows (`side` `inflow`/`outflow`, `rank` from 1; an inflow has a positive score and an outflow a negative one, so a side can hold fewer than 10 rows, DF70) of each Money Flow Fetch (#374, `module_sector_rotation/history.py`, board `module_money_flow`, from `app/runners.run_money_flow_latest` after the leaderboard file, or where the stored leaderboard was kept); one run per session; ETFs with no D01 bars (the leaderboard ranks `price_bars_daily` closes and volumes since M-ROT-3), nothing ranked, a kept leaderboard or failed UW calls make the run `partial` (header only). The newest complete session is the leaderboard an empty or partial pull keeps (`history.stored_leaderboard`, K2 `sessions`), and a missing board file is rebuilt from it. Key `(run_id, side, rank)`; append-only | — |
| `sec13f_metrics_history` | Result history (row table; schema 11, history.duckdb): `run_id`, `ticker`, `quarter DATE`, `name`, `sector`, `shares DOUBLE`, `value DOUBLE`, `holders INTEGER`, `top10_pct DOUBLE`, `hhi DOUBLE`, `new_holders INTEGER`, `exited_holders INTEGER`, `buy_flow DOUBLE`, `sell_flow DOUBLE`, `breadth DOUBLE`, `breadth_chg DOUBLE` (ownership breadth: the holders of the newest snapshot ÷ the distinct filer CIKs with long-equity holdings in that `quarter`, as a percentage with 4 decimals, and its change in percentage points vs the prior quarter, NULL when the quarter has no filer total; #375 F4, Chen-Hong-Stein 2002, as of quarter-end and filed up to 45 days later), `streak INTEGER`, `accel DOUBLE`, `pct_out DOUBLE`, `price DOUBLE`, `price_delta DOUBLE`, `shares_outstanding DOUBLE`, `float_shares DOUBLE`, `short_pct DOUBLE`, `insider_pct DOUBLE`. One row per 13F board row per 13F Fetch, from the entries the board renders (`report.board_entries`): the latest snapshot's metrics, the snapshot inputs (shares out and insider % from `ownership_snapshot`, the quarter-end price from D01, #375 F2; float and short % stay NULL until F6), the shares and quarter-over-quarter changes per quarter, and the CUSIPs the map gives the ticker. `quarter` is the board's latest quarter; `sec13f_metrics_by_quarter` takes each quarter's last run without an error. Written by a 13F Fetch (#375, `module_sec_13f/history.py`, from `app/runners.build_sec13f` after the board file); an SEC stage failure or a refused derive is the run's `error`, names not refreshed make it `partial`, the CUSIPs left out of the totals are a header warning. A Refresh and the store-only rebuild never record. Key `(run_id, ticker)`; append-only | ``shares_by_quarter``, ``chg_by_quarter``, ``cusips`` |
| `financials_report_history` | Result history (row table; schema 11, history.duckdb): `run_id`, `ticker`, `sector`, `data_fetched_at`, `modules BLOB`. One row per Financials Fetch: the modules run, the comps peers, the assumptions each model used, the warnings, Quick Remarks and every module's numbers (`modules`, JSON, compressed). Written by a Financials Fetch (#385, `module_financials/history.py`); a module that could not run, or a report the board did not get, makes the run `partial` with its row kept and the reason in `result_runs.quality_note`. Read by `module_financials/history.last_report` (#385, FIN-4): a store-only Refresh renders the newest stored run of the ticker that has comps (walking back at most 20 runs, decoding lazily, read-only), labelled with that run's fetch date in New York — so Refresh computes no comps and fetches nothing; a ticker with no recorded run reads as no comps, an explicit empty state. Key `(run_id, ticker)`; append-only | ``modules_run``, ``peers``, ``assumptions``, ``warnings``, ``quick_remarks`` |
| `forecast_ledger` | `source`, `forecast_date DATE`, `subject`, `target_type`, `target_date DATE` (NOT NULL), `frozen_at DOUBLE`, `run_id`, `model_version`, `rank INTEGER`, `score DOUBLE`, `bucket`, `market_cap DOUBLE`, `sector`, `shown_price DOUBLE`, `stop DOUBLE`, `target DOUBLE`. Key `(source, forecast_date, subject, target_type, target_date)`; no outcome columns | `probs` |
| `forecast_outcomes` | The ledger key (`source`, `forecast_date DATE`, `subject`, `target_type`, `target_date DATE` NOT NULL) + `horizon_days INTEGER`, `ref_price DOUBLE`, `mfe DOUBLE`, `mae DOUBLE`, `sessions_seen INTEGER`, `status` (`open`/`closed`/`no_bars`/`split_check`), `computed_at DOUBLE`, `model_version`, `run_id`. Key `(source, forecast_date, subject, target_type, target_date, horizon_days)`; keyed upsert (derived, #380 S0, added after schema 11 with no bump) | — |
| `forecast_baselines` | `forecast_date DATE`, `horizon_days INTEGER`, `basis` (`universe` or `vol_decile_1` … `vol_decile_10`, 1 the calmest), `n INTEGER` (members with a measurable outcome), `ge20`/`ge10`/`ge5 INTEGER` (cumulative counts of them whose MFE reached 20% / 10% / 5%), `mfe_mean DOUBLE`, `mae_mean DOUBLE`, `vol_lo DOUBLE`, `vol_hi DOUBLE` (the decile's boundaries, `vol_lo` inclusive and `vol_hi` exclusive, NULL at either end), `status` (`open`/`closed`), `computed_at DOUBLE`, `model_version`, `run_id`. Key `(forecast_date, horizon_days, basis)`; keyed upsert (derived, #25 HR-1, added after schema 12 with no bump) | — |
| `rotation_snapshots` | `as_of_date`, `ticker`, `name`, `"group"`, `close_price`, `rs_*`, `rs_ratio`, `rs_momentum`, `quadrant`, `benchmark`, `generated_at`, `source` (`live` from a Rotation Fetch, `backfill` from a range backfill; added by #374, no schema bump; older rows are NULL). Since M-ROT-2 (#374) both come from one function on D01 weekly closes and `as_of_date` is the latest complete session they end on (no weekend or holiday rows) | — |
| `ticker_scores` | 30 scalar columns (`market_cap DOUBLE`, `sector`, `industry_group`, `sub_industry`, `themes`, `signals`, …), plus `tier INTEGER` (3 Balanced, 2 Strong, NULL none), `flags` (JSON list of the C17/C18 data checks), `model_version` (the AG model version that scored the row), `anchor_fy INTEGER`, `prior_fy INTEGER` and `period_ends` (JSON object: revenue ends of the anchor and two prior years, balance-sheet and cash-flow ends), added by #380 AG W1 with no schema bump (ALTER on the first store call; rows of an older Fetch read NULL and the board shows them under a "scored before" notice) | — |
| `leaps_flow_prints` | `id`, `ticker`, `strike`, `expiry`, `premium`, `size`, `option_type`, `delta`, `next_earnings_date`, `executed_at`, `discovered_at`, `ask_vol`, `bid_vol`, `source`, `fetched_at DOUBLE`, `session_date DATE`, `run_at DOUBLE` (the last four: schema 11; stamped on each new print since #295: `source` 'uw', `fetched_at` its page, `run_at` the scan's start, `session_date` rule B on `run_at`; older rows NULL) | — |
| `leaps_daily_scores` | `ticker`, `date`, `frequency`, `notional`, `concentration`, `concentration_mult`, `final_score` | — |
| `sec13f_submissions` | `accession`, `cik`, `period`, `filing_date`, `type`, `report_type`, `is_amendment`, `amendment_type`, `amendment_no`, `other_managers_count` | — |
| `sec13f_holdings` | `accession`, `line_no`, `cusip`, `cik`, `shares`, `value`, `period`, `putcall`, `sshprnamttype`, `title_of_class`, `investment_discretion`, `other_manager`, `voting_sole`, `voting_shared`, `voting_none`; `PRIMARY KEY (accession, line_no)` | — |
| `sec13f_cusip_names` | `cusip`, `name_norm`, `name_raw`, `count` | — |
| `sec13f_ingested_zips` | `filename`, `ingested_at` | — |
| `oipd_chain` | `ticker`, `option_symbol`, `expiry`, `option_type`, `strike`, `bid`, `ask`, `last_price`, `volume`, `open_interest`, `iv`, `delta`, `tape_time`, `fetched_at` | — |
| `oipd_meta` | `ticker`, `as_of`, `spot DOUBLE`, `risk_free DOUBLE`, `expiry_count`, `horizon_days`, `fetched_at` | — |
| `sec_company_tickers` | `ticker`, `cik`, `name` | — |
| `security_master` | `ticker`, `cik`, `cik_source`, `exchange`, `name`, `name_source`, `security_type`, `type_source`, `type_asof`, `sector`, `sector_source`, `industry_group`, `group_source`, `industry`, `industry_source`, `sub_industry`, `sub_source`, `gics_asof`, `updated_at`, `figi`, `composite_figi`, `share_class_figi`, `exch_code`, `security_description`, `figi_source`, `sic`, `sic_description`, `sic_group2`, `sic_source`, `cusip`, `cusip_source`, `isin`, `isin_source` (one row per ticker; every field carries its `<field>_source`; since #265 `sector` is one of the 11 Morningstar names, from the S&P 1500 list for its own names, else UW / Yahoo / SIC / type rule (#272), `industry` is UW's text, and `industry_group`, `group_source`, `sub_industry`, `sub_source` are NULL; since #272 `sic` is the 4-digit code (TEXT, leading zero kept), `sic_group2` its two-digit major group and `sic_source` `sec_submissions` or `sec_company_sic`, raw and independent of the sector; since #267 the five FIGI columns are copied from the last good `openfigi` row, never blanked, with `figi_source` `openfigi`; since #127 `cusip` is the OpenFIGI 13F map inverted by ticker (`cusip_source` `openfigi_13f`; two CUSIPs for one ticker keep the newest `valid_from`, then the lowest CUSIP) and `isin` is the ISO 6166 value derived from a digit-leading valid CUSIP (`isin_source` `derived_cusip`; a CINS or a name with no CUSIP is NULL in `isin`, and NULL in both when the map has no CUSIP); nothing reads the FIGI columns yet) | — |
| `identity_conflicts` | `ticker`, `kind`, `field`, `side_a`, `side_b`, `note`, `first_seen`, `last_seen` (key `(ticker, kind, field)`) | — |
| `identity_source_cache` | `source`, `ticker`, `fetched_at`, `status`, `security_type`, `security_type2`, `sector`, `industry`, `sub_industry`, `cik`, `sic`, `is_etf BOOLEAN`, `forms` (comma-joined), `shell_closed BOOLEAN` (key `(source, ticker)`; last GOOD answer per source — a failed or skipped source never overwrites it, `core/identity/cache.py`) | `payload` (rest of the source row) |
| `identity_runs` | `run_id`, `started_at`, `finished_at`, `status` | `sources` (per-source status/counts/error), `counts` |
| `board_settings` | `board`, `key`, `value DOUBLE`, `updated_at` (key `(board, key)`; only user overrides — defaults are read from the board's config, and a Save is one transaction and a Reset deletes the board's rows in one statement, serialized by a lock; every save, drop and Reset is kept in `board_settings_changes`, a Reset as one change (#306); `core/board_settings.py`, issue #40; read by Co-movement (`module_comovement/settings.py`) and by Swing for its screen/score thresholds (`module_swing_potential/settings.py`, #384 SW-S4), whose stored overrides also feed the forecast ledger's model version) | — |
| `watchlist` | `ticker` (one row per pinned ticker; `rawstore.toggle_watchlist` writes one ticker per click, #306) | — |
| `migration_manifest` | `dataset`, `migrated_at`, `schema_version`, `source_count`, `source_bytes`, `source_hash`, `verify` | — |
| `store_meta` | `key`, `value` | — |
| `store_schema` | `key`, `value` (one row, key `schema_version`, this code's `_SCHEMA_VERSION`; the same table is created in every store file on ensure, #26 PL7, no schema bump — not `store_meta`'s ticker-store `schema_version`) | — |
| `price_actions` | `ticker`, `ex_date DATE`, `action`, `value DOUBLE`, `session_date DATE`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, ex_date, action)` | — |
| `index_levels_daily` | `symbol`, `session_date DATE`, `open DOUBLE`, `high DOUBLE`, `low DOUBLE`, `close DOUBLE`, `volume DOUBLE`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(symbol, session_date)`; prices.duckdb (#293) | — |
| `quote_snapshot` | `ticker`, `session_date DATE`, `source`, `market_cap DOUBLE`, `price DOUBLE`, `shares_outstanding DOUBLE`, `week_52_high DOUBLE`, `week_52_low DOUBLE`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, session_date, source)` | `payload` is **zlib-compressed JSON** (`BLOB`) |
| `short_interest` | `ticker`, `session_date DATE`, `market_date DATE`, `short_interest DOUBLE`, `si_float DOUBLE`, `total_float DOUBLE`, `days_to_cover DOUBLE`, `fee_rate DOUBLE`, `rebate_rate DOUBLE`, `short_shares_available DOUBLE`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, session_date)` | `payload` |
| `vendor_financials` | `ticker`, `source`, `statement`, `report_type`, `period_end DATE`, `valid_from DATE`, `status`, `revenue DOUBLE`, `gross_profit DOUBLE`, `operating_income DOUBLE`, `ebitda DOUBLE`, `net_income DOUBLE`, `operating_cash_flow DOUBLE`, `total_assets DOUBLE`, `inventory DOUBLE`, `receivables DOUBLE`, `deferred_revenue DOUBLE`, `rpo DOUBLE`, `total_debt DOUBLE`, `interest_expense DOUBLE`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, source, statement, report_type, period_end, run_at)` | `payload` |
| `vendor_valuation` | `ticker`, `source`, `session_date DATE`, `enterprise_value DOUBLE`, `ebitda DOUBLE`, `total_revenue DOUBLE`, `net_income_to_common DOUBLE`, `trailing_pe DOUBLE`, `beta DOUBLE`, `currency`, `long_business_summary`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, source, session_date)` | `payload` is **zlib-compressed JSON** (`BLOB`) |
| `earnings_history` | `ticker`, `endpoint`, `report_date DATE`, `valid_from DATE`, `status`, `report_time`, `actual_eps DOUBLE`, `eps_estimate DOUBLE`, `surprise_pct DOUBLE`, `expected_move_perc DOUBLE`, `post_earnings_move_1d DOUBLE`, `fiscal_period_end DATE`, `report_type`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, endpoint, report_date, run_at)` | `payload` |
| `estimate_snapshots` | `ticker`, `session_date DATE`, `period`, `eps_est_analysts DOUBLE`, `eps_est_avg DOUBLE`, `eps_est_low DOUBLE`, `eps_est_high DOUBLE`, `eps_est_year_ago DOUBLE`, `eps_est_growth DOUBLE`, `rev_est_analysts DOUBLE`, `rev_est_avg DOUBLE`, `rev_est_low DOUBLE`, `rev_est_high DOUBLE`, `rev_est_year_ago DOUBLE`, `rev_est_growth DOUBLE`, `eps_trend_current DOUBLE`, `eps_trend_7d DOUBLE`, `eps_trend_30d DOUBLE`, `eps_trend_60d DOUBLE`, `eps_trend_90d DOUBLE`, `eps_rev_up_7d DOUBLE`, `eps_rev_up_30d DOUBLE`, `eps_rev_down_7d DOUBLE`, `eps_rev_down_30d DOUBLE`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, session_date, period)` | `payload` |
| `identity_cusip` | `cusip`, `valid_from DATE`, `status`, `ticker`, `figi`, `share_class_figi`, `share_class`, `name`, `exch_code`, `security_type`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(cusip, run_at)` | — |
| `ownership_snapshot` | `ticker`, `session_date DATE`, `source`, `shares_outstanding DOUBLE`, `insider_pct DOUBLE`, `institutional_pct DOUBLE`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, session_date, source)` | — |
| `sec_mf_file` | `class_id`, `valid_from DATE`, `status`, `cik`, `series_id`, `symbol`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(class_id, run_at)` | — |
| `sec_exchange_file` | `ticker`, `valid_from DATE`, `status`, `cik`, `name`, `exchange`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, run_at)` | — |
| `earnings_calendar` | `session_date DATE`, `earnings_date DATE`, `ticker`, `report_session`, `sector`, `full_name`, `marketcap DOUBLE`, `expected_move_perc DOUBLE`, `street_mean_est DOUBLE`, `pre_earnings_close DOUBLE`, `post_earnings_close DOUBLE`, `report_time`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(session_date, earnings_date, ticker, report_session)` | `item` |
| `macro_series` | `series_id`, `obs_date DATE`, `source`, `value DOUBLE`, `frequency`, `proxy_for`, `session_date DATE`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(series_id, obs_date, source)` | — |
| `vol_index_daily` | `symbol`, `session_date DATE`, `source`, `open DOUBLE`, `high DOUBLE`, `low DOUBLE`, `close DOUBLE`, `operator`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(symbol, session_date, source)` | — |
| `cftc_cot` | `report_type`, `market`, `report_date DATE`, `release_date DATE`, `open_interest DOUBLE`, `asset_mgr_long DOUBLE`, `asset_mgr_short DOUBLE`, `asset_mgr_change_long DOUBLE`, `lev_money_long DOUBLE`, `lev_money_short DOUBLE`, `lev_money_change_long DOUBLE`, `dealer_long DOUBLE`, `dealer_short DOUBLE`, `dealer_change_long DOUBLE`, `session_date DATE`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(report_type, market, report_date)` | `raw_row` |
| `economic_calendar` | `session_date DATE`, `event_ts DOUBLE`, `event`, `type`, `forecast`, `prev`, `reported_period`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(session_date, event_ts, event, type)` | `raw_row` |
| `source_checks` | `dataset`, `source`, `key`, `checked_at DOUBLE`, `run_id`. Key `(dataset, source, key)` | — |
| `price_checks` | `dataset`, `source`, `key`, `checked_at DOUBLE`, `run_id`. Key `(dataset, source, key)`; prices.duckdb (#156) | — |
| `sec13f_filers` | `accession`, `cik`, `period`, `name`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(accession)` | — |
| `edgar_companyfacts` (edgar DB) | `cik`, `taxonomy`, `concept`, `unit`, `period_start`, `period_end DATE`, `accn`, `value DOUBLE`, `form`, `fy INTEGER`, `fp`, `filed DATE`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(cik, taxonomy, concept, unit, period_start, period_end, accn)` | — |
| `sec_submissions` (edgar DB) | `cik`, `accession`, `form`, `filing_date DATE`, `primary_document`, `items`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(cik, accession)` | — |
| `sec_submissions_cik` (edgar DB) | `cik`, `valid_from DATE`, `status`, `name`, `sic`, `sic_description`, `entity_type`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(cik, run_at)` | `tickers`, `exchanges` |

### Identity refresh (issues #85, #89)

`POST /run/identity` (long lane; the Settings **Fetch identity** button; from a shell add `-H "X-RC-Panel: cli"`) or `python -m RevenueChaser.core.identity --refresh
[--limit N] [--sources a,b] [--yahoo-limit N]` runs `core/identity/refresh.py` over every `equity_universe` ticker.
It writes `security_master`, `identity_conflicts`, `identity_source_cache` and `identity_runs`, plus one
sanctioned outside write: the SEC exchange map is **not** fetched here any more — the nightly RC-Daily-Bars step
pulls the file once a weekday night and this job reads the stored `sec_exchange_file` rows (#301 D21), so
`sec_company_tickers` is retired and nothing writes it. After a successful build (status `ok` or
`partial`, never after an error) the master's sector and industry (and UW's next earnings date) are applied to
`equity_universe` (`core/identity/apply.py`, issues #86 and #265: those columns, with `industry_group` and
`sub_industry` written NULL; tickers with no master row keep their stored values; the counts `applied` /
`unchanged` / `not_in_master` are in the run record).
Deltas: SEC submissions for tickers with no cached row or one older than 30 days (cache rows keyed by ticker),
plus a stagger (the oldest 25% of the cached rows that are at least 7 days old, so the cache does not expire in
one go), OpenFIGI older than 90 days (the four fields #267 added, `compositeFIGI`, `shareClassFIGI`, `exchCode` and
`securityDescription`, reach a ticker's master row when its OpenFIGI row is next fetched; a row cached earlier keeps
only its `figi`); Nasdaq, the S&P 1500 lists (#272) and UW sectors every run; Yahoo only where UW has no usable
sector (one of the 11 names); FDIC only for candidates (no SEC periodic filings, OpenFIGI common). The three
Wikipedia pages must all parse; a new list snapshot that changes the sector of more than 3% of the names in both it
and the last accepted snapshot is rejected (the previous snapshot stays, status `gated`, an `identity_conflicts` row
of kind `source`, field `sp1500_snapshot`). A run with no fresh accepted snapshot (failed, gated or without the list)
reads the last accepted snapshot only: the cache never deletes a row, so a name that has left the list keeps its old
row but is not listed. The SIC columns and the SIC sector step read the SEC submissions cache and
`sec_company_sic` only: no extra SEC call.
SPDR is a check, never a value: the 11 Select Sector holdings files (each fund under its Morningstar sector name)
are compared with the master sector of each S&P 500 member (`index_constituents`, slug `sp500`) and a mismatch is an
`identity_conflicts` row of kind `spdr` (`counts.spdr_checked` / `spdr_agree`); the result is not cached, is not in
"sources ok", and a failed check leaves the run `ok`.
Builder inputs follow `master.py`: a failed source with no cached row is `None`, a healthy source without a row
is `{}`. A run that cannot finish records status `error` instead of raising. Schema version is `9` (#272, the four SIC
columns).

Notes:
- Several caches are **partially flattened**: the scalar field a query filters
  on was hoisted out of the JSON into a real column
  (`ticker_payload_cache.fetched_at`, `swing_info.cached_at`,
  `rotation_holdings.date`), while the heterogeneous remainder of the record
  stays in the `payload`/`rows` JSON column.
- `ticker_scores.themes` / `signals` (and, since #380 W1, `flags` / `period_ends`) are `TEXT` columns that
  *contain* JSON text (SQLite legacy) — typed columns, JSON values. The runtime theme SSoT is the
  separate `ticker_themes` table, not these blobs.
- `edgar_financials` holds the only large JSON payload left (~146 MB of text,
  ~21 KB per ticker); it is always read per-ticker (`WHERE ticker = ?`), so it is
  not flattened. DuckDB stores string cells that large **uncompressed** (and
  `force_compression='zstd'` does not change that), so `annual`/`quarterly` and
  `chain_filings.item_text` (~169 MB of prose) are zlib-compressed in Python:
  MANIFEST `"compress": {column: "json"|"text"}` on a `BLOB` column; `write_rows`
  compresses, `get`/`get_all` decompress. Raw SQL sees bytes — read those columns
  through `get`/`get_all`. An existing table whose column is still text is rebuilt
  once (in one transaction) by `_ensure_table`; that rebuild also undid the old
  double-JSON-encoding of every `edgar_financials` row (a `_migrate_edgar_tables`
  bug: `query()` returned the JSON as text and `write_rows` encoded it again, so
  `sec_edgar.get_fundamentals` never got a list back and treated the cache as
  empty). On the live store: edgar.duckdb 507 MB → 81 MB after rebuild +
  compaction. The `quote` column is JSON rather than a pinned `STRUCT` so new
  provider fields are never silently dropped.

## The market store (`market/`)

The new data system (#206 F1/D1, #217 F12/D4), built beside the `.store/` files above, which it does not touch. One
**builder** process writes it; the dashboard and the boards only read it, through the library package `rcdata`.
PR 1 lays the skeleton and proves it on the SPX expiry-strike slice and the market-wide series; the views under the
old table names, the routes and the Settings cards come in later PRs.

**Roots** (environment, read in `core/config.py` through `_root()` like `RC_STORE_DIR`; the repo-local defaults
`.market/` and `.market_raw/` are git-ignored, so a checkout, the tests and the cloud never touch a server path):

| Variable | Constant | Holds |
|---|---|---|
| `RC_MARKET_DIR` | `MARKET_DIR` | The serving DuckDB files and `ops.sqlite` (the server's SSD) |
| `RC_MARKET_RAW_DIR` | `MARKET_RAW_DIR` | `raw/<source>/<dataset>/...` and `parquet/<source>/<dataset>/<partition>/` (the HDD) |
| `RC_MARKET_RAW_COPY_DIR` | `MARKET_RAW_COPY_DIR` | Where the backup copies the newest raw days; empty = off |

**Files.** Serving kinds: `market`. Each kind is one file, `<MARKET_DIR>/<kind>.duckdb`; today one,
`market` (serving schema version `1`): `serving_schema_version` (one row: kind, version, build time, build id),
`spx_gamma_expiry_strike` (key session date, expiry, strike; every numeric field UW serves, typed DOUBLE; provenance
`source`, `source_as_of`, `fetched_at`, `run_id`, `observed`) and `market_series` (key series id, observation date,
source; `value`, OHLC for the index files, `frequency`, `session_date`, provenance). `session_date` comes from
`core/calendar.py` and is empty outside its 2025-2028 table (its rule C: no second calendar). The previous file
stays as `<kind>.prev.duckdb`.

**The swap rule.** A kind is built as a new file `<kind>.<build_id>.duckdb`, from Parquet only (no network), and
checked before it replaces anything: its `serving_schema_version` row, the row count of every table, and the run's
`dataset_check` row (a `failed` one refuses the swap). Then the live file is hard-linked to `<kind>.prev.duckdb` and
`os.replace` moves the new file over the live name, so the live name is never missing. Any failure deletes the new
file and leaves the live file byte-identical (test T3, `rcdata/tests/test_serving.py`). The dashboard opens a serving
file read-only on every request (`rcdata.serving.open_read`) and never writes one; serving files are never
compacted, they are rebuilt. Pages only enqueue requests; the builder takes them in order.

**Raw and Parquet.** A raw file that exists is never fetched again: it is the resume marker. Raw files and Parquet
partitions are written to `<name>.tmp` and renamed, so a file under its real name is whole (zstd Parquet, row groups
of 100k rows). The SPX slice is one call per session: `raw/uw/spot_expiry_strike/<date>/SPX.json.gz`, Parquet
`parquet/uw/spot_expiry_strike/date=<date>/`. The market series keep one raw file per fetch with a `.meta.json`
sidecar (as-of date, fetch time, run id, URL without its query) and Parquet per source and year, merged by key with
the newest fetch winning a key.

**`ops.sqlite`** (SQLite WAL, `busy_timeout` 5000 ms on every open; `rcdata/ops.py` owns every statement;
ops schema version `1`, and a file stamped newer is refused):

| Table | Holds |
|---|---|
| `ops_schema` | The applied ops schema version |
| `build_requests` | The FIFO request queue (queued, running, done, failed, skipped) |
| `dataset_check` | One row per dataset run: changed, unchanged, failed or skipped; rows, added, removed, coverage, calls |
| `build_issues` | A row the builder could not use, with its key, until resolved |
| `job_runs2` | One row per builder run |
| `read_log` | Reads per day, process and table (counted in memory, written once a minute) |
| `uw_remaining` | UW's `x-uw-*` rate headers, sampled once a run |
| `provider_calls_daily` | Calls, failures and 429s per provider and New York day |
| `watchlist_option`, `watchlist_stock`, `watchlist_option_changes`, `watchlist_stock_changes` | The two watch lists and their history (no UI yet) |
| `dataset_schedule` | Cadence and pause state per dataset (no routes yet) |

**Day caps** (`rcdata/caps.py`, avo's numbers, to be tuned): uw 150,000, sec 40,000, fred 10,000, alpaca 20,000;
a dataset whose provider reached its cap for the New York day gets a `skipped` check row and sends nothing.

**Running it.** `python -m RevenueChaser.rcdata.build run [--once] [--dataset X [--as-of D]]` and
`python -m RevenueChaser.rcdata.build backfill <dataset> --from <date>`. It refuses to start (exit 3) while a job
lock is held in the store directory or another builder runs.

**Backup.** `python -m RevenueChaser.rcdata backup` writes under `<RAW_STORE_BACKUP_DIR>/market/` and refuses an empty
`RAW_STORE_BACKUP_DIR` (exit 2, nothing written): `ops.sqlite` through SQLite's online backup API, each serving file
copied and verified by row count and `sum(hash(row))` per table before its rename, the sha256 of every copy in
`.manifest.json`, the newest 5 kept per kind. Then the raw files of the last 35 days go to `RC_MARKET_RAW_COPY_DIR`
(rsync-like) when they total under 20 GB; over that, or with no folder set, a warning and no copy.

## Migration
Two separate things, deliberately not conflated:

- **Legacy import (one-time).** `./app.sh migrate-raw-data` rebuilds every table
  from the source-of-truth legacy tree (currently the unzipped
  `~/Downloads/rawData/`). It stores the JSON datasets in the typed layout
  above; the composite SQLite databases (13F, LEAPS, ticker store) are
  bulk-copied unchanged.
- **In-store schema evolution (ongoing).** `docs/plan_store_schema_evolution.md`
  documents which columns are selectable vs JSON, and why. `_ensure_table` in
  `core/rawstore.py` self-heals a store that predates a MANIFEST column: it
  emits `ALTER TABLE … ADD COLUMN` for anything missing, so a schema addition
  never turns into a silent no-op + `Binder Error` on the next write. The store
  is **durable, not disposable** — do not delete it. `./app.sh migrate-raw-data
  --reset` is only for a *fresh* rebuild from a legacy source tree; a live store
  is upgraded in place by `_ensure_table`. It fixes only the missing column. A
  retyped column, a changed `PRIMARY KEY` and a live column the MANIFEST no
  longer has are **not** migrated; they are recorded and logged once per
  process (#285) and listed by `rawstore.schema_drift()` (`db`, `table`, `kind`
  = `type` / `key` / `extra_column`, `column`, `live`, `manifest`). The schema
  apply of a process (`ensure_all_tables`, the first store call, read-write)
  also keeps what it saw in `store_meta`, key `schema_drift` (#312): value
  `{"recorded_at": <UTC>, "drift": [<those dicts>]}`, written only when the
  list differs from the stored one, so a store with drift gets the row, a
  store whose drift is gone gets an empty list, and a fresh or unchanged store
  writes nothing. It is the latest record, not a history (the log keeps that);
  a failed write is logged and never fails the store call, and with
  `RC_STORE_AUTO_ENSURE=0` nothing is written. Nothing migrates by
  itself, with one exception the log names: a table whose `compress` columns
  are still text is rebuilt from the MANIFEST right after the check, which
  replaces every difference it found (an extra column's data is dropped) and
  removes that table's records from `schema_drift()`.
  `core/tests/test_rawstore_schema_drift.py` also builds a fresh store
  from the MANIFEST and fails when a table's `PRIMARY KEY` differs from its
  declared `key` (the replace-mode tables without a constraint are listed
  there).

## Backups, restore and compaction
- **Backup** — `python -m RevenueChaser.scripts.backup_store` (scheduled daily by
  `register_tasks.ps1`). Per file: CHECKPOINT (through rawstore, so a busy store
  is waited out), copy, then verify every table's row count **and** content
  checksum (`sum(hash(row))`) against the live file; a busy store, a failed copy,
  a mismatch or a copy that opens but cannot be read (a damaged data block) is retried (bounded). Retains 5 `raw_*`, 2 `edgar_*`, 5 `history_*` (`--keep-history`; only once
  `history.duckdb` exists, #287), 2 `prices_*` (`--keep-prices`, #156; no weekly copy, because the price tables
  are append-only and the newest snapshot holds every row: about 4.7 GiB locally at the expected 2.4 GiB file)
  snapshots and 2 `edgar_docs_*.zip` archives (verified: member CRCs + file count; an unchanged tree reuses the
  newest archive, see below)
  under `%LOCALAPPDATA%\RevenueChaser\backups` (override `RAW_STORE_BACKUP_DIR`), plus the weekly
  `raw_*` copies (the first snapshot of each ISO week, the newest `--keep-weekly` of them, #342), the SPX mirror and an optional off-machine copy, described below. A step that fails
  is logged as `<step> FAILED: <why>` and the steps after it still run (so one failed step never costs the rest of
  the night's backup); an unexpected error, such as an `OSError` on a full disk, also prints its type and stack. The
  run then exits 1, and only the off-machine copy is skipped.
- **`edgar_docs/` archive (#336)** — the zip carries a fingerprint of the tree it was made from (SHA-256 over every
  file's relative path, size and modified time, a `stat` walk) in its zip comment. When the tree's fingerprint equals
  the newest archive's, the run writes no new archive and logs `backup OK: <path> (unchanged, kept)`; the off-machine
  copy then finds that file already there. The reused archive gets a cheap check each night (it opens as a zip, the
  fingerprint matches, it lists every document); the member CRCs were checked when it was written, and re-reading it
  is left to the periodic integrity scan below (#309 L1), which covers the local `edgar_docs_*.zip` files. An archive that is not a readable zip or misses a
  document is never reused: a new one is zipped and verified, then the damaged one is removed. An archive from before
  #336 has no fingerprint, so the first run after the change zips the tree once more. A file whose content changed
  with the same size and the same modified time is not seen. Restore is unchanged: unzip the chosen archive.
- **The Discord archive is no longer backed up here (October 2026)** — it became its own app and backs
  itself up. `discord.duckdb` is not snapshotted and the attachment tree is not mirrored. What earlier
  runs left behind is **not** pruned or deleted by anything here; it stays until the owner removes it by
  hand (#175), after the new app has taken a verified backup of its own:
  - in the backup folder: the `discord_*.duckdb` snapshots, the `discord_media_mirror/` folder and any
    `discord_media_*.zip` archive from before that mirror (#248);
  - in the off-machine folder (`<RAW_STORE_BACKUP_MIRROR>/RevenueChaser-backup`): the `discord_*.duckdb`
    copies (up to 3) and its own `discord_media_mirror/`.

  - in the store folder: the old `.store/discord.duckdb` (and its `.wal`). It stays, allowed by
    `core/tests/test_store_layout.py` as a retired file, until the owner removes it by hand.

  The local leftovers **are still scanned**: the integrity scan below walks every file of the backup
  folder and re-hashes each one its manifest lists, these included (some two thousand attachment files). So
  the night after they are deleted by hand, the scan reports every one of them as
  `integrity scan FAILED: <path>: missing` and the run exits 1. That is expected and happens once: the
  scan drops a missing file from the manifest when it reports it, and a scan that failed runs again the
  next night, which then passes. The off-machine leftovers are never scanned, so removing them reports
  nothing.
- **Weekly raw copy (#277, bounded by #342)** — the first verified `raw_*` snapshot of each ISO week (by its UTC
  stamp) is also copied to `weekly/` in the same backup folder. The copy is written under a temporary name,
  fingerprinted against the snapshot (row count and content checksum per table) and only then renamed, so a failed
  copy leaves nothing under the final name and the next night tries again. The newest `--keep-weekly` (12) copies
  stay: the nightly run prunes the older ones out of `weekly/` (after that night's copy, or after a night that added
  none because the week had one), they leave the #309 manifest before they are deleted, and the prune never looks at
  the top folder — as the top folder's `_prune` globs never look in here, so `restore_store --latest` keeps reading
  the top folder only. It was a stopgap for raw tables that keep only the latest value; the change history has landed
  (#279), so the owner can turn it off (the default is on), about 430 MB a week at today's size. `--no-weekly` or
  `RAW_STORE_BACKUP_WEEKLY=0` (also `false`, `no`, `off`) stops new weekly `raw_*` copies; the ones already made stay.
  The `history_*` weekly copies of earlier runs are no longer written (#342: history grows ~6.55 GB/yr) and this prune
  does not touch them: deleting them is the owner's own step. Log line: `weekly backup OK: <path>`, printed only in
  the run that writes a week's copy.
- **SPX Gamma tree (#276)** — `module_spx_gamma/data/` (or `SPX_GAMMA_DATA_ROOT`) is mirrored into
  `spx_gamma_mirror/` in the same backup folder, incrementally: a `stat` walk decides what changed (same size and
  an mtime within 2 s means unchanged), each copy goes through `spx_gamma_mirror/.tmp/` and is verified by size and
  SHA-256 before it replaces the mirror file, a file modified in the last 10 minutes (still being written) waits for
  the next run, and nothing is ever deleted from the mirror. Two extra rules for this tree. A `.json` file
  that no longer parses never replaces a mirror copy that does: the run keeps the good copy, prints
  `spx mirror WARNING: kept the previous copy of <n> file(s) whose source is not valid JSON: <names>` and exits 1
  after every other step has run; a repaired source replaces the copy on the next run. And every version of the two
  files that are rewritten in place, `brier.json` and `spx_gamma_gex_history.json`, also gets a dated copy
  `<name>.<UTC mtime>.json` in `spx_gamma_versions/`, beside the mirror, that is never replaced or pruned and is
  checked against the verified mirror copy (SHA-256) before it gets its name. Log line:
  `spx mirror OK: <copied> copied, <unchanged> unchanged, <n> in flight, <total> files`. About 1.3 GB a year (the
  review's estimate). It sits on the same disk as the live tree, so on its own it protects against deletion and
  corruption, not against losing the disk; the off-machine copy below copies it and `spx_gamma_versions/` too. #179 retires it when it moves the tree into DuckDB.
- **Off-machine copy (#275, #349)** — set `RAW_STORE_BACKUP_MIRROR` (in `.env`; empty means off and the step does
  nothing) to a folder that is not on this machine's disk: a Google Drive for desktop folder or an external disk.
  After a nightly run in which every step above was verified, `backup_store` copies the newest snapshot of each kind
  and the two mirror folders (`spx_gamma_mirror/`, `spx_gamma_versions/`) into the
  sub-folder `RevenueChaser-backup` of that folder, so it may be an existing Drive folder: nothing outside the
  sub-folder is written, pruned or deleted. The small kind, `history_*`, goes every run and the newest
  `--keep-mirror` (default 3) stay. The big kinds, `raw_*` and `edgar_*` snapshots and `edgar_docs_*.zip` (about
  1.9 GB a set on 2026-10-08), go at most every 7 days: only when the newest copy of that kind there is at least
  `--mirror-big-every-days` calendar days old (default 7; `0` sends them every run), or there is none yet, or the run
  has `--mirror-big-now`. The newest `--keep-mirror-big` (default 2) stay. The reason is the Drive trash: Drive for
  desktop moves every pruned copy into it, and it counts toward the Google quota for 30 days, so a daily big set held
  about 60 GB of a 200 GB account and filled it on 2026-10-08; weekly with 2 kept holds about 12 GB (an estimate). A
  manual extra run inside the week therefore sends only the small kinds. The `prices_*` snapshot (#156; about 2.4 GiB
  once filled) goes last, after the mirror folders, so a failed upload of the biggest file costs no other copy (the
  mirrors cannot be fetched again; the prices can). It goes once every 7 days (`MIRROR_PRICES_EVERY_DAYS`, which
  `--mirror-big-every-days` does not change; `--mirror-big-now` sends it too) and one copy stays (`KEEP_MIRROR_PRICES`;
  avo 2026-10-09). The `weekly/` copies stay on this machine
  (#349); `weekly/` copies that earlier versions sent stay in the second folder until removed by hand. Each file is
  written under a temporary name, verified there again (row count and content checksum per table for a snapshot;
  size, SHA-256 and a CRC check for a zip; size and SHA-256 for the files of a mirror) and only then renamed, so a
  half-written file never has its final name. A file that is already there is not copied again. Pruning follows the
  verified copy, and nothing is ever deleted from the mirror folders. The folder must exist already (only its last
  level and `RevenueChaser-backup` are created, so a disk that is not plugged in is an error and never a new folder
  on this one), and it must not be the backup folder, inside it or around it. Log line:
  `off-machine copy OK <iso>: <n> copied, <n> already there -> <folder>/RevenueChaser-backup` (with
  `, big files skipped until <date>` before the arrow in a run that did not send them), or
  `off-machine copy FAILED <iso>: <why>` with exit 1. The Automations row of `RC-Store-Backup` reads these lines and
  shows the age of the last good copy; it is red when no folder is set, no copy was made yet, the newest attempt
  failed, or the last good copy is older than 48 hours. A run in which an earlier step failed makes no copy and logs
  `off-machine copy FAILED <iso>: skipped, <n> backup step(s) failed earlier tonight`, so the row is red that night.
- **Manifest and integrity scan (#309 L1)** — every file the run writes into the local backup folder (snapshots,
  `weekly/` copies, `edgar_docs_*.zip`, every file of the three mirror folders) is recorded when it is copied, with
  its size and SHA-256, in `.backup_manifest.json` at the top of the backup folder (beside the mirrors, never inside
  one, so copying a mirror back does not carry it); a pruned file leaves it. As the last backup step, every
  `--scan-every-days` (default 7; `0` = every run; `--scan-now` forces it) the run re-reads the whole local folder
  against it: each listed file is hashed again and its size compared. A file the manifest does not have yet (made
  before #309) is first self-checked (every table of a `.duckdb` read, every member CRC of a zip) and then recorded.
  The scan is read-only for the backups: nothing is deleted, moved or rewritten; it writes only the manifest and its
  marker `.integrity_scan.json` (day, result, next due day). A mismatch, a missing file or a failed self-check prints
  `integrity scan FAILED: <path>: <why>` and the run exits 1 (after every other step, the off-machine copy
  included); a failed scan is due again the next night. A mismatch or a failed self-check stays red until a scan
  passes; a missing file is reported once (its entry leaves the manifest). Otherwise the log
  line is `integrity scan OK: <n> files, <x> GB match the manifest (<n> added to it); next on <date>`, or
  `integrity scan not due: next on <date>`. A manifest that is unreadable, or missing once a scan has run, prints
  `backup manifest WARNING` and makes the scan due that night even when it is not: a scan failure on
  `.backup_manifest.json` (its files are re-adopted with a self-check only, so the run exits 1 once and the next scan
  passes). A manifest update that fails (a locked file) prints `backup manifest WARNING` (`off-machine manifest
  WARNING` for the off-machine record) and never fails a backup, mirror or off-machine step. Cost on the PC
  (2026-10-16): 12.8 GB, 3,382 files, 80 s. The off-machine
  folder is never hashed or walked: walking the Drive folder for sizes alone took about 20 minutes.
- **Folder sizes (#309 part 3)** — the run ends with `backup sizes <iso>: local <n> bytes, off-machine <n> bytes`
  (the off-machine part is left out when no folder is set or no copy was recorded yet) and writes the same facts, with the file
  counts and the `.duckdb` / `edgar_docs` zip split, to `.backup_sizes.json`. The local size is a `stat` walk of the
  local backup folder (seconds). The off-machine size is what the job itself wrote and kept there, recorded at copy
  time in `.offmachine_manifest.json` in the LOCAL backup folder, so neither the job nor the dashboard lists that
  folder. The hand-made checkpoints folder is not recorded and shows as unknown. The RC-Store-Backup Automations row
  and the Backups card of Settings / Data health show both sizes, the last self-check and its next due day, and
  warn when a folder is above `RAW_STORE_BACKUP_WARN_GB` (default 20) locally or
  `RAW_STORE_BACKUP_MIRROR_WARN_GB` (default 320, 80% of the 400 GB Google quota) off-machine (GB = 10^9 bytes; set
  either in `.env`).
- **Restore** — `python -m RevenueChaser.scripts.restore_store --list`, then
  `... restore_store --latest raw` (or `edgar`, `history`, `prices`, or a snapshot file name). Refuses
  while a RevenueChaser process uses the store, verifies the snapshot, keeps the
  current file as `<name>.duckdb.pre-restore`. `edgar_docs/` is restored by
  unzipping the chosen archive over the folder; the SPX tree by copying
  `spx_gamma_mirror/` back over `module_spx_gamma/data` (stop the dashboard first; an earlier version of
  `brier.json` or the GEX history comes from `spx_gamma_versions/`). A weekly copy is restored with
  `... restore_store --from <backup dir>\weekly --latest raw`. From the off-machine folder use the same
  commands with `--from <RAW_STORE_BACKUP_MIRROR folder>\RevenueChaser-backup` (its newest `raw_*` is at most a week
  old; a `weekly` sub-folder there holds only what earlier versions sent); its
  mirror folders are copied back like the local ones.
- **Compaction** — DuckDB re-uses freed blocks but never shrinks the file, so
  every `replace_all`/DELETE leaves it larger than its data. With the dashboard
  and scheduled jobs stopped: `python -m RevenueChaser.scripts.compact_store`
  (applies the manifest first, then `COPY FROM DATABASE` into a fresh file,
  verifies row counts, swaps; the old file stays as `<name>.duckdb.pre-compact`
  until you delete it). On the live store: raw 591 → 429 MB, edgar 507 → 81 MB.
