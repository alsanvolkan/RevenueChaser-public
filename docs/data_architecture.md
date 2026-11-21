# Data Architecture — RevenueChaser

> **#860, 15 Nov 2026: the old store files are gone.** `.store/raw.duckdb`, `edgar.duckdb`, `history.duckdb` and
> `.store/prices.duckdb` were removed with every code path to them, in one PR, and no data was moved across. The
> store keeps only the board files, `.store/boards/<board>.duckdb`; the boards read market data from rcdata's
> serving layer under `RC_MARKET_DIR` (its `prices.duckdb` there is a different, new file).
> `core/tests/test_no_old_store.py` fails any code that names an old file, sends a store call to one, or imports a
> module removed with them.
>
> The sections that carried the old layout were rewritten for this one (#1167): **The two data folders**,
> **Storage layout** and its **Result history** note (there is none, and nothing replaced it), the schema stamp
> under **Storage format inside the store**, **Stage 5 — Serving** and the file layout under **Concurrency
> model**. What is left to do is named where it stands: the `DB` column of the Stage 2 and Stage 3 tables is
> stale for the rows that name no `rawstore.MANIFEST` table (#1209), and a mention of one of the four files
> elsewhere below is provenance — the issue or measurement a number came from — not the current design.

This document describes the data pipeline and where data lives at each stage.
Companions: [`data_retrieval.md`](data_retrieval.md) for how data gets in and
out (Fetch vs Refresh), [`e2e_testing.md`](e2e_testing.md) for how that is
verified.

The guiding principle: **one DuckDB file per board for everything the app itself writes**
(`.store/boards/<board>.duckdb`, and `boards/watchlists.duckdb` for avo's own lists), **the market data read
from rcdata's serving layer** under `RC_MARKET_DIR`, which the builder rebuilds from Parquet, **clearable
`.cache/` for regenerable outputs, and files only where files are intrinsically better** (immutable audit
artifacts, downloaded binaries and the raw `edgar_docs/` HTML tree). The three files for raw and derived data
this paragraph used to name, and the fourth for result history, went with #860 — see the banner above.

The Discord archive was a fourth store file until October 2026. It is now its own app
(`discord-archive`), with its own database, schema version and backup, because nothing here ever
read it for analysis.

## Decision note — the needs layer: built (PR #290); filling not yet (avo, 2026-10-29, renamed 2026-11-01)

The bridge between the dashboards and the store is **the needs layer**, `rcdata/needs/`: a board asks for a
named need with `needs.ask(need)`, and the layer serves it from the store with its freshness, coverage and
provenance. It was built in PR #290 (the catalog, the `ask()` envelope, `summary`, the board declarations and the
Settings / Data screen), and renamed from `rcdata.api` / `api.get` in #329 (`GET /needs`,
`GET /needs/<need>`; the old `/api/data` routes are gone). **What is not built is the filling:** `ask()`
accepts only `fill="never"`, so a missing or stale need is reported, never fetched, and nothing enqueues a
fill on demand yet. The order avo set still holds for that part: finish the setup, purge the data, run the
backfill, then add the on-demand fill, so its final shape is settled by real data rather than guessed. The
design it follows is `/mnt/project-files/earnings_bridge/2026-10-29_internal_data_api.md`; earnings dates
are its first use case, not the whole design. Its locking follows the concurrency model below rather
than adding a scheme of its own: a file lock (`core/job_lock.py`) where liveness matters, an
`ops.sqlite` row under `BEGIN IMMEDIATE` for shared state and for collapsing concurrent fill
requests, the existing `Pacer` and `DAY_CAPS` for provider rate and budget, and no leases while
homesrv is the only runtime (avo asked for mutex and semaphore to be worked out; that section of the
design file holds it).

**What a purge loses for good.** The backfill cannot bring these back, so the purge decision has to
cover them first:

- the SPX Gamma raw days (`spx_raw_days` and `spx_raw_files` in `.store/boards/spx_gamma.duckdb`; the old tree
  `<store>/spx_gamma/raw/<date>/` until it is imported): UW serves only recent days (see Storage layout);
- the UW options data observed inside its window (`option_strike`, `option_expiry`, `option_print`,
  the UW rows of `iv_daily`, `er_options`): a past evening cannot be observed again, and the raw
  retention hook already removes the older `option_list/` and `er_options/` raw folders;
- `ibkr_shortable`: IBKR's public short-stock file is one snapshot per read, with no history served;
- the weekly analyst-estimate snapshots behind `estimate_latest`: a past week's snapshot cannot be
  asked for again;
- `fundamental_revision`: it is the difference between two consecutive monthly Compustat snapshots,
  so without the earlier snapshot the revision cannot be recomputed (unless WRDS serves that
  publication date for the library in question);
- `forecast_ledger` (in `boards/forecast.duckdb`): our own forecasts, which no provider ever served. The
  result history that stood beside it here went with `history.duckdb` (#860); nothing records a board run's
  rows any more — see **Result history** under Storage layout;
- the Drive mirrors (see Backups): they cannot be fetched again either.

## The two data folders — read this first (for future maintainers & AI)

| Folder | Semantics | May you delete it? |
|---|---|---|
| **`.store/`** | **DURABLE.** Everything in `.store` is the source of truth. It holds the **board files** and nothing else — `.store/boards/<board>.duckdb`, one per board, plus `boards/watchlists.duckdb` (avo's own lists), and the tiny `.rc-proc-<pid>` presence markers and `*.lock` job locks beside them (see Concurrency model). The four files this row used to list — `raw.duckdb`, `edgar.duckdb`, `history.duckdb` and `.store/prices.duckdb` — are gone (#860): the market data the boards read is rcdata's serving layer under `RC_MARKET_DIR` (see The market store), so `.store` keeps only what the app itself writes. Raw 10-K/20-F HTML is the one file-based durable artifact, under `edgar_docs/{cik}/{accession}.html.gz`. The board files grow over time and CANNOT be regenerated from the app — losing them loses the watch lists, which nothing can rebuild, and what only accumulates (one Rotation snapshot per date, one Co-movement run per as-of date, the forecast ledger) — and the raw filings. | **NO.** Treat like database files. Back them up (`rcdata backup`, the weekly job: the serving DuckDBs, `boards/*.duckdb`, the ops state, the UW raw files and the SPX Gamma tree, and the `edgar_docs/` tree as a zip; `watchlists.duckdb` also gets a daily copy); never `rm -rf`. |
| **`.cache/`** | **ARTIFACTS.** Fully regenerable outputs: `*_latest.html` dashboards, `*_latest.json` payloads, PNGs, timestamped archives, and downloaded SEC 13F source zips. | **YES.** Delete any time; re-running the relevant module regenerates everything. |

Rule of thumb: if deleting it loses *history* or *ingested* data → it belongs in
`.store/`. If deleting it only costs a re-run → it belongs in `.cache/`.

### Test and harness isolation

Both roots are **relocatable by environment variable**, read once in
`core/config.py`:

| Env var | Overrides | Default |
|---|---|---|
| `RC_STORE_DIR` | `DATA_DIR`, and therefore `rawstore.STORE_DIR` and every board file under `<store>/boards/` | `<repo>/.store` |
| `RC_CACHE_DIR` | `OUTPUT_DIR` / `CACHE_ROOT` | `<repo>/.cache` |
| `RC_EDGAR_DOCS_DIR` | `EDGAR_DOCS_DIR` (raw 10-K/20-F HTML tree) | `<repo>/edgar_docs` |
| `RC_MARKET_DIR` | `MARKET_DIR`: rcdata's serving DuckDB files and `ops.sqlite` — what the boards read (see The market store) | `<repo>/.market` |
| `RC_MARKET_RAW_DIR` | `MARKET_RAW_DIR`: the builder's raw downloads and Parquet, which are fetched again rather than backed up | `<repo>/.market_raw` |

They are read **at import**, and that is deliberate rather than incidental. Some
twenty module-level constants derive from these roots — `app/runners.py`'s
`*_LATEST`, `core/rawstore.py`'s `STORE_DIR`, every module config's paths — and
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

Schema version `13` (`_SCHEMA_VERSION` in `core/rawstore.py`; v3 added
`industry_group` / `sub_industry`, and `equity_universe` also gained
`industry`; v6 added the identity master tables `security_master`,
`identity_conflicts`, `identity_source_cache` and `identity_runs`, issue #85, and `board_settings`, issue #40; v7 added
`relatedness_profiles.attempts`, issue #114; v8 added the `job_runs` table, issue #124 PL5 (it went with `raw.duckdb`; the run log is `job_runs2` in the ops store since #1205, see Jobs below),
`edgar_financials.fetched_at`, issue #161, the `price_bars_daily` table, issue #156, `catalyst_events`, issue #3,
`orphan_designations` / `orphan_lookups`, issue #190, and the shared `forecast_ledger`, issue #128 —
manifest only, no writers yet; #164; v9 added the four SIC columns of `security_master`, issue #272; v10 added the 22 G1 tables of #292 (manifest only, no writers yet); v11 was the history redesign C: 8 `<table>_changes` tables in `raw.duckdb` and `result_runs` plus 16 run-keyed row tables in `history.duckdb` (`core/history.record_run`) — **all of them gone with those files (#860)**, and none replaced; what is left of each is below (change history: the board files' own `history_mode`; result history: none, see Storage layout) — the key rebuild of `price_bars_daily` (`date` becomes `session_date`) and `orphan_lookups` (key `(ticker, session_date)`), and new columns on `theme_runs` and `leaps_flow_prints`; v12 expands the 13F ingest store to non-lossy INFOTABLE rows, adds COVERPAGE/SUMMARYPAGE fields to `sec13f_submissions`, makes `sec13f_holdings` keyed on `(accession, line_no)` and adds the derive-side VALUE-units rescale (#208); v13 adds the nullable `relatedness_profiles.profile_json` / `label_source` (the Relatedness v2 profile, #401) and the `relatedness_embeddings` table (manifest only, its writer is Step 2)). Adding manifest columns auto-migrates via
`ALTER TABLE ... ADD COLUMN`; the whole manifest (renames, merges, views,
column additions, compressed-column rebuilds) is applied on each process's
first store call — see "Schema on first use" below. `equity_universe.next_earnings_date TEXT` (Phase 1,
#265) is such a column, and so are the six FIGI columns of `security_master` (#267, no bump), its four SIC
columns (#272, the v9 bump), its four identifier columns (`cusip`, `cusip_source`, `isin`, `isin_source`, #127, no bump) and `fetched_at DOUBLE` on `chain_meta`, `leaps_cache`, `rotation_holdings`, `sec13f_security_meta` and `ticker_themes` (#127, no bump; NULL on rows written before the column reads as unknown, never as age 0): they are added by that `ALTER TABLE` on the first store call; the version is a label
written to `migration_manifest`, and the bump is what makes the change a gated one (manual `.store` checkpoint, see
below).

Each store file carries its own schema stamp (#26 PL7-a): a `store_schema` row, key `schema_version`, equal to `_SCHEMA_VERSION`. The ensure path writes it when the row is absent or older than the code, and leaves a newer stamp as stored; an upgrade of an older stamp is a warning and keeps the old value in the same table (`upgraded_from`, `upgraded_at`), so a restored older file leaves a trace. `rawstore.store_schema_mismatch()` lists those (`db`, `file`, `stored`, `code`); a read logs a warning and does not raise. An existing file with no stamp is stamped with the current version on that first ensure — an already-wrong file would be marked current; `schema_drift()` remains the backstop for a retyped column, a changed key or an extra column. The table is additive (`CREATE TABLE` plus one row) in each of the board files (`rawstore._SCHEMA_FILES`, which is `BOARD_DBS`); a board file is made by the schema apply and carries its stamp from birth. It does not bump `_SCHEMA_VERSION` and does not need a Drive checkpoint.

Refusing writes on a mismatch is off (#26 PL7-b). `SCHEMA_REFUSE_WRITES` in `core/store_schema_refuse.py` is `False`. Set `RC_STORE_SCHEMA_REFUSE=1` to refuse a data write when that file's stamp is ahead of this code: newer, or not a version number. A file with no stamp is not refused: the ensure stamps it current. The same check runs before the ensure, so an old checkout never renames, merges or ALTERs a newer file; the ensure then logs one warning and the writes are refused. A refused write is also a skipped `job_runs` row (the run log goes quiet while jobs fail), so read the Server log first. `store_schema_mismatch()` opens the files read-write, so run the gate check with the dashboard stopped or on the fresh backup copy. Readers still only warn. `RC_STORE_SCHEMA_OK=1` is the escape for a deliberate migration: writes are allowed and the warning is suppressed. The exception is `StoreSchemaMismatch`; it names the file, the stored version, the code version and `python -m RevenueChaser.rcdata restore --step boards --latest <board> --live`. It does not run at import. Leave the switch off unless that refusal is intended.

## Storage layout

| Path | Contents | Clearable? |
|---|---|---|
| `.store/boards/<board>.duckdb` | **The whole durable store (#860).** A board's own tables, written only by that board's package, incrementally (DECISIONS S9, S16); a MANIFEST table with `"db": "<board>"` lives only there. The board files today (`rawstore.BOARD_DBS`): `news`, `welcome`, `relatedness`, `comovement`, `asymmetric_growth`, `swing_potential`, `fundamentals`, `sector_rotation`, `oipd`, `forecast`, `spx_gamma`, `sec_13f`, `watchlists`, `value_chain`. Each is made by the schema apply with its own `store_schema` stamp, and the writer's presence marker sits beside it. One file here is not a board result: `boards/watchlists.duckdb` (#786) holds avo's own watch lists, typed by hand and not regenerable from any source. It sits here for the one writer, the peer lock, the schema stamp and the `boards` backup step, and for nothing else: it is never rebuilt and never written whole | No — durable, and `watchlists.duckdb` is the one that cannot be regenerated |
| `edgar_docs/{cik}/{accession}.html.gz` | Raw 10-K/20-F HTML (the durable raw-document record; keyed by CIK + accession). Dir via `RC_EDGAR_DOCS_DIR` | No — durable; backed up as a tree |
| `.cache/sec_13f/*_form13f.zip` | Downloaded SEC 13F source archives, left by the retired dashboard ingest (G9); the builder keeps its own under `raw/sec/13f_sets/` | Yes — nothing reads them |
| `.cache/` | All regenerable outputs: `*_latest.json`, `*_latest.html` (flat and nested), PNGs, timestamped archives | Yes — delete anytime; re-run regenerates |
| `<RC_STORE_DIR>/spx_gamma/` (override `SPX_GAMMA_DATA_ROOT`; `module_spx_gamma/data/` until S4, #38) | The SPX Gamma board's old data tree: the UW files per date under `raw/<date>/` with their `manifest.json`, `brier.json`, `spx_gamma_gex_history.json` and `latest.json`. Its durable part now lives in `.store/boards/spx_gamma.duckdb` (the five `spx_*` tables, written only by `module_spx_gamma/store.py`; SPX Gamma review 9 Nov 2026, D1-D5), and the tree is that file's import source: `python -m RevenueChaser.module_spx_gamma --import-tree [--apply] [--legacy PATH] [--extra-gex FILE ...]` (dashboard stopped) reads it in, a dry run by default, idempotent by key, every file's sha256 checked against its manifest, deleting and moving nothing; with `--apply` it then rebuilds the parsed files, charts and reports of every stored day. The board's regenerable files (a Fetch's `staging/` folder, parsed files, charts, reports, the history pull's `history_pull/` run files) go to `SPX_GAMMA_DATA_ROOT` when set, else `.cache/spx_gamma/` (`config.DATA_ROOT`); `config.TREE_ROOT` is the tree. The tree stays until avo's word after the board file's first verified weekly backup (PR 4) | No — durable until imported and the board file is backed up. Copied by the weekly uw-raw step until PR 4 |

### Result history

**There is none, and nothing replaced it.** `history.duckdb` held `result_runs` (one header row per board run)
and 17 run-keyed row tables that mirrored what each board had just shown; `core/history.record_run` and every
`module_*/history.py` wrote them. The file and all of that code went with #860, so a board's own file now holds
only its **current** rows, and a past run's rows cannot be read back. Two capabilities went with it, and are
named here rather than described as if they still worked:

- **the per-run record of what a board showed** — the Financials board's "render the newest stored run" Refresh
  (#385 FIN-4) and the Rotation / Money Flow "keep the newest complete session" fallbacks read it;
- **the per-run provider-health row** (`result_runs.provider_health`, PH-1).

What remains is the **job run log**: one row per run of every writer in `job_runs2` in the ops store, read by the
**Runs** tab (see Jobs). It records that a job ran, its steps and its outcome — not the rows it produced.

## Pipeline stages

### Stage 1 — External acquisition (network)
Providers pull from Unusual Whales, yfinance, SEC EDGAR (XBRL companyfacts, 13F
ZIPs), FRED, OpenFIGI. Nothing is persisted at this
stage except caches (Stage 2).

> **Every row of the next two tables names a table or a view that exists (#1209).** The `DB` column names the
> file it lives in: a board file for a board's own table, or a serving kind (`spine`, `prices`, `fundamentals`,
> `ownership`, `market`) for a row rcdata serves. A row marked `(VIEW)` is a **bridge view** rcdata creates under
> an old `raw.duckdb` name on purpose (decision D4), so a reader written against the old store keeps working; each
> keeps the old column list, with NULL of the old type where the new source has no value. The caches of the four
> files #860 deleted are not listed one by one any more — the `*(removed)*` and `*(retired with the old files)*`
> rows say where their data went instead. `core/tests/test_doc_dataset_tables.py` holds every row against the code:
> a row may name a `rawstore.MANIFEST` table, a serving table (`rcdata/read.DATASET_OF_TABLE`) or a bridge view,
> and nothing else.

### Stage 2 — Raw caches (written at fetch time)
Cached in the board file of whichever board fetched them (the `DB` column below names it), so re-scans reuse
data instead of re-hitting rate-limited APIs. Each row carries a `fetched_at` timestamp; readers apply a TTL and
refetch on expiry. `no_fetch=True` (model-only mode) reads regardless of age and never touches the network. The
caches that lived in `raw.duckdb` and `edgar.duckdb` went with those files (#860); what a board still needs from
them it reads from rcdata's serving layer instead (see The market store).

| Table | Content | DB | TTL |
|---|---|---|---|
| *(removed)* | The caches of the deleted files, each named in `rawstore.RETIRED_TABLES` with the issue that took its last reader: `ticker_payload_cache`, `swing_ohlc`, `quote_snapshot`, `vendor_valuation`, `sec_company_tickers`, `sec_company_sic`, `sec_exchange_file`, `sec_mf_file`, `sec_submissions_cik`, `sec13f_enrichment`, `sec13f_ticker_names`, `cusip_ticker`, `identity_source_cache`, `leaps_cache` and `leaps_flow_prints`. Their writers went with them (`core/quotes.py`, `core/sec_files.py`, `core/sec_submissions.py`, `core/vendor_data.py` and the rest of `test_no_old_store.REMOVED_MODULES`) | — | — |
| *(retired with the old files)* | The Stage 2 rows #1209 dropped, with what holds the data now. **Gone with no live home:** `catalyst_events`, `orphan_designations` / `orphan_lookups` (the Biotech board was never built, so nothing ever wrote them), `tradewhisperer_*`, `sec13f_edgar_backfill`, `sec13f_filers` (manifest only, never written; the ownership store's `sec13f_filings` is the live record), `source_checks` and `price_checks` (the builder keeps its own per-dataset state in ops, `dataset_check`). **Served elsewhere now:** `edgar_documents` → the gzipped filings under `EDGAR_DOCS_DIR` (`core/edgar_docs.py`) with `filing_item` / `filing_section` for their metadata; `sec13f_security_meta` and `identity_cusip` → the spine's `security` (and `sec13f_cusip_map` for the 13F CUSIP map); `etf_holdings` → the spine's `etf_holdings` and `etf_holdings_nport`, the whole list per fund rather than a top 10; `watchlist` → `wl_list` / `wl_item` in `watchlists.duckdb` and the stock watch list in ops (D5); `earnings_history` → `earnings_reported` / `earnings_report_history`; `macro_series`, `vol_index_daily` and `cftc_cot` → the market store's `market_series` (mapped by name in `rcdata/needs/screen.RETIRING`); `economic_calendar` → the market store's `economic_event` (`uw/economic_calendar`, hourly; retired 11 Nov 2026); `ownership_snapshot` → the prices store's `shares_class` for shares outstanding, while the ownership store keeps the filings themselves (`sec13f_ticker_period`, `beneficial_ownership`, `insider_transactions`) instead of Yahoo's two percentages; `vendor_financials` → nothing: the Financials board reads the SEC's own facts (`fundamental_fact`), so a vendor-only card has nothing to render (#860) | — | — |
| `edgar_financials` (VIEW) | **A bridge view (D4), not a cache.** The old companyfacts row per ticker, served by `fundamentals.duckdb` over `fundamental_fact` and `fundamental_company` (`rcdata/build/fundamentals/tables._financial_view`): `annual` and `quarterly` as JSON record lists in the shape `sec_edgar.extract_*` wrote, with `period_ends` / `filed` / `accn` maps, plus `meta`; a field with no value has no key, and a preliminary row (no accession) is left out. The board's own reads go through `core/fundamentals_layer.py`, not this view | fundamentals | — |
| `price_bars_daily` (VIEW) | **A bridge view (D4).** Daily bars per `(ticker, session_date)` in the old column shape, served by `prices.duckdb` over `price_bar_daily` joined to `security_symbol_now`, with the split factor applied to OHLC and volume and the total-return factor to `adj_close` (`adj_source` `prices/adj_factor`); index rows are left out, and `split_ratio` is NULL. `fetched_at` and `run_at` are the serving file's `built_at`, so they age with the build, not with a fetch. Readers (windowed by SQL, K8a): Swing and Fundamentals (#383 R3), Asymmetric Growth's momentum (#380 S4), the forecast outcomes, Co-movement and the Relatedness peer market (`module_comovement/ohlc.py` `price_frame`, #379 D01), and the Earnings Fetch's reaction window (`module_earnings_radar/stored_inputs.ohlc_bars`, #372 E5e) | prices | — |
| `rotation_holdings` | Per-ETF holdings + RS rows for today. `fetched_at DOUBLE` (#127); NULL on older rows is unknown, never 0 | sector_rotation | same-day |
| `equity_universe` (VIEW) | **A bridge view (D4).** The shared US universe in the old column shape, served by `spine.duckdb` over `security` (tiers A and B) with the old membership rule (#264); market cap, price and the quote are NULL of their old type, because they are market data and not identity, and the type filter stays with the reader. The boards' universe is `core/universe.py` over the spine — `RC_UNIVERSE_SOURCE` and the old table's path are gone (#642 PR 6) | spine | — |
| `comov_runs` / `comov_nodes` / `comov_members` / `comov_lag` | Co-movement board: dated return-cluster runs, clusters, members; groups formed a quarter earlier with their held-out-quarter test | comovement | per Fetch run (latest as_of wins); each stored run deletes runs older than 14 days (`config.KEEP_DAYS`), keeping each config hash's newest (review v2 N2) |
| `chain_filings` / `chain_mentions` / `chain_meta` | Value Chain board: latest 10-K/20-F Item 1/4 text + supplier/customer edges. `chain_meta.fetched_at DOUBLE` (#127) is the fetch time; NULL on older rows is unknown, never 0 | value_chain | per fetch (resumable by ticker) |
| `llm_cache` | DeepSeek and EVREN prompt/response cache (Relatedness + Value Chain) | relatedness | none (immutable) |
| `ticker_themes` | Runtime theme SSoT `(ticker, theme_id, theme, level, theme_run_id)`. `fetched_at DOUBLE` (#127) is when the row was written; NULL on older rows is unknown, never 0 | relatedness | per theme run |
| `price_actions` (VIEW) | **A bridge view (D4).** Dividends and splits per `(ticker, ex_date, action)` in the old column shape, served by `prices.duckdb` over `corporate_action` (applied rows only): a cash dividend carries its amount scaled by the split factor, a split its ratio (4.0 = 4-for-1). Read with the bars when a reader adjusts on read (`core/prices.daily_bars`) | prices | — |
| `index_levels_daily` (VIEW) | **A bridge view (D4).** `^GSPC` daily levels, served by `prices.duckdb` over `price_bar_daily` where `security_id = 'index:SPX'`. SPX Gamma read its 8-week candle window from the old table and no longer does (it reads Cboe's `SPX` closes from the market store's `market_series`, `module_spx_gamma/store_reads.py`, D03); the Settings / Data health board reports newest session against latest complete, the row count and the source | prices | — |
| `short_interest` (VIEW) | **A bridge view (D4).** FINRA short interest per `(ticker, settlement_date)` in the old column shape, served by `ownership.duckdb` over `finra_short_interest`, a settlement date visible from the eighth business day after it (`FINRA_VISIBLE_DAY`, #252, which read it a week early). Our own readers take the table and not the view (`rcdata/read.finra_short_interest`, review N3) through the `ownership.short_interest` need: 13F Short% (`module_sec_13f/enrich.py`) and Earnings R5 (`module_earnings_radar/stored_inputs.short_rows`) | ownership | — |
| `estimate_snapshots` (VIEW) | **A bridge view (D4).** The newest analyst estimate per ticker and period in the old D14 shape, served by `fundamentals.duckdb` over `estimate_latest`, with `source_as_of` as `session_date`; the serving layer maps the old name onto `estimate_latest` as well (`rcdata/needs/screen.RETIRING`). Earnings R1 reads `estimate_latest` itself (`module_earnings_radar/store_reads.estimates`, the `0q` row while its `source_as_of` is at most `MAX_AGE_DAYS` 14 days old, the need `fundamentals.estimates`; the rule is logged only, weight 0). The source is narrower than the old nightly job: the build asks Tier A companies only, one ticker per company (the shortest), so a calendar name outside Tier A, or the other class of a two-class company, has no R1 row | fundamentals | — |
| `earnings_calendar` (VIEW) | **A bridge view (D4).** The old UW-calendar shape, one row per ticker and report, served by `fundamentals.duckdb` over `earnings_event` joined to `fundamental_company`; what the layer does not hold is NULL (`marketcap`, `expected_move_perc`, the two closes, `item`), and `report_time` is mapped onto the old `premarket` / `afterhours` wording. The rows come from the Alpha Vantage calendar dataset (`alphavantage/earnings_calendar`, weekly since #1249); a board asks for them through the `earnings.next` need | fundamentals | — |
| `news_events` | News board (S9, S16): its own file `.store/boards/news.duckdb`. Writer `module_news/calendar.build`, incremental from `market.economic_event` rows fetched after the `news_meta` mark `calendar_mark`, and since #807 from `market.fda_event` after the mark `fda_mark` (`type` 'fda', one row a catalyst, `event_ts` its target date at midnight Amsterdam, the real precision in `target_precision`; an FDA row is never a day's highlight): upsert by key (first sighting kept, last sighting, forecast and previous value move forward); a key the newest fetch no longer lists inside that fetch's span gets `retired_at`, never a delete; no new rows retire nothing. `importance` from `core/econ_importance.py` (3 high, 2 medium, 1 low). Key `(event_ts, event, type)`. `event_ts DOUBLE`, `event`, `type`, `importance INTEGER`, `forecast`, `prev`, `reported_period`, `first_seen DOUBLE`, `last_seen DOUBLE`, `retired_at DOUBLE`, `run_id`, `run_at DOUBLE`, and the FDA lane's own `ticker`, `drug`, `catalyst`, `target_text`, `target_precision`, `indication`, `status`, `outcome`, `url` (NULL on an economic row) | news | none |
| `news_earnings` | News board (#886): next week's report day of every name on the News watch lists (`module_news/headlines.LISTS`), from `fundamentals.earnings_event` (the Alpha Vantage calendar). Writer `module_news/earnings.build`, every Sunday 09:00 Amsterdam (`RC-News-Earnings-Weekly`) and on the board's own Fetch: it drops every other week first, so the table is the one week the board shows. The pass's own time is the `news_meta` key `earnings_refresh`. One row a name; a confirmed report beats a calendar guess. Key `(week_start, ticker)`. `week_start DATE`, `ticker`, `company`, `report_date DATE`, `report_time`, `confirmed BOOLEAN`, `source`, `run_id`, `run_at DOUBLE` | news | none |
| `news_highlight` | News board: the highlighted release of each Amsterdam day, its most important live `news_events` row (earliest on a tie), with a Turkish template text (`text_source` 'template', no model call). Key `session_date`. `session_date DATE`, `event_ts DOUBLE`, `event`, `type`, `importance INTEGER`, `forecast`, `prev`, `text`, `text_source`, `run_id`, `run_at DOUBLE` | news | none |
| `news_headlines` | News board: Dow Jones headlines (`DJ-N`, `DJ-RT`) from the IBKR Gateway, `module_news/headlines.py` (avo, 9 Nov 2026: "Ibkr kullanalim"): per name of avo's own watch lists and the `stock` and `news` lists (`jobs.stock`, `jobs.news` in `watchlists.duckdb`; avo, 18 Nov 2026: "Hepsini watchliste.duckdb ye bagla"), every name, headlines only, never the article body; 30 names a 5-minute pass in turn after the `news_meta` cursor `headlines_cursor` (each name about hourly, inside IBKR's pacing), each name's headlines of the last 24 h, writing only a story not stored yet or met under a new name; the page's ticker box (job `news_ticker`, client 75) asks one name on a click for its newest 300 headlines of the last 7 days from `DJ-N`, `DJ-RT`, `BRFG` and `BRFUPDN` (`source` Dow Jones or Briefing), stored the same way, its answer kept in `news_meta` as `ticker_ask:<TICKER>`; the same story under several names is one row whose `tickers` lists them. Also the X lane, whose one source is RC's own X reader (`module_news/x_reader.py`, job `news_x_follow`, plan step 5; avo 18 Nov 2026: "sen kendi islemcini kur twitter icin"): `provider` `x-follow` rows from the Following timeline of RC's X account (twifork, cookies `RC_X_AUTH_TOKEN`/`RC_X_CT0` from `/etc/life/secrets.env`): every 30 minutes in the US session, every 4 hours otherwise, at most 5 pages a pass, newest status id and last run in `news_meta` `x_follow_state`; an id already stored is never written again, so its `x_check` survives; `kind` is `option` or `news`. Each row is `headline_id` `x:<status id>`, `published_at` the status id's own time, and for an option post `x_check` (JSON: the contract and what UW's prints of it show around the post, then the next day's open-interest change; `module_news/x_check.py`, avo 11 Nov 2026: "we need to show that option flow from our sources"). avo's Grok "X saatlik özet" mails were the lane's other source (`provider` `grok-x`, fetcher `module_news/x_feed.py`, mark `x_mark`) until 21 Nov 2026, when he had everything Grok removed: the fetcher, the loader, the saved mails and those rows are gone. One row per headline id, upserted. Key `headline_id`. `headline_id`, `provider`, `published_at DOUBLE`, `headline`, `source`, `tickers` (JSON text), `url`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`, `kind`, `option`, `x_check` | news | none |
| `news_meta` | News board bookkeeping: the incremental marks (`calendar_mark` = the newest `raw.economic_calendar.fetched_at` already built). Key `key`. `key`, `value`, `run_at DOUBLE` | news | none |
| `news_reading` | News board (#1234, avo 21 Nov 2026 decision A): the EVREN model's reading of the headline flow, one pass an hour. Writer `module_news/reading.py`, queued hourly at :20 by `RC-News-Reading-Hourly` through the dashboard (`POST /run/news_reading`, the one writer). The window is the clock hour that has just finished, so a second pass of the same hour upserts its own rows. One row per `(window_end, field)`, `field` one of `module_news.reading.FIELDS` (tone, driver, macro, rates, most_named, surprise, watch): a field is a row and not a column so that its source sentence cannot be optional — every row carries `quote`, the words of the headline `headline_id` names, and a field whose quote does not occur in that headline is dropped, never stored. One call an hour, no fee (the writer is the `news.reading` LLM job, EVREN by default); an hour with no stored headline asks nothing, and a paid writer stores nothing unless the run was started with `paid=1`. Read back by `GET /api/news/reading` and the MCP tool `stored_reading`. Key `(window_end, field)`. `window_end DOUBLE`, `field`, `window_start DOUBLE`, `value`, `quote`, `headline_id`, `source`, `published_at DOUBLE`, `provider`, `model`, `prompt_version`, `headlines INTEGER`, `run_id`, `run_at DOUBLE` | news | none |
| `welcome_snapshot` | Welcome board (S9, S16): its own file `.store/boards/welcome.duckdb`. Writer `module_welcome/build.py`: the last stored close of SPX (`index_levels_daily` `^GSPC`), VIX and MOVE (`vol_index_daily`, newest fetch of a day), US 10-year (`macro_series` `DGS10`), with the change on the session before and a 20-session line (`spark`, JSON text); a row only for a session newer than the series' stored one. Key `(series, session_date)`. `series`, `session_date DATE`, `close DOUBLE`, `prev_close DOUBLE`, `change DOUBLE`, `change_pct DOUBLE`, `spark`, `run_id`, `run_at DOUBLE` | welcome | none |
| `welcome_earnings` | Welcome board: the names reporting today and on the next trading day (New York dates), from the newest built session of the options builder's `option_screener_daily` (needs `earnings.next` / `options.screener`, the name from `identity.security` where the row carries none); a name that session drops gets `retired_at`, never a delete. Key `(earnings_date, ticker, report_session)`. `earnings_date DATE`, `ticker`, `report_session`, `full_name`, `expected_move_perc DOUBLE`, `report_time`, `source_session DATE`, `first_seen DOUBLE`, `last_seen DOUBLE`, `retired_at DOUBLE`, `run_id`, `run_at DOUBLE` | welcome | none |
| `welcome_freshness` | Welcome board: **one row per builder file** (avo's D2 of 10 Nov 2026), rolled up from the builder's own run ledger `job_runs2` in ops: its last finished run, last success and last failure. Key `job`, which holds the serving kind (`spine`, `prices`, `fundamentals`, `ownership`, `options`, `market`, `sec_forms`, `sec_reads`), named on the card by `module_welcome.build.FILE_LABELS`. `job`, `last_status`, `last_finished_at DOUBLE`, `last_ok_at DOUBLE`, `last_failed_at DOUBLE`, `last_error`, `run_id`, `run_at DOUBLE` | welcome | none |
| `welcome_meta` | Welcome board bookkeeping, designed for the incremental marks `earnings_mark` and `freshness_mark` — **nothing writes it** (#643). Each of the board's three parts compares the rows it already holds instead, which converges where a drifted mark would not; `module_welcome/build`'s dead `_meta` / `_set_meta` helpers went with #643, and `core/tests/test_db_persistence.UNWRITTEN` names the table. The table is left in place rather than dropped, because dropping a board table is a store change with its own backup and rollback. Key `key`. `key`, `value`, `run_at DOUBLE` | welcome | none |
| `spx_raw_days` | SPX Gamma board (review 9 Nov 2026, D1): its own file `.store/boards/spx_gamma.duckdb`, written only by `module_spx_gamma/store.py`. One row per stored UW day, inserted with its `spx_raw_files` rows in one transaction; a complete day is replaced only by a forced Fetch. UW serves no history, so a row cannot be fetched again. Key `session_date`. `session_date DATE`, `requested_date`, `api_snapshot_time`, `origin` (fetch / history_pull), `spot DOUBLE`, `zero_dte_expiry`, `file_count INTEGER`, `manifest BLOB` (zlib JSON, the day's whole manifest), `stored_at DOUBLE` | spx_gamma | none |
| `spx_raw_files` | SPX Gamma board: every UW answer of a stored day, byte-exact (zlib text) with its sha256. Key `(session_date, file)`. `session_date DATE`, `file`, `sha256`, `bytes INTEGER`, `body BLOB`, `stored_at DOUBLE` | spx_gamma | none |
| `spx_brier` | SPX Gamma board: the scenario-probability records (Fetch only); a scored row is never changed (SPX-4); average and n are a query, not stored. Key `asof_date`. `asof_date DATE`, `scenario_probs JSON`, `outcome`, `brier DOUBLE`, `rule`, `target`, `bands JSON`, `resolved_on`, `resolved_close DOUBLE`, `updated_at DOUBLE` | spx_gamma | none |
| `spx_gex_daily` | SPX Gamma board: the daily GEX record behind the calendar colours and the deltas, every day kept (D2, no 10-day prune), written by a Fetch only (F2). Key `session_date`. `session_date DATE`, `is_friday_close BOOLEAN`, `spot`, `net_dex`, `net_gex_oi`, `net_vanna`, `live_charm`, `vanna_at_spot`, `charm_live_sum`, `matrix_gex`, `vix` (DOUBLE), `source_chip`, `regime`, `updated_at DOUBLE` | spx_gamma | none |
| `spx_meta` | SPX Gamma board bookkeeping: `latest`, the manifest of the newest day the board shows (was `latest.json`), JSON text. Key `key`. `key`, `value`, `updated_at DOUBLE` | spx_gamma | none |
| `sec13f_board_cells` | 13F board (#732): its own file `.store/boards/sec_13f.duckdb`, written only by `module_sec_13f/store.py`. Per ticker and report period the builder's metrics as filed, read by the board until the ownership build or the New York date changes; a period stored as `closed` is never rewritten. Key `(ticker, period)`. `ticker`, `period DATE`, `shares`, `value`, `holders INTEGER`, `top10_pct`, `hhi`, `new_holders INTEGER`, `exited_holders INTEGER`, `buy_flow`, `sell_flow`, `breadth`, `breadth_chg` (DOUBLE unless named), `complete BOOLEAN`, `closed BOOLEAN`, `stored_at DOUBLE` | sec_13f | none |
| `sec13f_board_breaks` | 13F board: per ticker and settled period, each filer that started or stopped reporting it (from `sec13f_filer_breaks`), kept with its period's cells. Key `(ticker, period, kind, cik)`. `ticker`, `period DATE`, `kind`, `cik`, `filer_name`, `shares DOUBLE`, `stored_at DOUBLE` | sec_13f | none |
| `sec13f_board_meta` | 13F board bookkeeping: `source`, the ownership build stamp and New York date the rows were taken from. Key `key`. `key`, `value`, `updated_at DOUBLE` | sec_13f | none |
| `wl_list` | Watch lists (#786, avo 12 Nov 2026): their own file `.store/boards/watchlists.duckdb`, written only by `module_watchlists/store.py`. Not a board result — avo types these and nothing recomputes them, so the file is never rebuilt and never written whole; it rides the `boards` step of the weekly backup. One row per list: its name, avo's free-text description, where it sits among the lists, and `feeds`, the job group it serves (`option` and `stock` for the two job lists since plan v5 step 7g, read by `rcdata.read.watched`; empty for avo's own). Lists come in and go out in TradingView's own one-line export format (`module_watchlists/tradingview.py`): items separated by commas, an item starting with `###` a section heading, everything else a symbol. `python -m RevenueChaser.module_watchlists import <file-or-folder>` reads them, a dry run by default that names every symbol with no source yet (#785); `--apply` writes and a re-run adds only what is missing. `export <list_id>` writes a list back out in the same format, order, headings and spelling kept. Key `list_id`. `list_id`, `name`, `description`, `position INTEGER`, `feeds`, `created_at DOUBLE`, `created_by`, `updated_at DOUBLE` | watchlists | none |
| `wl_section` | The ordered headings inside a list (avo's own, e.g. "AI ÇIPLERI — GPU VE ACCELERATOR"); a list with no headings carries one section with an empty name, so a reader never special-cases "no sections". Order is data, not the alphabet. Key `(list_id, section_id)`. `list_id`, `section_id`, `name`, `position INTEGER`, `created_at DOUBLE` | watchlists | none |
| `wl_item` | One symbol on one list. `symbol` is avo's own spelling kept exactly (`NASDAQ:NVDA`, `TVC:US10Y-TVC:US02Y`), because it is what an export writes back out; `exchange` and `ticker` are its parts and `coverage` says where its data comes from here — `us_equity`, `market_series`, `derived` or `unsupported` (#785), read from the spelling alone by `module_watchlists/symbols.py`. An unsupported symbol is kept and shows no data, so the list stays whole. Key `(list_id, symbol)`. `list_id`, `symbol`, `section_id`, `position INTEGER`, `exchange`, `ticker`, `coverage`, `note`, `added_at DOUBLE`, `added_by` | watchlists | none |
| `wl_change` | One row per add or remove, kept when the list itself is deleted, so what a list held is still readable. Key `(list_id, symbol, changed_at, action)`. `list_id`, `symbol`, `action`, `changed_at DOUBLE`, `changed_by`, `note` | watchlists | none |
| `sec_submissions` (VIEW) | **A bridge view (D4).** One row per filing with its 8-K items as one comma-separated text, served by `fundamentals.duckdb` over `filing_item` (`kind = 'item'`), grouped by `(cik, accession)`. Read by the Fundamentals board's corporate events (`module_fundamentals/events.corporate_events`): a card whose filer filed an 8-K carrying item 2.01, 2.06, 3.01, 4.01, 4.02 or 5.01 in the last 365 days shows those item codes — display only, never scored, ranked or frozen | fundamentals | — |

### Stage 3 — Normalized / derived stores
Computed from the raw caches above and from the serving layer, and persisted in the board files.

| Table / VIEW | Derived from | Notes |
|---|---|---|
| `forecast_ledger` | Frozen forecasts of every board (#128 / #62 D-3) | **append-only, first write wins** (nothing is ever updated; don't call `update_columns`/`replace_all`/`delete`/`clear` on it); `forecast_date` = the NY session of the newest complete bar used; `target_date` = the session the forecast resolves on (the forecast date for picks). Outcomes live in the derived `forecast_outcomes` table (62b-1, #380 S0) |
| `forecast_outcomes` | `forecast_ledger` picks + D01 bars (`price_bars_daily` / `price_actions`, nominal basis) | **derived, keyed upsert** (`latest_cache`): recomputed from the ledger and the bars, never a fact of its own. Writer `core/outcomes.py` (CLI `scripts/forecast_outcomes.py`, task RC-Forecast-Outcomes 23:45, after RC-Daily-Bars); one row per pick and horizon (30 and 90 calendar days). See "The forecast-outcomes writer" below |
| `forecast_baselines` | the dated universe of a freeze date (`my_universe_changes` in the spine, stocks only) + D01 bars (nominal outcomes, adjusted closes for the volatility) | **derived, keyed upsert** (`latest_cache`, #25 HR-1, added with no schema bump): what the same day's universe did, so a family's hit rate can be compared with something. Writer `core/baselines.py` (CLI `scripts/forecast_baselines.py`, task RC-Forecast-Baselines 23:50, after RC-Forecast-Outcomes); one row per freeze date, horizon (30 and 90) and basis (`universe`, `vol_decile_1` … `vol_decile_10`) |
| `index_constituents` (VIEW) | **A bridge view (D4), in `spine.duckdb`** over `index_membership`: the current fund-file members (`sp500`, `sp400`, `sp600`, `ndx`) and `nasdaq`, the listed Nasdaq rows (the old slug meant "listed on Nasdaq") | `dow` has no source in the spine and is absent, so a reader that needs it asks the DIA holdings; `index_tickers` is built over this view and `equity_universe` |
| `index_tickers` (VIEW) | **A bridge view (D4), in `spine.duckdb`** over `index_constituents` and `equity_universe` | adds `name` and `sector` to a slug's members; it replaced the old `ticker_registry`, and `core/rawstore.py` no longer defines it — the spine does (`rcdata/build/spine/views.py`) |
| `ticker_scores` | Asymmetric Growth cards (ex-SQLite `tickers.db`) | single writer = market-cap scan; others read-only |
| `fundamentals_scores` / `fundamentals_gates` | Fundamentals scan | typed gates `(ticker, gate, rule_id, passed, actual, unit)` |
| `swing_scores` / `swing_gates` | Swing Potential scan | same gate shape |
| `relatedness_profiles` / `relatedness_attributes` / `relatedness_scores` | Relatedness LLM run | CIK-grained profiles + similarity |
| `relatedness_embeddings` | Relatedness v2 Step 2 (#401, schema 13) | one vector per ticker and embedding model; written by `module_relatedness/embeddings.py` (#669), made again only when the embedded text changes |
| `relatedness_themes` / `relatedness_theme_tags` / `relatedness_theme_members` / `theme_runs` | NMF over Relatedness | nested fine/medium/coarse themes; pinned run in `run_registry` |
| `rotation_snapshots` | Rotation engine cross-section (up to 78 symbols live, 67 backfilled; D01 weekly closes since #374 M-ROT-2) | one row per ETF per session (time series; `as_of_date` is the session, #283) |
| `sec13f_totals` (VIEW) | **A bridge view (D4), in `ownership.duckdb`** over `sec13f_ticker_period`: per ticker the old JSON `entry`, one object per period with the eleven metrics (`rcdata/build/ownership/serve.METRICS`) | the board reads `sec13f_ticker_period` through `core/ownership.sec13f_totals`, not this view |
| *(retired with the old files)* | The Stage 3 rows #1209 dropped | `sec13f_submissions` / `sec13f_holdings` / `sec13f_cusip_names` / `sec13f_ingested_zips` went with `raw.duckdb` (#860) and had had no writer since G9; the builder keeps the filings and holdings in Parquet and serves them as `sec13f_filings`, `sec13f_filer_holdings` and `sec13f_cusip_map`, and #274 tracks what is left of the retirement. `run_registry` has no non-test reader left: the profile mode, the theme run id and the migration markers live in ops. `company_profile` was a view over `equity_universe` and the three score tables and went with the old store; `core/rawstore.py` defines no view at all now, and the name today belongs to the SEC reader's `company.profile` question (`rcdata/build/sec_reads/company_profile.py`) |

**One session date (#283, ADR-3).** `core/calendar.py` is the only place that turns a run time into a session
date; no table or board does its own `date.today()` or UTC-date cut.
- `session_for(run_at)` (rule B) gives the New York session and a quality: on a trading day from 09:30 ET it is that
  day, `intraday` before 16:30 ET and `complete` from 16:30; before 09:30, on a weekend or on a holiday it is the
  previous trading day, `complete`. A session-keyed writer stores nothing for `intraday` (first write wins on
  `complete`); a run-keyed table keeps every run.
- `to_session_date(value)` is the read helper for old run-time columns (epoch, ISO text, `date`): a value with a time
  goes through `session_for`, a date-only value becomes the latest session on or before it. Naive text is read as New
  York time. `provenance.session_of` is the never-raising wrapper for labels.
- Rule C: the holiday table runs 1993-2028 and raises outside it. 1993-2024 (#348) equals CRSP's trading dates
  (`crsp_a_stock.dsi`) exactly, every year. Sessions before 1993 come from the stored price bars, not from this
  module; the D01 / D03 fill (`core/prices.py`, `CALENDAR_START`) still keeps its pre-2025 rows on SPY's dates. Early-close days count as full days (conservative).
- Not in this slice: Rotation's weekend and holiday rows (wave 5, with #182) and migrating old tables.
- Open items, kept out of this slice so no board display changes: `provenance._day` still reads epoch as the UTC
  date; the move to `provenance.session_of` happens in wave 5 (#182, Rotation and Co-movement), because it changes
  data-date labels (a Saturday fetch would read as Friday). Early-close days (13:00 ET) stay full days on purpose
  (conservative: 13:00-16:30 reads `intraday`); revisit only if a writer needs the early-close time.

**The forecast-ledger writer (#128).** `core/ledger.py` is the only code that writes `forecast_ledger`,
through `rawstore.write_rows` alone. A board freezes its shown picks at the end of a **Fetch** (`no_fetch=False`;
a Refresh never freezes), from `app/runners.py` (`run_swing`, `run_fundamentals`, `run_asymmetric_growth`,
`run_earnings_range`) into the module's own `freeze.py`, which maps the board to ledger rows. The rule, applied by
`ledger.freeze_board`:
- the data is one complete New York session D, and D is the latest complete session (`core/calendar.py`:
  30 minutes after the 16:00 ET close; early-close days use 16:00, which is conservative);
- a run inside the US session window (09:30–16:30 ET) freezes the session before it (the prices store holds
  complete sessions only; the old refusal there lost every daytime Fetch, #25);
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
  Earnings Radar (#580): SPY's D01 bar for that session, the same anchor as AG and for the same reason — a card is
  scored from the option chain, the flow and the estimates, so one name without its own bar must not refuse the
  board. One rule is the Earnings Radar's own: **only a report still to come is frozen.** A card whose earnings date
  is on or before D is a report the market has already heard, so it is left out and the rest keep the board's order
  (`module_earnings_radar/freeze.picks`); a Fetch of past days therefore freezes nothing. The Earnings Radar has no
  Refresh, so every run of it is a Fetch. Its `score` is the number the board ranks by (confidence plus the preselect
  score) and its `bucket` is `"<final_action>|<conviction>"`, so the action carries the direction;
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
- `basis` `universe`: every stock of avo's list (`my_universe`, kind `stock`) on that date, from the **dated
  universe** `rcdata.read.my_universe_spans` (one span `entered_on` .. `left_on` per stay, read from the spine's
  append-only `my_universe_changes`; each build applied the rules to that day's spine). Never today's membership: a
  name that has since left still counts on the day it was in it. A date before the list's first build has no
  snapshot, and the rows say `n = 0`. A member the prices store holds no bar for is counted in the run's `members`,
  not in `n`;
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

The task RC-Forecast-Outcomes runs daily at 23:35, chained on RC-Daily-Bars (the bars it reads), after
RC-Screeners-Daily (weekdays from 23:30), the night's other store writer. The task RC-Forecast-Baselines
runs daily at 23:50, chained on RC-Forecast-Outcomes (the same night's other writer of
`boards/forecast.duckdb`, so the two never overlap) and writes `forecast_baselines` alone; its `--full` is the attended first fill. A dated target (an SPX pin)
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
The Flask dashboard serves the HTML/JSON from `.cache/` and reads the board files
(`.store/boards/<board>.duckdb`) and rcdata's serving files under `RC_MARKET_DIR` via short-lived
per-request read handles for API endpoints (rotation dates/latest, ticker store, watchlist
toggle, holdings bulk cache). Served pages never hold a DB handle across
requests.

**Refresh ignores the cache and recomputes from the store.** Full contract,
per-board table and the async job model: [`data_retrieval.md`](data_retrieval.md).
This is the general rule for every board backed by downloaded data (SEC 13F, Sector
Rotation, Swing Potential, Fundamentals, Asymmetric Growth, LEAPS, Financials,
Co-movement, Value Chain, Relatedness):
Refresh drops the board's `.cache/` html artifact and re-derives it from the
store with **zero network calls** (Value Chain reads its own
`boards/value_chain.duckdb`). `Fetch`, by contrast, updates the raw data on the store over the
network and then recomputes + re-caches. Boards that genuinely need live data and
hold no downloaded cache (Earnings Radar) keep their "click Fetch to fetch" empty
state. Because the `.cache/` artifact is always regenerable this way, deleting any
of it costs only a re-run — the durable source stays `.store/` (the board files
+ the `edgar_docs/` tree), with the market data behind it rebuildable by the builder.

## Concurrency model
DuckDB allows one *process* to hold a database file at a time (unlike SQLite in
WAL mode, a second process cannot even read it meanwhile — DuckDB's own `.wal`
file is a crash journal, not a concurrency mechanism), but any number of
connections **within** one process — as long as they all use the same
configuration. The scheduled tasks (the nightly backup, Relatedness off-peak and the Value Chain
filing sync, listed in `core/automations.py`; on homesrv each is a systemd timer) and
the dashboard both touch the store files, and inside the dashboard the `app/jobs.py`
worker thread writes while Flask request threads read. So:

- **One file per board, one writer per file (DECISIONS S9, S16).** This replaced the
  split-by-write-pattern layout the rest of this section was written for: `raw.duckdb` held the structured data
  and the identity spine, `edgar.duckdb` the EDGAR content and `prices.duckdb` the daily bars, each with its own
  backup retention, and every board shared the one writer process. Since #860 a board's tables live in
  `boards/<board>.duckdb`, written only by that board's package, so two boards no longer queue behind one
  file lock and a restore touches one board. What a board needs from another board's data it reads through
  rcdata's serving layer, not by joining another file; the cross-file dependency this bullet used to name
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
  from before #83 shows no `trigger`. Since #1205 that tab reads **`job_runs2`** in the new store (the table
  above went with raw.duckdb, #860): every run of every writer, the seconds of each step inside it
  (`counts["steps"]`, #1198), the runs of one pass grouped by their chain id (`params["chain"]`), a total per
  day, and the audit of the records themselves — a chain whose own outer run left no row, and a row with no
  chain id. That audit is the RevenueChaser half of home-server's daily count of starts against records: the
  two stores never read each other, so each answers for its own rows.
  Every job now opens the store twice, including jobs that never used it (Money flow, queued after each
  Rotation Fetch).
- **A scheduled task that writes tables leaves a `job_runs` row too (#129 A1).** `core/automations.run_task` runs
  the task's command as a subprocess, so the dashboard never sees it; it upserts the run's own row (`run_id` =
  `job_id`, a fresh 12-hex id; `trigger` `scheduled`; `module`, `source` and `registry_name` the registry name;
  `params` the command's arguments; `pid` the task runner) when the command starts (`running`) and when it ends
  (`done` on exit 0, else `error` with `exit <code>`; a step its chain blocked ends `error` with
  `chain blocked: <why>` and no `started_at`). The command gets the id in `RC_RUN_ID` and the registry name in
  `RC_RUN_SOURCE` (`rawstore.task_run`), so `ledger.new_run_id`, `app/jobs.py:run_id()` and the store's change
  capture stamp that id, and the source, on everything the run writes. **Every** command, table writer or not, also
gets the run's id in `RC_CHAIN_ID` (`ops.CHAIN_ID_ENV`), which the runs it starts keep in their own
`job_runs2.params["chain"]`. A task that writes no table (the backup)
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
  the legacy migration does (`save_watchlist` did, until PR 4 moved it to the
  stock watch list in `ops.sqlite`). A writer whose run produced nothing
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
  #186-#192), `orphan_designations` and `orphan_lookups` (#190) and `sec13f_filers`
  (G1 #292, manifest only). Retired today:
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
  (Value Chain filing sync) and 23:00 (daily price bars, #156; first the SEC exchange and fund files, #301; last the
  `^GSPC` daily levels, #293), plus Saturday 10:00 weekly (index lists,
  RC-Index-Lists, #302), weekdays not before 23:30 (the screeners Fetch RC-Screeners-Daily, chained on the
  daily price bars, #129), daily not before 23:35 (the forecast outcomes RC-Forecast-Outcomes, chained on the daily
  price bars, #380 S0), daily 23:50 (the forecast baselines RC-Forecast-Baselines, chained on the outcomes run, #25
  HR-1), the Earnings inputs (E0 #372, one entry per schedule since #620, none chained: the sweeps read no
  bars) RC-Earnings-Calendar Monday and Thursday 23:40, RC-Short-Interest weekdays 23:40, RC-Earnings-Daytime
  Tuesday and Thursday 13:00, RC-Macro-Weekly Thursday 23:40 and RC-CFTC-Weekly Friday 23:40, plus daily 09:15 (the IBKR Gateway login check RC-IBKR-Login-Check, #162:
  `scripts/ibkr_login_check.py`, one TCP connection and the TWS API version handshake, which writes no table and
  no store file, only its own record under `.cache/ibkr_login/`, and exits 0 whatever it finds — a session that
  expired on its weekly cycle is not a failed job), plus the market-store builder RC-Build hourly at :05 (one
  `rcdata.build run --once` pass over the request queue, unit `life-rc-builder`, 120 min limit) and the weekly
  backup RC-Backup-Weekly, Sunday 03:30 (`rcdata backup`, every step, unit `life-rc-backup-weekly`, 60 min limit;
  avo, 8 Nov 2026), plus
  Monday to Friday 06:30 (the SPX Gamma Fetch RC-SPX-Gamma-Fetch, #291: `module_spx_gamma --nightly`, the
  latest complete session's raw day with all expiries, only once its Cboe SPX close is stored and outside the US
  session, else exit 3; no catch-up; the Fetch itself is queued on the dashboard, `POST /run/spxgamma`, so the
  dashboard stays the one writer, and with the dashboard down the task exits 1);
  `core/automations.py`: the Relatedness and Value Chain rows are 30 min apart.

### Full-refresh vs. incremental tables
The JSON files these tables replaced were rewritten wholesale, so the tables
that mirror a complete snapshot use `replace_all` (an empty batch needs
`allow_empty=True`, #284) — a key absent from the new
payload is **removed**, not left behind: `ticker_scores`,
`fundamentals_scores`/`_gates`,
`swing_scores`/`_gates`. `watchlist` is no longer one of them: the dashboard
adds or removes one ticker per click, on the stock watch list in `ops.sqlite` since PR 4 (`rawstore.toggle_watchlist`). Per-ticker caches (`rotation_holdings`) stay incremental upserts, and
`rotation_snapshots` / `oipd_chain` /
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
| *(removed)* | The rows that stood here for the **result-history** tables (`result_runs` and its 17 run-keyed row tables), for the 8 `<table>_changes` tables and `etf_holdings_changes`, and for the retired names of `rawstore.RETIRED_TABLES` (`swing_ohlc`, `cusip_ticker`, `identity_source_cache`, `quote_snapshot`, `sec13f_enrichment`, `sec13f_ticker_names`, `sec_company_sic`, `sec_company_tickers`, `sec_exchange_file`, `sec_mf_file`, `sec_submissions_cik`, `ticker_payload_cache`, `vendor_valuation`) were columns of `raw.duckdb`, `edgar.duckdb` and `history.duckdb`, which #860 deleted. They are gone from this table; `core/tests/test_doc_constants.py` fails it if a retired name comes back as a row here, and `core/tests/test_no_old_store.py` fails code that names one. | — |
| `edgar_financials` (edgar DB) | `ticker`, `fetched_at DOUBLE` | `meta`; `annual`, `quarterly` are **zlib-compressed JSON** (`BLOB`, see `compress`) |
| `edgar_documents` (edgar DB) | `accession`, `cik`, `tickers`, `form`, `filed`, `fiscal_year`, `doc_url`, `path`, `sha256`, `bytes_raw`, `bytes_gzip`, `fetched_at`, `source`, `run_at` (#299; added to an existing store by `ALTER TABLE ADD COLUMN`, no bump) | — |
| `chain_filings` (value_chain DB) | `ticker`, `cik`, `form`, `filed`, `accession`, `primary_doc`, `quality`, `fetched_at` | `item_text` is **zlib-compressed text** (`BLOB`) |
| `chain_mentions` / `chain_meta` (value_chain DB) | `src`, `dst_name`, `dst_ticker`, `relation`, `snippet`, `filed`, `share_pct DOUBLE` (the share of the filer's sales or purchases a concentration sentence states; NULL on a keyword edge; #387 D7, added by ALTER at the first call), `source` (`profile` / `xbrl` / `regex`, #410; NULL on older rows; added by ALTER, no bump) / `key`, `value`, `fetched_at DOUBLE` (#127; NULL on older rows is unknown, never 0; added by ALTER, no bump) | — |
| `comov_runs` / `comov_nodes` / `comov_members` / `comov_lag` | `as_of` + run config (`n_factors`, `null_json`, `oos_json`) + the Fetch's `run_id`, `session_date DATE` (rule B on `as_of`) and `dropped_json` (the run's drop lists; #379 D2, additive, older runs read NULL) / `(as_of, cut, cluster_id)` metrics incl. `baseline_r`, `persist_*`, `oos_*` / `(as_of, cut, ticker)`, `core` (was in the matching group a quarter ago; `stability` legacy, no longer written) / `(as_of, cut, ticker)` lagged group + `oos_r`, `oos_base`, `held` | — |
| `rotation_holdings` | `ticker`, `date` (hoisted out of the payload), `fetched_at DOUBLE` (#127; NULL on older rows is unknown, never 0) | `rows` (holdings + RS list) |
| `index_constituents` | `slug`, `ticker` | — |
| `index_tickers` (VIEW) | `slug`, `ticker`, `name`, `sector` | — |
| `ticker_themes` | `ticker`, `theme_id`, `theme`, `level`, `theme_run_id`, `fetched_at DOUBLE` (#127; NULL on older rows is unknown, never 0) | — |
| `fundamentals_scores` / `swing_scores` | per-ticker metric columns (see MANIFEST) | — |
| `fundamentals_gates` / `swing_gates` | `ticker`, `gate`, `rule_id`, `passed`, `actual`, `unit` | — |
| `relatedness_profiles` | `ticker`, `cik`, `model`, `prompt_version`, `fetched_at`, `cache_key`, `status`, `attempts`, `provider` (the writer, #360: `evren` / `deepseek`, older rows also `grok` / `claude`; nullable, added by `ALTER TABLE ADD COLUMN`, no schema bump) | `profile_json` (the v3 structured profile, #401, schema 13; NULL on a v2 row), `label_source` (which chunk or table each v3 field came from, #401) |
| `relatedness_embeddings` | `ticker`, `model`, `dim INTEGER`, `vector BLOB` (a 16-byte hash of the embedded text, then the float32 bytes of `dim` values; raw, not zlib-compressed); `PRIMARY KEY (ticker, model)` (#401, schema 13; writer: `module_relatedness/embeddings.py`, #669) | — |
| `relatedness_attributes` | `ticker`, `field`, `value` (TEXT holding a JSON list), `value_num` | — |
| `relatedness_scores` | `ticker`, `peer`, `score`: each name's top `config.TOP_PEERS` peers. **The record of what was scored, not a read path** (#594, avo's "tut" of 19 Nov 2026): the board's neighbours come from the run's own `payload["peers"]`, so nothing reads these rows back — it was v1's read path and v2 (#400) recomputes the peers each Fetch. Kept because it is the only durable record of which pair scored what: `relatedness_scores_history` mirrors it, #284 stops a run that scored nothing from wiping it, and the incremental scoring of #884 would read it as the previous score. One writer, `module_relatedness/store.save_scores`, from a whole-board Fetch only (avo's decision F of 16 Nov 2026 — a sliced run would retire every pair it did not score) | — |
| `relatedness_themes` / `relatedness_theme_tags` / `relatedness_theme_members` / `relatedness_theme_etfs` | `theme_id` (run id is its prefix), `level`, `parent_id`, `label`, `size` / `tag`, `rank` / `ticker`, `weight`, `rank` / `etf`, `overlap`, `rank`. The members' `rank` (#298) is the place the name holds in the cap-descending classification universe, and `run_rows` reads that table `theme_id, rank NULLS LAST, ticker`: it is the order the naming prompt takes its example companies in, so they are the theme's largest names (avo's B, 19 Nov 2026); additive, added by `ALTER TABLE ADD COLUMN`, no schema bump, and a run written before it reads NULL and sorts last | — |
| `theme_runs` | `id`, `created_at`, `k_fine`, `k_medium`, `k_coarse`, `jaccard`, `is_current`, `session_date DATE`, `run_at DOUBLE` (schema 11); the five nullable validation numbers `val_peer_recall_25`, `val_hand_precision_10`, `val_gics_purity`, `val_etf_recall_medium`, `val_seed_ari` (all `DOUBLE`, #341, `module_relatedness/validation/`; added by `ALTER TABLE ADD COLUMN`, no schema bump; `val_peer_recall_25` = ETF-set mean, denominator `min(25, n-1)`) | — |
| `llm_cache` | `cache_key`, `ticker`, `cik`, `model`, `thinking`, `effort`, `prompt_version`, `input_hash`, `fetched_at`, `status` | `response` |
| `job_runs` | `run_id`, `job_id`, `module`, `lane`, `trigger`, `source`, `pid`, `registry_name`, `queued_at DOUBLE`, `started_at DOUBLE`, `finished_at DOUBLE`, `status` (`queued`/`running`/`done`/`error`/`cancelled`/`lost`, the `app/jobs.py` vocabulary), `error`, `message` | `params`, `counts` |
| `run_registry` | `key`, `value` | — |
| `sec13f_security_meta` | `ticker`, `fetched_at DOUBLE` (#127; NULL on older rows is unknown, never 0) | `entry` (`{name, sector, type}`) |
| `sec13f_totals` | `ticker` | `entry` (`{period: metrics}`) |
| `sec13f_edgar_backfill` | `cik`, `period`, `accession`, `fetched_at` | — |
| `etf_holdings` | `ticker`, `provider` (`uw`/`yfinance`), `fetched_at DOUBLE` (#303) | `entry` (holdings list) |
| `price_bars_daily` | `ticker`, `session_date DATE` (NY session date), `open`/`high`/`low`/`close`/`adj_close`/`volume DOUBLE`, `split_ratio DOUBLE` (always NULL from #156 on; splits are rows of `price_actions`), `adj_source`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, session_date)`; append (first write of a key wins; v10 key was `(ticker, date)`, rebuilt by the first v11 call with the rows kept) | — |
| `catalyst_events` | `ticker`, `event_date DATE`, `date_precision`, `kind`, `title`, `source`, `source_event_id`, `source_url`, `confidence`, `nct_id`, `fetched_at DOUBLE`. Key `(source, source_event_id)` | — |
| `orphan_designations` | `ticker`, `generic_name`, `designation`, `date_designated DATE`, `designation_status`, `approval_status`, `marketing_approval_date DATE` / `trade_name` (the earliest approval), `sponsor`, `fetched_at DOUBLE`. Key `(ticker, generic_name, designation, date_designated)` | `approvals` (every `{marketing_approval_date, trade_name}`) |
| `orphan_lookups` | `ticker`, `session_date DATE`, `sec_name`, `query`, `match` (`matched`/`unmatched`/`no_hits`/`failed`), `provider_status`, `n_rows INTEGER`, `error`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, session_date)` (schema 11; was `(ticker)`) | `candidates` |
| `forecast_ledger` | `source`, `forecast_date DATE`, `subject`, `target_type`, `target_date DATE` (NOT NULL), `frozen_at DOUBLE`, `run_id`, `model_version`, `rank INTEGER`, `score DOUBLE`, `bucket`, `market_cap DOUBLE`, `sector`, `shown_price DOUBLE`, `stop DOUBLE`, `target DOUBLE`. Key `(source, forecast_date, subject, target_type, target_date)`; no outcome columns | `probs` |
| `forecast_outcomes` | The ledger key (`source`, `forecast_date DATE`, `subject`, `target_type`, `target_date DATE` NOT NULL) + `horizon_days INTEGER`, `ref_price DOUBLE`, `mfe DOUBLE`, `mae DOUBLE`, `sessions_seen INTEGER`, `status` (`open`/`closed`/`no_bars`/`split_check`), `computed_at DOUBLE`, `model_version`, `run_id`. Key `(source, forecast_date, subject, target_type, target_date, horizon_days)`; keyed upsert (derived, #380 S0, added after schema 11 with no bump) | — |
| `forecast_baselines` | `forecast_date DATE`, `horizon_days INTEGER`, `basis` (`universe` or `vol_decile_1` … `vol_decile_10`, 1 the calmest), `n INTEGER` (members with a measurable outcome), `ge20`/`ge10`/`ge5 INTEGER` (cumulative counts of them whose MFE reached 20% / 10% / 5%), `mfe_mean DOUBLE`, `mae_mean DOUBLE`, `vol_lo DOUBLE`, `vol_hi DOUBLE` (the decile's boundaries, `vol_lo` inclusive and `vol_hi` exclusive, NULL at either end), `status` (`open`/`closed`), `computed_at DOUBLE`, `model_version`, `run_id`. Key `(forecast_date, horizon_days, basis)`; keyed upsert (derived, #25 HR-1, added after schema 12 with no bump) | — |
| `rotation_snapshots` | `as_of_date`, `ticker`, `name`, `"group"`, `close_price`, `rs_*`, `rs_ratio`, `rs_momentum`, `quadrant`, `benchmark`, `generated_at`, `source` (`live` from a Rotation Fetch, `backfill` from a range backfill; added by #374, no schema bump; older rows are NULL). Since M-ROT-2 (#374) both come from one function on D01 weekly closes and `as_of_date` is the latest complete session they end on (no weekend or holiday rows) | — |
| `ticker_scores` | 30 scalar columns (`market_cap DOUBLE`, `sector`, `industry_group`, `sub_industry`, `themes`, `signals`, …), plus `tier INTEGER` (3 Balanced, 2 Strong, NULL none), `flags` (JSON list of the C17/C18 data checks), `model_version` (the AG model version that scored the row), `anchor_fy INTEGER`, `prior_fy INTEGER` and `period_ends` (JSON object: revenue ends of the anchor and two prior years, balance-sheet and cash-flow ends), added by #380 AG W1 with no schema bump (ALTER on the first store call; rows of an older Fetch read NULL and the board shows them under a "scored before" notice) | — |
| `sec13f_submissions` | `accession`, `cik`, `period`, `filing_date`, `type`, `report_type`, `is_amendment`, `amendment_type`, `amendment_no`, `other_managers_count` | — |
| `sec13f_holdings` | `accession`, `line_no`, `cusip`, `cik`, `shares`, `value`, `period`, `putcall`, `sshprnamttype`, `title_of_class`, `investment_discretion`, `other_manager`, `voting_sole`, `voting_shared`, `voting_none`; `PRIMARY KEY (accession, line_no)` | — |
| `sec13f_cusip_names` | `cusip`, `name_norm`, `name_raw`, `count` | — |
| `sec13f_ingested_zips` | `filename`, `ingested_at` | — |
| `oipd_chain` | `ticker`, `option_symbol`, `expiry`, `option_type`, `strike`, `bid`, `ask`, `last_price`, `volume`, `open_interest`, `iv`, `delta`, `tape_time`, `fetched_at` | — |
| `oipd_meta` | `ticker`, `as_of`, `spot DOUBLE`, `risk_free DOUBLE`, `expiry_count`, `horizon_days`, `fetched_at` | — |
| `security_master` | `ticker`, `cik`, `cik_source`, `exchange`, `name`, `name_source`, `security_type`, `type_source`, `type_asof`, `sector`, `sector_source`, `industry_group`, `group_source`, `industry`, `industry_source`, `sub_industry`, `sub_source`, `gics_asof`, `updated_at`, `figi`, `composite_figi`, `share_class_figi`, `exch_code`, `security_description`, `figi_source`, `sic`, `sic_description`, `sic_group2`, `sic_source`, `cusip`, `cusip_source`, `isin`, `isin_source` (one row per ticker; every field carries its `<field>_source`; since #265 `sector` is one of the 11 Morningstar names, from the S&P 1500 list for its own names, else UW / Yahoo / SIC / type rule (#272), `industry` is UW's text, and `industry_group`, `group_source`, `sub_industry`, `sub_source` are NULL; since #272 `sic` is the 4-digit code (TEXT, leading zero kept), `sic_group2` its two-digit major group and `sic_source` `sec_submissions` or `sec_company_sic`, raw and independent of the sector; since #267 the five FIGI columns are copied from the last good `openfigi` row, never blanked, with `figi_source` `openfigi`; since #127 `cusip` is the OpenFIGI 13F map inverted by ticker (`cusip_source` `openfigi_13f`; two CUSIPs for one ticker keep the newest `valid_from`, then the lowest CUSIP) and `isin` is the ISO 6166 value derived from a digit-leading valid CUSIP (`isin_source` `derived_cusip`; a CINS or a name with no CUSIP is NULL in `isin`, and NULL in both when the map has no CUSIP); nothing reads the FIGI columns yet) | — |
| `identity_conflicts` | `ticker`, `kind`, `field`, `side_a`, `side_b`, `note`, `first_seen`, `last_seen` (key `(ticker, kind, field)`) | — |
| `identity_runs` | `run_id`, `started_at`, `finished_at`, `status` | `sources` (per-source status/counts/error), `counts` |
| `board_settings` | `board`, `key`, `value DOUBLE`, `updated_at`, `value_text TEXT` (a text setting, #360: each LLM job's `<JOB>_WRITER` / `<JOB>_MODEL` on the `llm_writers` board, `core/llm_jobs.py`, #404; a row fills `value` or `value_text`; nullable, added by `ALTER TABLE ADD COLUMN`, no schema bump) (key `(board, key)`; only user overrides — defaults are read from the board's config, and a Save is one transaction and a Reset deletes the board's rows in one statement, serialized by a lock; every save, drop and Reset is kept in `board_settings_changes`, a Reset as one change (#306); `core/board_settings.py`, issue #40; read by Co-movement (`module_comovement/settings.py`) and by Swing for its screen/score thresholds (`module_swing_potential/settings.py`, #384 SW-S4), whose stored overrides also feed the forecast ledger's model version) | — |
| `watchlist` | `ticker` (one row per pinned ticker until PR 4; unwritten since, the stock watch list in `ops.sqlite` replaced it) | — |
| `migration_manifest` | `dataset`, `migrated_at`, `schema_version`, `source_count`, `source_bytes`, `source_hash`, `verify` | — |
| `store_meta` | `key`, `value` | — |
| `store_schema` | `key`, `value` (one row, key `schema_version`, this code's `_SCHEMA_VERSION`; the same table is created in every store file on ensure, #26 PL7, no schema bump — not `store_meta`'s ticker-store `schema_version`) | — |
| `price_actions` | `ticker`, `ex_date DATE`, `action`, `value DOUBLE`, `session_date DATE`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, ex_date, action)` | — |
| `index_levels_daily` | `symbol`, `session_date DATE`, `open DOUBLE`, `high DOUBLE`, `low DOUBLE`, `close DOUBLE`, `volume DOUBLE`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(symbol, session_date)`; prices.duckdb (#293) | — |
| `short_interest` | `ticker`, `session_date DATE`, `market_date DATE`, `short_interest DOUBLE`, `si_float DOUBLE`, `total_float DOUBLE`, `days_to_cover DOUBLE`, `fee_rate DOUBLE`, `rebate_rate DOUBLE`, `short_shares_available DOUBLE`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, session_date)` | `payload` |
| `vendor_financials` | `ticker`, `source`, `statement`, `report_type`, `period_end DATE`, `valid_from DATE`, `status`, `revenue DOUBLE`, `gross_profit DOUBLE`, `operating_income DOUBLE`, `ebitda DOUBLE`, `net_income DOUBLE`, `operating_cash_flow DOUBLE`, `total_assets DOUBLE`, `inventory DOUBLE`, `receivables DOUBLE`, `deferred_revenue DOUBLE`, `rpo DOUBLE`, `total_debt DOUBLE`, `interest_expense DOUBLE`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, source, statement, report_type, period_end, run_at)` | `payload` |
| `earnings_history` | `ticker`, `endpoint`, `report_date DATE`, `valid_from DATE`, `status`, `report_time`, `actual_eps DOUBLE`, `eps_estimate DOUBLE`, `surprise_pct DOUBLE`, `expected_move_perc DOUBLE`, `post_earnings_move_1d DOUBLE`, `fiscal_period_end DATE`, `report_type`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, endpoint, report_date, run_at)` | `payload` |
| `estimate_snapshots` | `ticker`, `session_date DATE`, `period`, `eps_est_analysts DOUBLE`, `eps_est_avg DOUBLE`, `eps_est_low DOUBLE`, `eps_est_high DOUBLE`, `eps_est_year_ago DOUBLE`, `eps_est_growth DOUBLE`, `rev_est_analysts DOUBLE`, `rev_est_avg DOUBLE`, `rev_est_low DOUBLE`, `rev_est_high DOUBLE`, `rev_est_year_ago DOUBLE`, `rev_est_growth DOUBLE`, `eps_trend_current DOUBLE`, `eps_trend_7d DOUBLE`, `eps_trend_30d DOUBLE`, `eps_trend_60d DOUBLE`, `eps_trend_90d DOUBLE`, `eps_rev_up_7d DOUBLE`, `eps_rev_up_30d DOUBLE`, `eps_rev_down_7d DOUBLE`, `eps_rev_down_30d DOUBLE`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, session_date, period)` | `payload` |
| `identity_cusip` | `cusip`, `valid_from DATE`, `status`, `ticker`, `figi`, `share_class_figi`, `share_class`, `name`, `exch_code`, `security_type`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(cusip, run_at)` | — |
| `ownership_snapshot` | `ticker`, `session_date DATE`, `source`, `shares_outstanding DOUBLE`, `insider_pct DOUBLE`, `institutional_pct DOUBLE`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(ticker, session_date, source)` | — |
| `earnings_calendar` | `session_date DATE`, `earnings_date DATE`, `ticker`, `report_session`, `sector`, `full_name`, `marketcap DOUBLE`, `expected_move_perc DOUBLE`, `street_mean_est DOUBLE`, `pre_earnings_close DOUBLE`, `post_earnings_close DOUBLE`, `report_time`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(session_date, earnings_date, ticker, report_session)` | `item` |
| `macro_series` | `series_id`, `obs_date DATE`, `source`, `value DOUBLE`, `frequency`, `proxy_for`, `session_date DATE`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(series_id, obs_date, source)` | — |
| `vol_index_daily` | `symbol`, `session_date DATE`, `source`, `open DOUBLE`, `high DOUBLE`, `low DOUBLE`, `close DOUBLE`, `operator`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(symbol, session_date, source)` | — |
| `cftc_cot` | `report_type`, `market`, `report_date DATE`, `release_date DATE`, `open_interest DOUBLE`, `asset_mgr_long DOUBLE`, `asset_mgr_short DOUBLE`, `asset_mgr_change_long DOUBLE`, `lev_money_long DOUBLE`, `lev_money_short DOUBLE`, `lev_money_change_long DOUBLE`, `dealer_long DOUBLE`, `dealer_short DOUBLE`, `dealer_change_long DOUBLE`, `session_date DATE`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(report_type, market, report_date)` | `raw_row` |
| `economic_calendar` | `session_date DATE`, `event_ts DOUBLE`, `event`, `type`, `forecast`, `prev`, `reported_period`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(session_date, event_ts, event, type)` | `raw_row` |
| `news_events` | `event_ts DOUBLE`, `event`, `type`, `importance INTEGER`, `forecast`, `prev`, `reported_period`, `first_seen DOUBLE`, `last_seen DOUBLE`, `retired_at DOUBLE`, `run_id`, `run_at DOUBLE`, and for the FDA lane (#807) `ticker`, `drug`, `catalyst`, `target_text`, `target_precision`, `indication`, `status`, `outcome`, `url`. Key `(event_ts, event, type)` | — |
| `news_highlight` | `session_date DATE`, `event_ts DOUBLE`, `event`, `type`, `importance INTEGER`, `forecast`, `prev`, `text`, `text_source`, `run_id`, `run_at DOUBLE`. Key `session_date` | — |
| `news_headlines` | `headline_id`, `provider`, `published_at DOUBLE`, `headline`, `source`, `tickers`, `url`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`, `kind`, `option`, `x_check`. Key `headline_id` | — |
| `news_earnings` | `week_start DATE`, `ticker`, `company`, `report_date DATE`, `report_time`, `confirmed BOOLEAN`, `source`, `run_id`, `run_at DOUBLE`. Key `(week_start, ticker)` | — |
| `news_meta` | `key`, `value`, `run_at DOUBLE`. Key `key` | — |
| `news_reading` | `window_end DOUBLE`, `field`, `window_start DOUBLE`, `value`, `quote`, `headline_id`, `source`, `published_at DOUBLE`, `provider`, `model`, `prompt_version`, `headlines INTEGER`, `run_id`, `run_at DOUBLE`. Key `(window_end, field)` | — |
| `welcome_snapshot` | `series`, `session_date DATE`, `close DOUBLE`, `prev_close DOUBLE`, `change DOUBLE`, `change_pct DOUBLE`, `spark`, `run_id`, `run_at DOUBLE`. Key `(series, session_date)` | — |
| `welcome_earnings` | `earnings_date DATE`, `ticker`, `report_session`, `full_name`, `expected_move_perc DOUBLE`, `report_time`, `source_session DATE`, `first_seen DOUBLE`, `last_seen DOUBLE`, `retired_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(earnings_date, ticker, report_session)` | — |
| `welcome_freshness` | `job`, `last_status`, `last_finished_at DOUBLE`, `last_ok_at DOUBLE`, `last_failed_at DOUBLE`, `last_error`, `run_id`, `run_at DOUBLE`. Key `job` | — |
| `welcome_meta` | `key`, `value`, `run_at DOUBLE`. Key `key`. Unwritten (#643) | — |
| `spx_raw_days` | `session_date DATE`, `requested_date`, `api_snapshot_time`, `origin`, `spot DOUBLE`, `zero_dte_expiry`, `file_count INTEGER`, `manifest BLOB`, `stored_at DOUBLE`. Key `session_date` | — |
| `spx_raw_files` | `session_date DATE`, `file`, `sha256`, `bytes INTEGER`, `body BLOB`, `stored_at DOUBLE`. Key `(session_date, file)` | — |
| `spx_brier` | `asof_date DATE`, `scenario_probs JSON`, `outcome`, `brier DOUBLE`, `rule`, `target`, `bands JSON`, `resolved_on`, `resolved_close DOUBLE`, `updated_at DOUBLE`. Key `asof_date` | — |
| `spx_gex_daily` | `session_date DATE`, `is_friday_close BOOLEAN`, `spot`, `net_dex`, `net_gex_oi`, `net_vanna`, `live_charm`, `vanna_at_spot`, `charm_live_sum`, `matrix_gex`, `source_chip`, `regime`, `vix`, `updated_at DOUBLE`. Key `session_date` | — |
| `spx_meta` | `key`, `value`, `updated_at DOUBLE`. Key `key` | — |
| `sec13f_board_cells` | `ticker`, `period DATE`, `shares`, `value`, `holders INTEGER`, `top10_pct`, `hhi`, `new_holders INTEGER`, `exited_holders INTEGER`, `buy_flow`, `sell_flow`, `breadth`, `breadth_chg`, `complete BOOLEAN`, `closed BOOLEAN`, `stored_at DOUBLE`. Key `(ticker, period)` | — |
| `sec13f_board_breaks` | `ticker`, `period DATE`, `kind`, `cik`, `filer_name`, `shares DOUBLE`, `stored_at DOUBLE`. Key `(ticker, period, kind, cik)` | — |
| `sec13f_board_meta` | `key`, `value`, `updated_at DOUBLE`. Key `key` | — |
| `wl_list` | `list_id`, `name`, `description`, `position INTEGER`, `feeds`, `created_at DOUBLE`, `created_by`, `updated_at DOUBLE`. Key `list_id`; boards/watchlists.duckdb (#786) | — |
| `wl_section` | `list_id`, `section_id`, `name`, `position INTEGER`, `created_at DOUBLE`. Key `(list_id, section_id)` | — |
| `wl_item` | `list_id`, `symbol`, `section_id`, `position INTEGER`, `exchange`, `ticker`, `coverage`, `note`, `added_at DOUBLE`, `added_by`. Key `(list_id, symbol)` | — |
| `wl_change` | `list_id`, `symbol`, `action`, `changed_at DOUBLE`, `changed_by`, `note`. Key `(list_id, symbol, changed_at, action)` | — |
| `source_checks` | `dataset`, `source`, `key`, `checked_at DOUBLE`, `run_id`. Key `(dataset, source, key)` | — |
| `price_checks` | `dataset`, `source`, `key`, `checked_at DOUBLE`, `run_id`. Key `(dataset, source, key)`; prices.duckdb (#156) | — |
| `sec13f_filers` | `accession`, `cik`, `period`, `name`, `source`, `fetched_at DOUBLE`, `run_id`, `run_at DOUBLE`. Key `(accession)` | — |
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
  (`ticker_payload_cache.fetched_at`, `rotation_holdings.date`), while the
  heterogeneous remainder of the record
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

**Where a board's SQL lives (#217, F12).** A board reads through one reader module of its own, `store_reads.py`,
and carries no `SELECT` and no table name anywhere else. A board on the serving layer reads `rcdata.read` through it
(`module_spx_gamma/store_reads.py`, `module_earnings_radar/store_reads.py`); a board with a file of its own reads that
file through it (`module_news/store_reads.py`, which owns the four `news_*` table names, every read of
`boards/news.duckdb` from any package, and the SQL that cuts the page's three headline lanes). The writes stay with
the code that produces the rows, but they take their table names from the reader, so a rename or a new column is a
change to one file. Each step's `tests/test_store_reads_217.py` pins it: no file but the reader
queries that store, no module of the board spells one of its table names, and every table the manifest files under
that `db` is named in the reader. The strangler is board by board — on `main` at `d349458` there were 83 non-test
`rawstore.query` calls; the News board (PR #1195) took 20 of them plus 3 Welcome-card reads of the same store,
leaving 60.

The Relatedness board is the second step: `module_relatedness/store_reads.py` owns the eleven table names of
`boards/relatedness.duckdb` and the 15 reads of it — 10 in the board, 2 in `module_sector_rotation/theme_focus.py`
and 3 in `core/universe.py`, which held its own copy of the pinned-run lookup and of the
`theme_id = ? OR starts_with(theme_id, ?)` predicate that means "the rows of this run", so the board and the one
Theme vocabulary (ADR-2) now cut a run the same way by construction. 59 non-test `rawstore.query` calls -> 45.

The Co-movement board is the third step: `module_comovement/store_reads.py` owns the four table names of
`boards/comovement.duckdb` and the 13 reads of it — 10 in `module_comovement/engine.py` and 3 in
`module_relatedness/engine.py`, which held the Co-movement board's own rule for picking the run to read (the
newest prices, then the board's default window (#95), then the latest), so the two boards could disagree about
which run is the current one. The dated run is still written in one transaction in `engine._persist`, table names
from the reader. 45 non-test `rawstore.query` calls -> 33.

The last two boards follow. **Welcome** (`module_welcome/store_reads.py`, 5 reads): the four table names already
sat in one module, so what this step took was the SQL, and with it two pairs of reads of the same table that have
to agree — the tile's "newest close per series" and the build's "what do I already hold", and the card's two-day
earnings cut with its `retired_at IS NULL` beside the build's without it (45 -> 41 on its own base).
**Sector Rotation**
(`module_sector_rotation/store_reads.py`, 5 reads): 3 in `store.py`, 1 in `etf_holdings.py` and one in
`core/provenance.py` — the board's **data date**, resolved by SQL three directories from its tables, which is the
shape of the News bug (`news_calendar` never existed, and a read swallows `CatalogException`, so that board's date
had been blank for ever). A date that comes from a board's rows is read by that board. 33 -> 30.

That is every board with a file of its own. What is left under `rawstore.query` is `core`'s own single-purpose
modules (`outcomes.py`, `provenance.py`, `baselines.py`, `ledger.py`, `ticker_store.py`, `ticker_themes.py`,
`universe.py`), each already the only owner of its tables, where wrapping buys nothing: #217's harm is a rename
touching every file of a board, and that does not apply there.

**The first board on it: SPX Gamma (PR 3, D7).** `module_spx_gamma/store_reads.py` is the board's one reader (D4:
no SQL of its own, no kept connection; a missing day is a "not stored" marker, never an exception or a provider call).
Since #330 it reads through the needs layer, `needs.ask(need, as_of=day)` over `market.spx_expiry_strike`,
`market.spx_greek_expiry_strike`, `market.vix`, `market.cot`, `market.cot_release` and `market.spx_close` (its `READS`;
the greek grid feeds the DTE-band walls, falling back to the day's own per-expiry files when the day is not stored, #291). VIX and COT are judged against the day rendered, not
the newest session, with the catalog's lag limits; the closes and the 0-1 DTE slice keep their own rules, and a day
the calendar's holiday table does not cover keeps its rows with the lag left undecided: the 0-1 DTE dollar OI gamma per strike (`call_gamma_oi + put_gamma_oi` over the
expiries 0 and 1 DTE of `spx_gamma_expiry_strike`; a day whose session has not closed reads the previous session's
snapshot kept to the expiries from the day on, decision A3; that is the day's 0 DTE expiry only, labelled "0 DTE only
(previous-session snapshot, <date> <time>)" on the pin line and in the banner, a smaller and conservative sum, review
SF2) for the $10B pin and $200M squeeze tests (review S4; a day without the slice has neither a pin flag nor a squeeze
trigger, avo 28 Oct 2026); the
VIX family (`VIX`, `VIX9D`, `VIX3M`, `VIX6M`, `VIX1Y`, `VVIX`: the newest observation on or before the day, inside
3 sessions); the E-mini TFF report (`cftc:tff:13874A:*`: the newest report released on or before
the day, inside 10 sessions, on CFTC's own release day from the stored schedule (`CFTC_COT_RELEASE_LAG`, #227: days
from each report to its release, keyed on the report's Tuesday, fetched weekly); a report the schedule does not cover
counts as released on Tuesday + 3 days, an estimate the label marks with "~"; an unreleased report is never judged; the weekly change is this report minus the previous one, decision A5; None, shown "n/a (gap)", when the two newest reports are not 5 to 9 days apart (#228)); and Cboe's
`SPX` closes for the 8-week window (one column: no open, high, low or volume, decision A6). The job of an accepted
Fetch queues `uw/spot_expiry_strike` and `market_series` with `requested_by` `spx_gamma` (`ops.enqueue_once`) and
renders at once with what is stored.

**Roots** (environment, read in `core/config.py` through `_root()` like `RC_STORE_DIR`; the repo-local defaults
`.market/` and `.market_raw/` are git-ignored, so a checkout, the tests and the cloud never touch a server path):

| Variable | Constant | Holds |
|---|---|---|
| `RC_MARKET_DIR` | `MARKET_DIR` | The serving DuckDB files and `ops.sqlite` (the server's SSD); with `RC_REDIS_URL` set and no `RC_REDIS_NAMESPACE`, also the key of the Redis namespace the ops state lives in |
| `RC_MARKET_RAW_DIR` | `MARKET_RAW_DIR` | `raw/<source>/<dataset>/...` and `parquet/<source>/<dataset>/<partition>/` (the HDD) |
| `RC_MARKET_RAW_MARKER` | `rcdata.build.base.RAW_MARKER_ENV` | Optional: a file name under `RC_MARKET_RAW_DIR` that only the mounted HDD holds; when set, a run without that file refuses before its fetch (#292 N-2). Unset, nothing is checked |

**Build memory.** The one read-write connection of a build (`serving.build`) is opened with DuckDB's `memory_limit`
and `threads` from `RC_DUCKDB_THREADS` (default `3`); an empty or invalid value keeps the default. The memory limit
**follows the machine at build start** (`serving.auto_memory_limit`): `RC_DUCKDB_MEMORY_SHARE` (default `0.6`) of
what is still available, held between `RC_DUCKDB_MEMORY_FLOOR` (default `2GB`) and `RC_DUCKDB_MEMORY_CAP`
(default `12GB`). "Available" is the smaller of the kernel's `MemAvailable` and the headroom of the process's own
cgroup v2 limits (`memory.max` / `memory.high` minus `memory.current`, at every level up to the root, because a
limit on a parent slice binds its children): on homesrv the builder runs under a systemd `MemoryMax`, so free host
RAM alone would be a limit the cgroup kills the build for taking. A fixed 5 GB was both too much and too little —
the `sec_forms` build of 19 Nov 2026 was OOM-killed at it while about 10 GB was free. `RC_DUCKDB_MEMORY_LIMIT`
(a DuckDB size such as `5GB`) overrides all of it, and `5GB` stays the fallback for a machine whose free memory
cannot be read. Each build logs the limit it got, its threads and what was available. Over the limit DuckDB spills rather than growing: without
it a connection may take 80 % of the machine's RAM, and the kernel OOM-killed the first `prices/serving` build on
#319's code (request #35, 30 Oct 2026, 7.7 GB peak on a 15 GB server). The spill goes to
`<MARKET_RAW_DIR>/build_tmp/<build file>.tmp` (`serving.spill_dir`, the HDD), never beside the live files on the SSD
(#372: request 71 spilled 47 GB there), and DuckDB removes it when the build closes. `max_temp_directory_size` comes
from `RC_DUCKDB_MAX_TEMP` (default `200GB`), held to 90 % of the spill disk's free space when the build opens
(DuckDB's own default, which a set cap replaces; #379): past it the build fails instead of filling the disk.
`RC_DUCKDB_TEMP_DIR` moves the
spill's parent folder (unset: `<MARKET_RAW_DIR>/build_tmp`).

A build step's own in-memory query connection (`base.scratch_con`, `serving.connect_scratch`) carries the same
adaptive limit, overridden on its own by `RC_DUCKDB_SCRATCH_MEMORY_LIMIT`, and spills into a folder of its own
under `<RC_TMP_HDD_DIR>/duckdb-scratch` (else the raw root), one per connection because two DuckDB instances on one
spill folder write over each other's blocks. A bare `duckdb.connect()` is not allowed in `rcdata/` any more
(`core/tests/test_scratch_connections.py`): it has no limit — DuckDB's own default is 80 % of the machine's RAM —
and no spill folder, so such a step grows until the kernel ends the build. Serving **readers** keep their fixed cap
(`RC_DUCKDB_READ_MEMORY_LIMIT`, default `1GiB`): many run at once, so a share of free memory each would add up to
more than the machine has. Each dataset's build logs one line with its seconds and
its spill peak (`base.spill_peak`, sampled every 2 s), the measure for moving the spill to the SSD or chunking a kind.

**Files.** Serving kinds: `market`, `spine`, `prices`, `ownership`, `fundamentals`, `options`, `sec_forms`, `sec_reads`. Each kind is one
file, `<MARKET_DIR>/<kind>.duckdb`. The identity spine, `spine` (serving schema version `10`), is described under
"The identity spine" below; the prices store, `prices` (serving schema version `1`), under "The prices store"
below; the ownership store, `ownership` (serving schema version `5`), under "The ownership store" below; the
fundamentals layer, `fundamentals` (serving schema version `7`), under "Fundamentals" after it; and the options
data, `options` (serving schema version `3`), under "The options data" after it; and the SEC form reader, `sec_forms` (serving schema version `1`), under "The SEC form reader"; and the SEC reader tool, `sec_reads` (serving schema version `3`), under "The SEC reader tool" last. The eight numbers live in `rcdata/serving_versions.py`, outside the builder's rules fingerprint
(`rcdata/build/dirty.py`), so a bump makes only its own kind's next build whole (#649). The market data,
`market` (serving schema version `5`): `serving_schema_version` (one row: kind, version, build time, build id, build mode),
`spx_gamma_expiry_strike` (key session date, expiry, strike; every numeric field UW serves, typed DOUBLE; provenance
`source`, `source_as_of`, `fetched_at`, `run_id`, `observed`), `spx_greek_expiry_strike` (the same key, the four
greeks per side from `greek-exposure/strike-expiry` for every expiry of the chain, same provenance; where two
option series share an expiry date — SPX and SPXW on a third Friday — UW's two rows for the strike are summed
into one, because its answer has no field to tell the series apart; its `dte` is
UW's **calendar**-day count, while `spx_gamma_expiry_strike.dte` counts NYSE sessions, so the two columns are
different measures of different endpoints and are never substituted) and `market_series` (key series id, observation date,
source; `value`, OHLC for the index files, `frequency`, `session_date`, provenance), `economic_event` (UW's economic
calendar, hourly: key snapshot day, event time as epoch seconds, event, type; `forecast`, `prev` and
`reported_period` as text; each day is the newest fetch of that New York day, so a same-day revision is kept) and
`fda_event` (UW's FDA calendar, hourly, market 5 / #807: key snapshot day, target date, ticker, drug; `catalyst`
(PDUFA Date, Advisory Committee Meeting, ...), `application_status` (UW's own `status`, renamed because
`rcdata.read` stamps a `status` of its own on every row), `indication`, `description`, `notes`, `outcome`,
`outcome_brief`, `source_link`, `has_options`, `marketcap`, and the target date twice — `target_text` as UW served
it and `target_date` the day it resolves to, with `target_precision` (`day`, `quarter`, `half`, `mid`, `late`)
saying what the text really said, because UW writes `2027-Q2` and `2027-LATE` as well as a day and a quarter is
never read as a day; a day is the newest fetch of that New York day like the economic calendar's). Market 3's
`news_headline` (IBKR's Dow Jones headlines, #643) left in market 4: the headlines live only in the News board's
`boards/news.duckdb` (avo's option A, 10 Nov 2026). `session_date` comes from
`core/calendar.py` and is empty outside its 1993-2028 table (its rule C: no second calendar). The previous file
stays as `<kind>.prev.duckdb`.

**The swap rule.** A kind is built as a new file `<kind>.<build_id>.duckdb`, from Parquet only (no network), and
checked before it replaces anything: its `serving_schema_version` row, the row count of every table, the run's
`dataset_check` row (a `failed` one refuses the swap), and the live file: every live table must be in the build (except
one the kind's code retired by name, `serving.RETIRED_TABLES`: `prices`' `crsp_unadjusted` since PR 382, `market`'s
`news_headline` since market 4), and
none may empty or lose more than 1% of its rows unless the request says `"allow_shrink": true` (the CLI's
`--allow-shrink`), which relaxes only the asking dataset's own tables (`tables`) and the served tables of its kind its
rows reach, which the dataset declares itself (`feeds`, `base.shrink_scope`): never a table its rows never reach, and
never another source dataset's own table. Until #326 the second half was inferred — every table of a dataset with no
provider was relaxed for every dataset of the kind — so a run on one feeder could let a table its rows never reach
shrink (#293). A dataset that declares no `feeds` relaxes its own tables only;
`rcdata/tests/test_shrink_scope.py` holds the declarations to their kind: every served table is owned or fed by at
least one dataset, and no declaration names a table of another kind.
A table that serves only its newest sessions (`option_eod`) names its first kept session in the
build file's `serving_window` table, and is compared on the rows inside that window: a session leaving it is not a
loss, a session inside it that lost rows is. That window is read from the Parquet partitions the build found, so the
window itself is compared as well: a build whose windowed table holds fewer sessions than the live file's own window
does is refused (#292 N-4; before it, a build that found one session matched the live file row for row inside its own
one-session window and swapped 20 sessions away). A table whose row count the source or avo chooses rather than
accumulates is named in `serving.SHRINKS_BY_CHOICE` (`my_universe`; `economic_event` since 14 Nov 2026, because UW's
calendar is a forward window and a snapshot day's partition is replaced whole by that day's newest fetch;
`fda_event` since market 5, kept the same way): it may
shrink by any amount and still may not empty. A build also refuses
a missing Parquet root (the raw disk not mounted). `prices` adds a guard of its own before the swap (`rcdata/build/prices/serving.py` `_refuse_shrink`): `price_bar_daily` is compared with the live file **per security id** as well as over the file, so a name it goes on serving with fewer bars refuses the build even when the table grows (#1096, avo 19 Nov 2026). The per-id check has a tolerance of `5` bars (`PER_ID_SHRINK_BARS`), which is what a source restating the last days of one name costs; a shortfall within it is named in the build note instead of refused, and the file total may not shrink at all whatever the per-id counts are. A name no source returns a bar for at all keeps its stored bars, carried over from the live file (#1088). Then the live file is copied to `<kind>.prev.duckdb` under
`serving_prev/` of the raw root (the HDD; `RC_SERVING_PREV_DIR` moves it) and
`os.replace` moves the new file over the live name, so the live name is never missing. A build does not start when the live files' disk has less free space than
`RC_BUILD_MIN_FREE` (10 GiB) or twice the live file (`serving.check_room`, 15 Nov 2026: a full root SSD froze homesrv). Any failure deletes the new
file and leaves the live file byte-identical (test T3, `rcdata/tests/test_serving.py`). The dashboard opens a serving
file read-only on every request (`rcdata.serving.open_read`) and never writes one; serving files are never
compacted, they are rebuilt. Pages only enqueue requests; the builder takes them in order.

**A build's base.** A build file starts either empty (`serving.build(kind)`, every table made from Parquet) or as a
copy of the live file (`serving.build(kind, base="live")`), for a build that replaces only what changed: the rows
already served are in the file, so a dataset can reload what is dirty instead of its whole history. The copy is a
build file like any other — the live file is never opened read-write — so the checks above, the atomic swap, the
`prev` rollback and the shrink guard all hold unchanged. `serving_schema_version.build_mode` records which base the
build really got: `"live"` falls back to `"fresh"` when there is no live file yet, or it was built by another serving
schema version, whose tables are not the ones this code builds — which is what makes a version bump a whole rebuild
without any dataset asking for one.

**What is dirty** (`rcdata/build/dirty.py`). A build on the live copy only pays off when it can say which rows to
reload, so the contract for that answer sits in one module. `full_rebuild_reason(kind)` names the cases where no
dirty set is safe and the kind must be built whole: a live file that is missing or built by another serving schema
version (`serving.live_version`), or a dataset that declares `full_rebuild_reason(ctx)` because its source restated
history (a WRDS monthly refresh, SEC republishing a set). `dirty.of(ds, ctx)` is one dataset's dirty set, a `Dirty`: a dataset
declares `dirty(ctx)`, and a dataset that declares none is `dirty.ALL`, which is why every kind is still built whole
today. A `Dirty` has two axes, because the tables have two shapes: a **cumulative** table only gains rows for new
source days, so what is dirty is a *range* of days; a **restated** table can have an old row changed by new data, so
what is dirty is a *key* whose whole history is rebuilt. A chosen list (`my_universe`) narrows to neither. A `Dirty`
carrying neither axis means everything, so a dataset that forgets to narrow gets the slow, correct answer instead of
a silently partial table; an answer of any other shape raises. The signal a `dirty` is meant to read is `changed_partitions(since)`: the Parquet files written after the live
file's `built_at` stamp. That stamp is written when the build file opens, before any dataset reads Parquet, so a
partition written while a long build runs is newer than the stamp and comes out dirty on the next build instead of
being lost. The signal is the partition's mtime, which a copy or a restore can carry over from another machine; it
becomes a manifest of (path, size, mtime) compared manifest-to-manifest before any kind relies on the dirty set for
data correctness. Which class each served table falls in — cumulative (a day only adds rows), restated (new data
rewrites served rows) or a derivation rebuilt whole — is read from the build code in `docs/dirty_table_classes.md`,
together with the two dependencies (the spine's symbol map, the prices universe) that make a table dirty with no
new source day of its own.

**The audit sample** (#446 step 5, `rcdata/build/audit.py`). A build on the live copy never looks at the rows it
carries over, so a row built wrongly — by a rule that was once wrong, or by a dirty set that missed it — would stay
served until a whole build, and the whole build is what #446 removes. Instead every incremental build re-reads a
small, risk-weighted sample of keys from Parquet and compares them with the file. A dataset declares an
`audit(ctx)` answering with `Subject`s (a served table, its key column, SQL that rebuilds that table's rows for
given keys from Parquet, and optional SQL weighting the keys); a dataset that declares none is not audited, as a
dataset that declares no `dirty` is not built incrementally. The draw is A-Res over the declared weight times how
long ago each key was last audited (`build_audit`, carried in the serving file; a key never audited counts as ten
years old), and `RC_AUDIT_KEYS` sets how many per pass (default 500, `0` off). A difference is **evidence, not a
refusal**: it opens a build issue (`kind = audit`, so the Logs board shows it) and marks the key for the next build
(`build_audit_dirty`, which `dirty.plan` hands to that dataset through `Plan.audited` and `ctx.audited`, for its
own `dirty` to add in its own key space — only the dataset knows what its keys mean), while the file still swaps in —
refusing it would stop the whole flow on one bad key, and the next pass rebuilds those keys anyway.

**What the Settings board shows of all this** (#446 step 8). The Data tab carries one **Serving builds** card,
above the per-file sections: a row per served file saying which base its last build got (`serving_schema_version`),
how many Parquet files its manifest holds and how many differ from the tree right now (`build_manifest`, compared
exactly as `dirty.changes` does), what the last audit round checked and found (`build_audit`), and how many keys
it still owes (`build_audit_dirty`). Every number is read out of the files themselves on each render
(`module_settings/newdata.py`'s `_build_of`, on the connection the page already opens per kind), so there is no
second copy of it anywhere to keep in step. A file that carries none of those tables was built before #446 landed
and reads as a whole build with no manifest, never as an error.

**Whose build code changed** (#814). Build code is a rule: a change to an extraction or a table rule restates rows
that no Parquet file marks as changed, so the build file carries a fingerprint of the code it was built by and a
change to it forces a whole build. That fingerprint used to be one per kind — every Python file in the packages
holding the kind's datasets — which made the first pass after **every** deploy rebuild `ownership/serve` from
scratch: 2,352 s and a 23.8 GB spill, measured on homesrv on 12 Nov 2026, above the 20 GB cap the build line itself
printed. The file now carries one fingerprint per dataset plus one shared (`dirty.fingerprints`, table
`build_rules`). A dataset's own fingerprint covers the files **of its own package that its code reaches**
(`dirty.code_files`, the import graph inside `rcdata/build`, stopping at the shared files), so `ownership/serve` is
restated by an edit to `ownership/common` and not by one to `ownership/regsho`; the package stays the boundary it
always was, so this only ever narrows what a change restates. The shared fingerprint covers `rcdata/serving.py` and
`rcdata/build`'s `__init__.py`, `base.py` and `dirty.py` — the code every kind's rows depend on — and a change there
is still a whole build of every kind. A dataset whose own code changed is everything of itself: its tables are
restated on the live copy and the rest of the kind keeps its dirty set, which the pass's note names
("restated for new build code: ...").

**Which kinds build on the live copy** (#446). `fundamentals` (#554) reloads its dirty companies. `prices`
(`rcdata/build/prices/incremental.py`) rebuilds every served row on or after one day — the first day of each changed
WRDS year partition or Alpaca bar session, and at the latest the day after the live file's newest session — and,
per security, a whole history (a name that joined or left the universe, a changed issuer CIK, PERMNO link or
Compustat alias) or the rows from a changed cover's `as_of` or Alpaca action's ex-date, closed over the issuer. The
identity a build used is recorded in the file's own `build_identity` table, bookkeeping like `build_manifest`, so the
next build sees what moved. It reads the WRDS history whole only through
views, for the identity (`_universe`, `_permno_link`, `_gvkey_alias`), and stages from the January of the year before
that day; a whole build staged forty years of `comp.secd` into TEMP tables (homesrv, 9 Nov 2026: 6 h 12 min, 92 GB
of spill). `wrds_price_diff` is kept from the last whole build, which the monthly WRDS refresh (a new
`comp.sec_split` snapshot) forces; so do a change of `wrds/sec_idhist`, of the build code, and `RC_BUILD_WHOLE=1`.
`ownership` (`rcdata/build/ownership/common.py` `build_keyed`) keys every source table by the partition its rows
come from (`date=`, `half=`, `day=`, carried in each row as `settlement_date`, `list_date`, `snapshot_date`, `half`
or `index_day`): a build on the live copy deletes the rows of each changed, new or gone partition and loads only
those, then names every row's ticker again from the live spine and rewrites the rows whose name moved, so a spine
change reaches rows no partition touched. A new source of the store (N-PORT) declares the same `dirty` and
`build_keyed`. The 13F totals (`ownership/serve`) are still derived whole on a `sec/13f` or `ownership/serve` run
and kept as they are otherwise (whole build of 6 Nov 2026: 91 min).
`market` (`rcdata/build/partitions.py`) reloads each changed Parquet partition: a UW session (`date=`) of
`spx_gamma_expiry_strike` or `spx_greek_expiry_strike`, a source and year of `market_series`; a gone partition only
deletes its rows. `options` is still built whole; `spine` stays whole by design (seconds).

**A whole build the hourly pass does not start** (#446 step 8). A dataset may declare `whole_build_by_hand = True`;
`prices/serving` does. When the plan for such a kind says the build would be whole and **every** member of the pass
is the schedule's own request (`RunContext.scheduled`, `requested_by` `schedule`; one Fetch press among them is a
person asking for it, and a build called any other way is not held at all), `base.build_kind_many` raises
`base.WholeBuildByHand` before the build file is opened, and the request ends `skipped` with the reason on its check
row. Nothing is copied, staged or swapped, so the live file is untouched and the dataset stays due for the next
pass. The reason it holds: a whole `prices` build is 86 min for the 2010+ volume and 6 h 12 min with the WRDS
history, against a builder window of under an hour, while a build on the live copy fits it — so `prices/serving` is
in the scheduled flow (it no longer `starts_by_hand`) and only its whole build waits for a person.

**What each kind's last build did** (#446 step 8). Nothing outside the build could see whether a build had rebuilt
its rows or carried them over: a build's rows and its time were recorded and no more. `rcdata/build/state.py`
answers it from the live files — `serving_schema_version.build_mode` (`live` or `fresh`, with `built_at` and
`build_id`), the row count of `build_manifest`, and the last round of `build_audit` with the keys
`build_audit_dirty` holds for the next build — and `GET /api/build/state` serves one row per kind. It reports the
**last** build, not the next one: asking `dirty.plan` would answer the second question but calls `dirty.scan`,
which stats every Parquet file of the kind, so one request would cost minutes of disk, on every call. Whoever
draws it says so in words, since a kind whose build code changed since its last build is built whole next time
and the file cannot know it yet. Every read is `serving.open_read` (read-only) and the route starts nothing; a
kind that cannot be read is that row's `error` rather than a failed answer, and a table the file does not carry
leaves its cell empty instead of reading as zero.

CFTC COT rows are futures-only: the TFF dataset's `Combined` rows (futures and options) are filtered out; taking
them instead is a later choice.

**Raw and Parquet.** A raw file that exists is never fetched again: it is the resume marker. Raw files and Parquet
partitions are written to `<name>.tmp` and renamed, so a file under its real name is whole (zstd Parquet, row groups
of 100k rows). The SPX slice is one raw file per session: `raw/uw/spot_expiry_strike/<date>/SPX.json.gz`, Parquet
`parquet/uw/spot_expiry_strike/date=<date>/`. An ordinary session is one call (UW's page is at most 500 rows); a
session that fills the page (OPEX and most Thursdays and Fridays: 575 rows on 21 Mar 2025, 683 on 15 Oct 2026) is
paged with `page=1, 2, ...` (0-based, the same `limit`, `date` and `expirations[]`) until a page is shorter than
500, an empty one after a page of exactly 500. Every page is a call against the day cap. The day is written only
when every page arrived, each a `data` list with a row on the session: a failed page, a later page without a
`data` list or stamped wholly on another day fails the day (the client's retry ladder, nothing more), and 20 full
pages (`MAX_PAGES`) fail it with a `truncated` build issue, so no partial day is kept. A key (expiry, strike) on two
pages fails the day too, with a `duplicate_key` issue: pages are offsets into an ordered set, so a repeat means the
answer moved while paging and a row may also have been skipped; the next run asks again. A repeat inside one page
goes, as on a one-page day, to `to_parquet`'s per-key `duplicate_key` issue (the first row kept). A day that keeps
failing is asked again by every run (the hourly one and each backfill pass), at up to 20 calls a run at the
ceiling, until it is fixed. A one-page day's raw file holds UW's body as sent; a paged day's holds
`{"data": <the rows of every page, in order>}`, with each page's row count in `request.pages`. The market series
keep one raw file per fetch with a `.meta.json`
sidecar (as-of date, fetch time, run id, URL without its query) and Parquet per source and year, merged by key with
the newest fetch winning a key.

**The ops state: Redis or `ops.sqlite`** (master plan Order 3). `rcdata/ops.py` is the one surface; which store it
uses is the environment, read on every call. With `RC_REDIS_URL` set (`redis://127.0.0.1:6379/0` on the server)
every function reads and writes Redis (`rcdata/ops_redis.py`): the same tables below, the same rows and ids, the
same answers. The Redis is the `redis` service of the root `docker-compose.yml`: a container with AOF on
(`appendonly yes`, `appendfsync everysec`, `noeviction`), bound to `127.0.0.1` only, its data in
`RC_REDIS_DATA_DIR`, so a restart keeps the day's call counts, the queue and the open issues. Keys sit under
`RC_REDIS_NAMESPACE` (the server sets it, `rc:ops:live`, so a moved or remounted market root keeps its state); unset,
under `rc:ops:<12 hex of sha1 of the resolved RC_MARKET_DIR>`, one namespace per market root, which is how the tests
and the e2e dashboards keep apart. A namespace never written is refused on its first write (`ops.OpsNotCopied`) while
the root's `ops.sqlite` still holds rows: Redis switched on before the copy, or a lost namespace, cannot start the day
from zero calls and empty watch lists; each row is a hash `<ns>:<table>:row:<primary key>` (NULL is a missing field, the column types are the
SQLite DDL's), each table a sorted set of its keys, with the lookups the reads need (the active queue, each
dataset's checks, the open issues per dataset and kind). What SQLite did in one `BEGIN IMMEDIATE` transaction is a
`WATCH` of the tables touched, the reads, and one `MULTI`/`EXEC` that runs again when someone else wrote meanwhile
(at most 25 times, with a jittered backoff; then the store reads as busy).
A caller that waits on a busy store catches `ops.BUSY_ERRORS` (a locked file, a Redis that does not answer).
`python -m RevenueChaser.rcdata.ops_copy` copies `ops.sqlite` into an empty namespace once: read-only on the file,
`--dry-run` counts per table and per open issue kind, a namespace with keys is refused unless `--force`, every row is
copied (the price issues stay), and every table is compared with the file afterwards (exit 1 on a difference).
With `RC_REDIS_URL` unset the app runs on `ops.sqlite` as below; that is the rollback, since the file is never
written while Redis is on.

**`ops.sqlite`** (SQLite WAL, `busy_timeout` 5000 ms on every open; `rcdata/ops.py` owns every statement;
ops schema version `3` (Redis keeps the same stamp in `<ns>:ops_schema`); `ensure` upgrades an older file in place, in one transaction, and refuses a file stamped
newer. Rolling back to an older checkout is `DELETE FROM ops_schema WHERE version = <n>` with every ops process
stopped: the older code runs on the newer tables, and the next upgrade leaves alone what is already there (a
history table that has `id`, a `job_runs2` that has `job` and `counts`). Version 3 (#136) added those two columns
by `ALTER`, so nothing is rewritten and the builder's rows keep their values):

| Table | Holds |
|---|---|
| `ops_schema` | The applied ops schema version |
| `build_requests` | The FIFO request queue (queued, running, done, failed, skipped) |
| `dataset_check` | One row per dataset run: changed, unchanged, failed or skipped; rows, added, removed, coverage, calls |
| `build_issues` | A row the builder could not use, with its key, until resolved |
| `job_runs2` | One row per run: a builder run (its `dataset`), a scheduled task (`job` = the registry name, `trigger` = `schedule`) or a dashboard job (`job` = its module, `trigger` = `dashboard`), with what the run did in `counts` (#136), the seconds per step in `counts["steps"]` and the chain id in `params["chain"]`. `ops.runs_between` and `ops.last_builder_run` read the builder's rows only (`dataset IS NOT NULL`); `ops.job_runs` reads the task and dashboard rows |
| `read_log` | Reads per day, process and table (counted in memory, written once a minute) |
| `uw_remaining` | UW's `x-uw-*` rate headers, sampled once a run: `x-uw-daily-req-count` as `daily_req_count`, `daily_remaining_computed` = our cap (`rcdata/caps.py`) minus it, the per-minute window, and `x-uw-token-req-limit` as sent (it is not the daily cap) |
| `provider_calls_daily` | Calls, failures and 429s per provider and New York day, written by the builder (every HTTP request it sends, retries included) **and**, since #166, by the dashboard's own provider clients through `core/providers/spend.py`. One row per provider and day, with no column for which of the two spent it: the quota is the account's. Its unit is the provider's unit in both halves — a request for `uw` and `sec`, a Yahoo read for `yfinance`. One call reaches the row once: the caller that keeps its own book owns it (`UWClient`'s observers, `yfinance.spend_owned_by_caller`; #1277). `RC_APP_SPEND=0` stops the app half |
| `provider_spend_daily` | What the **paid** writers cost, per provider, model and New York day (#166 item 3): `calls`, `tokens_in`, `tokens_out` and `usd_micros` (millionths of a USD, an integer so a day is a sum of exact numbers). Written by `core/llm_spend.py` after a paid answer, and read by its day cap, which refuses a paid call once the New York day's USD is spent. The money is a measured estimate until a per-token price is set in `core/llm_prices.py`, never the provider's invoice; tokens are recorded either way, so a day can be re-costed. `RC_LLM_SPEND=0` stops the writes and the gate. A counter, so it is not in the ops backup — see `docs/llm_spend.md` |
| `watchlist_option`, `watchlist_stock`, `watchlist_option_changes`, `watchlist_stock_changes` | The two watch lists (D2): ticker, added at, added by (the board), note; and their history, one row per add or remove with an `id` that keeps its order (version 2 added `id` and `note` to the history). No longer read or written since plan v5 step 7g (18 Nov 2026): the two lists are rows of `boards/watchlists.duckdb`; the tables stay until avo retires them |
| `dataset_schedule` | Cadence and pause state per dataset (Pause / Resume on Settings, PR 2) |
| `kind_automation` | The old file-level mode (Settings, 6 Nov 2026): kept, no longer read; `ops.copy_kind_modes` moved it onto each dataset (10 Nov 2026) |
| `switch` | The board's two switches (#527): `fetch` stops every fetch, build and RC timer task but the backups and the IBKR login check; `backup` stops the three backups. No row, or `off_at` NULL, is on |
| `dataset_automation` | One source's own automation (#527): `off`, `raw`, `parquet` or `full`; no row is `off`. The only mode the hourly pass reads (avo, 10 Nov 2026) |
| `request_log` | The per-request log (#715): one row a read (`kind` `read`), a need ask (`need`) and a provider call (`call`), with the time, the process, what was asked (a table, a need, or the provider and for an HTTP call the method and `host/path`, never the query, a header or a body), the dataset of a build, the answer (an HTTP status, an error's name, `sent`, `failed`) and the milliseconds of an HTTP call. Collected in memory and written with the read log's flush (once a minute and at exit); every write keeps only the newest 100,000 rows (`ops.REQUEST_LOG_KEEP`, about seven days). Not in any backup. `RC_REQUEST_LOG=0` turns it off. Read by `GET /api/settings/requests` |

**How long a run's rows are kept** (avo, 20 Nov 2026: "Bir yil yeterli ama her seyi tuttugundan emin ol, hangi
service yapacak bunu?"; review of 20 Nov 2026, finding R5): `job_runs2`, `build_requests` and `dataset_check` keep
**365 days** (`ops.RUN_ROWS_DAYS`), the same year homesrv keeps its own `ops.db job_runs` rows in
(home-server `docs/job-records.md`), which are the outer record of the same runs. The deleter is **the hourly
builder pass** (`ops.prune_runs`, called beside `ops.prune_issues` at the end of the due step), because it is the
writer of this store: no second writer and no new timer. It is best effort, so a busy store only means the next
hour prunes. Never deleted, whatever its age: a run that has not finished (`finished_at` NULL) and a request still
queued or running — the sweep closes those, not the clock — and, per dataset, the three check rows a board reads as
that dataset's state (the newest, the newest that is not `skipped`, the newest that built), so a dataset that last
built two years ago still reads true. `ops.oldest_run_rows` says how far the kept history reaches, and `RC_PRUNE_RUNS=0` stops the prune deleting anything without a revert (as `RC_REQUEST_LOG=0` stops the request log).

**Day caps** (`rcdata/caps.py`, master plan Order 5, rule 3: only the quotas a provider publishes per day, each with
its source): alphavantage 25 (its free tier, shared by the earnings calendar and the spine's
`alphavantage/listing_status`, #346); UW has none (owner-removed avo 2026-11-10). **Request windows**
(`caps.WINDOWS`, enforced by `base.Pacer`, one pacer per provider and run through `base.run_pacer`): sec 10 a second,
alpaca 200 a minute (Basic plan, historical), openfigi 250 a minute (keyed `/v3/mapping`; `/v3/search` 20 a minute,
no caller yet), finra 1,200 a minute (synchronous, per IP), yfinance 2,000 an hour (per IP, owner-marked avo
2026-11-02; Yahoo publishes none). A provider with no published limit (FRED, WRDS, SSGA, iShares, Vanguard, Nasdaq,
Invesco, ARK, Nasdaq Trader, the NYSE threshold file, IBKR's short-stock file and its gateway's 1-day bars) has no
number here; a pacer it keeps is politeness, not a provider limit. Neither table is the provider's own meter: UW's
`x-uw-*` headers are (`uw_remaining`).
a dataset whose providers have all reached their cap for the New York day gets a `skipped` check row and sends
nothing; inside a run every call checks its provider's cap first (one capped provider, FRED among the market series,
is a source failure and the others go on), and the counter is written every 25 calls, at the end, and on SIGTERM (systemd's stop, turned into a clean exit) or Ctrl-C; a keyless call is counted when it is sent, a UW call when its answer arrives (the UW client's observer), so a UW request cut off by a stop is not counted. Only the
builder's calls and the SPX history pull's (`module_spx_gamma/history_pull.py`, whose `max_calls` is held to what is
left of UW's day) are counted: the dashboard's other UW calls are not yet.

The keyless sources send one User-Agent with the contact from `RC_HTTP_CONTACT` (BLS refuses a client without
one; a 403 names the variable in the source's failure note).

**Running it.** `python -m RevenueChaser.rcdata.build run [--once] [--dataset X [--as-of D]]`,
`python -m RevenueChaser.rcdata.build backfill <dataset> --from <date>` and
`python -m RevenueChaser.rcdata.build attended` (the attended datasets' runner, below).

The store takes one writer at a time, so a start whose turn it is not **waits** for it: the gate (a job lock held
on a file it writes, another builder) and the builder lock are one loop. `--wait SECONDS` is how long; a person's
commands (`backfill`, `attended`, and `run --dataset X`) wait 1800 s by default and the hourly pass (`run` with no
dataset) waits none, because its timer brings the next pass anyway. It exits 3 only when the wait runs out, and
names the holder it is waiting for, again whenever that changes.

**One heavy dataset per pass (#812).** A dataset whose own run needs gigabytes of the machine's memory carries
`heavy = True` (`rcdata/build/base.py`, `heavy()`): today `sec/companyfacts`, `fundamentals/tables` and
`ownership/serve`. A batch claims at most one of them, and once a builder process has run one, every other heavy
request stays queued for the next pass, which starts with that process's memory given back; the pass prints one
line saying so. Measured on homesrv on 12 Nov 2026: a `sec/companyfacts` run reached 6.4-6.8 GB resident, and the
kernel OOM-killed the builder twice while it ran beside `fundamentals/tables` of its own kind — both kills with
`constraint=CONSTRAINT_NONE`, the whole machine, so the unit's `MemoryMax` was no protection. A dataset named on
the command line (`run --dataset X`, `backfill`) is a person's own run and keeps its turn whatever its weight.

`attended` refuses a runner with no terminal (`sys.stdin.isatty()`), because the WRDS login and the Duo push need a
person, and it says so and exits 0 when no attended request is queued rather than waiting for a turn it has no use
for. A person's `backfill <dataset>` and `run --dataset X` run **their own** request for an attended dataset
(`ops.next_request(by="cli")`), never one a page left for `attended`: that would spend their login and the WRDS day
cap on somebody else's params. For every other dataset an older queued request still goes first, as it always has.

**Which lock holds which off.** `core/job_lock.WRITES` says which store each writer family writes: `STORE` for the
old store's DuckDB files under `RC_STORE_DIR` (`raw`, `edgar`, `history`, the legacy `prices`) and `MARKET` for the
serving files and `ops.sqlite` under `RC_MARKET_DIR`, the builder's. The gate asks `blocking_holders(name)` —
`holders()` filtered to the families that can meet `name` on a file — so an old-store job (the Relatedness pilot, an
EDGAR sync, a nightly step) no longer makes the market builder wait, while a second builder still does. The grain is
the store and not the single file on purpose: a store is a superset of every family's real file set, so the map can
only refuse too much, never too little. A family with no entry writes `UNKNOWN` and holds everything off, which is
what keeps a new writer from slipping past the gate; `core/tests/test_job_lock_names_pl6a.py` fails when a
documented lock name has no entry.

**The builder as a service (PR 2).** Scheduled jobs run hourly unless avo says otherwise. The registry row
RC-Build (`core/automations.py`) runs `rcdata.build run --once` every hour at :05, with a 120 min limit, as the
systemd unit pair `life-rc-builder.service` / `.timer`. RC-Backup-Weekly runs `rcdata backup` every Sunday at
03:30, with a 60 min limit, as `life-rc-backup-weekly`: every step, into `RAW_STORE_BACKUP_DIR/weekly-v2/<step>/`,
the newest 4 copies of every file kept (avo, 8 Nov 2026; the section **Backups, restore and compaction** has the
steps). It replaced the nightly RC-Build-Backup (04:30, `--subdir market`) and the weekly RC-Build-Backup-Heavy
(`--subdir market-heavy`) on 9 Nov 2026. Run `rcdata backup --only <kind>` by hand before a deploy that touches a
DuckDB file: that copy is tagged manual and kept apart from the weekly ones. The unit pair is installed from the
home-server repository, not from the Settings page: installing it from both would give one unit name a user timer
and a system unit, each firing at 03:30 into the same folder. The old `market/` and `market-heavy/` folders are
not pruned by the weekly run (it only looks inside `weekly-v2/`); removing them is a deliberate step on avo's word.
Each pass first queues what is due (below), then takes the queued
requests in order: those, Fetch and Backfill from Settings, and the CLI's. It skips a paused dataset (`dataset_schedule.paused_at`, a `skipped: paused` check row,
no call). At start it fails a request a dead builder left running, with `lost: pid <n> gone` and a `failed` check
row — or, when the kernel's own log says it killed that pid for memory, with `lost: pid <n> killed by the kernel:
out of memory` (`core/oom.py`, #815: the journal is read by pid, bounded and never fatal, and `RC_OOM_JOURNAL=0`
turns the read off, which is what the test suite runs with). A request whose builder is still alive (a live `.rc-proc` marker of its pid in the market dir) is left
alone. A **dashboard job's** row (no dataset, trigger `dashboard`) is the one the sweep words differently
(#1239): its pid is the dashboard's, so it is closed `interrupted` with **no end time** rather than `failed` with
`builder died` — the end time the sweep could write is only its own clock, which turned four interrupted
`news_headlines` runs into 45-minute ones. A kernel OOM kill stays a failure, whoever ran it. Neither row writes an old `job_runs` row: the builder records its own `job_runs2` rows. The page and the routes only enqueue and read (`docs/data_retrieval.md`, "The
market-store builder's routes").

**Why a pass exited 1, and where that is written (#1119).** The hourly unit runs
`jobrun rc-builder -- python -m RevenueChaser.core.automations run RC-Build`, and `run_task` sends the builder's
whole stream to `<OUTPUT_DIR>/logs/RC-Build.log`. `jobrun` keeps only what its child printed, the **last line** of
it as the job's `message`, so a failed pass ends with one summary line on stdout: `pass failed: <reason>;
<reason>`, which carries every failed dataset with its step and exception, the due step's own failures (the one
reason that fails a pass with every dataset run ok), a store that stayed busy, and `not started: <holder>` for an
exit 3. `run_task` echoes a non-zero child as `<entry> exit <rc>: <its last line> (log: <path>)` on its own
stdout, so the jobrun log, the journal and ops.db all name the reason and the log that holds the rest. A failed
check row, the `job_runs2` error and the request's note name the step that raised first — `fetch`, `parquet`,
`backfill`, `build`, `check`, `swap` or `after_swap` (`RunContext.step`) — so an HTTP 504 a fetch got is told from
the same exception class out of a check read.

**Where a run's time went, and whose chain it is.** The same step names also time the run: `RunContext.step` stamps
its clock as it changes, and the seconds per step go into the run's `job_runs2.counts` as `{"steps": {"fetch": 4.0,
"build": 8.0, ...}}` (review of 20 Nov 2026, step 1; the builder path left that column empty, so no schema bump).
`start` is the time before the first named step. Every run row's `params` also carry a `chain`: the id of the run
that started this process (`RC_CHAIN_ID`, which `core/automations.run_task` sets to its own run id for the command
it starts), else the run's own id — so a task row and the dataset runs it caused read as one chain, and a run
nobody started is the root of its own.

**Known limits.** The idle gate is checked when the builder starts, not again before each request: a job that takes
a lock after that runs beside it (both write different files, so nothing breaks). Since the gate only asks about the
files the builder writes, that is now the same answer it would give later — only a second builder, or an undeclared
writer, can change it. DuckDB keeps a database instance per path inside a process, so a reader that holds
a connection open across overlapping requests can keep reading the build it opened while a newer one is swapped
in; `open_read` opens and closes per call so each request sees the file live at its start.

**What is due.** `run` without `--dataset` first queues every due dataset (`rcdata/build/due.py`, requested by
`schedule`), then drains the queue. The request goes in through `ops.enqueue_once`, whose check and insert are one
`BEGIN IMMEDIATE`, so a dataset with a request already queued or running gets no second one and two overlapping
passes queue it once. A dataset whose own automation (`ops.dataset_automation`, set per source on the Settings
Fetch/Health tab) is `off` — the default, no row — is not queued (the file-level `kind_automation` is no longer read,
avo 10 Nov 2026; `ops.copy_kind_modes` copied each kind's mode onto its datasets once at that deploy); `raw` and `parquet` queue the same partial run a Fetch's ▾
asks for, and that run's own `skipped` check then counts as the run for the cadence; `full` queues the whole run. A
person's Fetch is not touched by the setting. A paused dataset is not queued, and `dataset_schedule.next_due`, while later than today in New
York, holds a dataset until that day. `due.EARLIEST_NY_HOUR` holds one until its hour in New York: `wrds/prices` not
before 05:00 there, because the Compustat snapshot holds the session's bar only from the early morning
(WRDS stamps the product 3:40 a.m.; a 01:23 NY snapshot measured here was byte for byte the one before it)
and the dataset runs once a New York day, so an earlier pass would spend the day's run on a snapshot one
session short. Otherwise a dataset never built, or whose last check failed or was skipped,
is due. With cadence `daily` (the default, and what an unknown cadence reads as) it is due once its own
`default_as_of()` reaches the last check's `next_check_due`: the SPX slice after the next session is complete, the
market series (as-of today) on the first pass of each trading day. `weekly` and `monthly` are due 7 and 30 days after
the last check. A check with no `next_check_due` (past the calendar's table) is due once the as-of is newer. A
loop run (`run` without `--once`) does this once, at start. A due-step error (a due check that raised, `ops.sqlite`
locked or Redis not answering while queueing) is printed, the queue is still drained, and the pass exits 1. A second `--once` started while
one runs exits 3 on the `rcdata_build` lock before its due step. A failed or skipped check is retried every hourly
pass with no backoff: for the SPX slice at worst about 24 runs a day, each one UW call plus its retries. A pause
holds a dataset twice: `due.py` does not queue it, and `base.run_request` skips a request of it that was queued
anyway (PR #222).

**Backup.** `python -m RevenueChaser.rcdata backup` is the weekly job (RC-Backup-Weekly, Sunday 03:30). It writes
under `<RAW_STORE_BACKUP_DIR>/weekly-v2/<step>/` and refuses an empty `RAW_STORE_BACKUP_DIR` (exit 2, nothing
written). Its `duckdb` step copies each serving file from one open handle, verifies it by row count and
`sum(hash(row))` per table before the rename, and writes the sha256 of every copy to the step's `.manifest.json`,
the newest 4 kept per kind. Its `ops` step keeps only the needed tables (the watch lists and their changes,
`dataset_schedule`, `switch`, `kind_automation`, `dataset_automation`, `job_runs2`; the queue, the read log and the
counters are emptied in the copy) as `ops_<stamp>.sqlite`: on `ops.sqlite` through SQLite's online backup API, built
on local storage and copied once finished; on Redis a snapshot of every table read by one Lua script, filtered,
written into a new ops file in one transaction and compared with it row for row. It restores with
`ops_copy --sqlite <copy>` into an empty namespace (over a damaged one, delete its keys first,
`redis-cli --scan --pattern '<ns>:*' | xargs -r redis-cli del`, since `--force` keeps rows newer than the copy and
its check then fails). The raw files and the Parquet get no copy (avo, 8 Nov 2026: they are fetched again); the
exception is the `uw-raw` step, which keeps the Unusual Whales raw files and the SPX Gamma tree, since no provider
serves them again. `RC_MARKET_RAW_COPY_DIR` is no longer read. The other steps and the retention rule are in
**Backups, restore and compaction**.

### The identity spine (`spine.duckdb`, PR 5)

The second serving kind: who each SEC-file security is, its tier, CIK-CUSIP link, FIGI, GICS and index membership.
Built by the same builder, swapped the same way, read only through `rcdata.read`. Nothing here writes `raw.duckdb`;
the old identity tables there are untouched (D5: they are empty since 27 Oct).

| Dataset | Cadence | Source | Stages |
|---|---|---|---|
| `sec/company_tickers_exchange` | weekly | `www.sec.gov/files/company_tickers_exchange.json`, `If-Modified-Since` from the last copy (a 304 writes nothing) | `stg_sec`, `stg_sec_all` |
| `sec/company_tickers_mf` | weekly | `www.sec.gov/files/company_tickers_mf.json`, the same shape and the same `If-Modified-Since` fetch: the fund file, one row per series and share class | `stg_sec_mf` (the newest snapshot only: nothing reads the older ones, which stay in the Parquet tree) |
| `wrds/sec_idhist` | monthly, attended | `comp.company`, `comp.security`, `comp.sec_idhist`; between snapshots only the new listed CIKs | `stg_wrds_company`, `stg_wrds_security`, `stg_wrds_idhist` |
| `wrds/co_hgic` | monthly, attended | `comp.co_hgic`; Compustat Global `comp.g_company`, `comp.g_security` (ISIN lines) and the GICS code names `comp.r_giccd` | `stg_wrds_hgic`, `stg_wrds_g_company`, `stg_wrds_g_security`, `stg_wrds_giccd` |
| `wrds/idxcst_his` | monthly, attended | `comp.idxcst_his` for the S&P 500, 400 and 600 index keys | `stg_wrds_idx` |
| `openfigi/map` | weekly | OpenFIGI `/v3/mapping`, the CUSIPs not mapped yet (a CINS, a letter first, asked as `ID_CINS`), 250 requests a minute per key, one box-wide bucket shared with the dashboard's mapping (125 a minute each when it cannot be used). A second lane asks a `TICKER` job for every Tier A row of the live `security` table with no FIGI **or whose FIGI this lane itself gave** (`wanted_tickers`), with or without a CUSIP: either there is no CUSIP to ask with, or OpenFIGI did not answer for the one there is, and in both cases the type rules have no OpenFIGI security type to read. Measured on the live spine (homesrv, 2 Nov 2026 file read 4 Nov): 321 Tier A rows carry `security_type = 'unknown'` and none of them has a FIGI — 192 with no CUSIP and no gvkey, 129 with both. A `TICKER` answer expires after `ABSENT_RETRY_DAYS` whether it is 'present' or 'absent', unlike a CUSIP's: a ticker is not a stable key, a symbol can move to another issuer, and `figi_ticker_differs` cannot catch that because the asked and answered tickers are equal. So the lane re-asks its own 4 requests once every 90 days. Both lanes share the one 400-request budget, the CUSIPs first | `stg_figi`, `stg_figi_ticker` (the ticker lane, keyed by the ticker asked) |
| `funds/index_holdings` | daily | SSGA SPY/MDY/SPSM, iShares IVV/IJH/IJR (`latest-holdings.csv`), Vanguard VOO/IVOO/VIOO (daily creation basket), Nasdaq's nasdaq100 list and Invesco QQQ (both vote `ndx`); SSGA DIA (votes no index: `etf_class` reads it) | `stg_fund_lines`, `stg_fund_files` |
| `funds/etf_holdings` | daily | the SPDR workbooks of the 11 sector funds and XBI/XOP/XME/KRE/XHB/XRT/KIE, the iShares `latest-holdings.csv` of SOXX/IBB/ITA/ITB/IGV/IAK, and UW `/etfs/<t>/holdings` whole (no top-10 cap) for the 14 themes; on a day UW holds nothing for ARKQ, ARK's own holdings CSV (`ark-ARKQ.csv`, #339; AIRR, SKYY, WCLD and ROBO have no machine-readable issuer file) | none: serves `etf_holdings` itself |
| `alphavantage/listing_status` | weekly, keyed (builder service) | Alpha Vantage `LISTING_STATUS` (CSV, every US listing: `assetType` `ETF` or `Stock`, `ipoDate`), plus one `state=delisted` call whose non-CSV answer is one open `no_rows` build issue (resolved when it answers a list), never a failed run (#346); an active list under 3,000 ETF rows or under 80% of the last snapshot's fails and is not kept; a failed run is due again on the next New York day, not every hourly pass (`due.DAY_CAPPED`, the 25 a day it shares with the calendar), unless the day's raw file is held (a rerun then costs no call); the key is never stored or logged | `stg_av_listing` (the newest snapshot) |
| `sec/ncen` | monthly; every run (Fetch or backfill) holds the last 4 published quarters (a 404 is not yet, the walk goes one quarter further back; a held quarter is never asked again; one held but not typed is typed) | `www.sec.gov/files/dera/data/form-n-cen-data-sets/<YYYY>q<N>_ncen.zip`: `FUND_REPORTED_INFO.tsv` joined to `SECURITY_EXCHANGE.tsv` on `FUND_ID` (the series' name, ticker, fund-type flags, average net assets), `SUBMISSION.tsv` for each filing's CIK and dates (#346) | `stg_ncen` (each series' newest filing, every held quarter), `stg_ncen_filer` (every filer's CIK) |
| `spine/security` | daily | none: builds the served tables from the staging tables | the tables below except `fund` and `etf_holdings` |

The sources load their Parquet into TEMP tables of the build connection (never stored in the file), and all
but one stop there; `sec/company_tickers_mf` also serves `fund` from its own staging, because nothing derives
that table from anything. The kind's datasets build in `DATASETS` order, so `spine/security` builds last. Raw and Parquet follow the market store's
rules (`raw/<source>/<dataset>/...`, `parquet/<source>/<dataset>/...`; a raw file that exists is the resume marker).

**Attended (WRDS).** The three WRDS datasets need a person, and run in their own interpreter: `wrds` is not in the
builder's venv (on the server it has a venv of its own, because its pandas has no wheel for the builder's Python).
`rcdata/build/spine/wrds_worker.py` runs under `RC_WRDS_PYTHON` (that venv's python, set in the server's env file;
no default), one subprocess per builder process, so one WRDS connection: the login is that venv's
`wrds.Connection` (the user from `WRDS_USERNAME`, which is required, the password from that user's `~/.pgpass`; WRDS may ask
for a Duo push). The worker streams each answer in chunks to `<table>.jsonl.gz`; the builder types it in DuckDB.
The datasets are never due, and only a runner with a person at it runs one: the CLI's own request
(`requested_by` `cli`) or `python -m RevenueChaser.rcdata.build attended`. A request a page queued **waits in the
queue** for that runner — the route answers 202 with `waits_for`, the hourly builder leaves it there
(`ops.next_request(skip=base.attended_names())`) and `attended` claims nothing else
(`only=base.attended_names()`); the schedule queues none. So the CLI runs them:
`python -m RevenueChaser.rcdata.build backfill wrds/sec_idhist
wrds/co_hgic wrds/idxcst_his openfigi/map` starts one worker for the whole process. Run it in a foreground terminal:
the worker reads a WRDS prompt from `/dev/tty`, and a background job that reads its terminal is stopped (SIGTTIN).
A failed login, or no
interpreter at `RC_WRDS_PYTHON`, fails the run and keeps the last snapshot served. With `RC_WRDS_UNATTENDED=1` the
hourly builder runs them on their cadence, and a WRDS dataset whose last check `failed` **today** (New York) is not
due again before the next day (`due.WRDS_FAILED_WHY`): no hourly login retry, but at most one retry a day, so a
failure after the WRDS calls — a refused serving swap (#1115) — does not stop a daily dataset writing any check row
at all until a person notices. A person's run (Fetch or CLI) that does not fail clears it at once. `requested_by` `cli` is the CLI's
own mark: a panel header that says `cli` is recorded as `panel:cli`.

**Starts by hand.** Every spine dataset is not due until it has built once (a `changed` or `unchanged` check, from
the CLI or a Fetch), so the first hourly builder pass after a deploy calls no spine source before the attended
steps, and a first attempt that failed is not retried every hour.

**Served tables** (`spine` serving schema version `10`). Periods are `valid_from` inclusive, `valid_to` exclusive
(Compustat's inclusive ends are moved one day on), `valid_to` NULL while current.

| Table | Rows |
|---|---|
| `security` | one per ticker of the newest SEC file: CIK, name, exchange, `tier` and `tier_rule`, `security_type` and its source, the Compustat link (`gvkey`, `iid`, `link_source`), `cusip`, `isin` (derived), the FIGI fields, the current GICS codes, the bridge `sector` / `industry`, Compustat's company `sic` (spine 6), `country` and `country_source` (spine 10: where the business is, ISO 3166-1 alpha-2 — Compustat's own `comp.company.loc` where the row links to a company, else the SEC submissions header's business address; NULL is unknown, never "not US"), each with its source, and the SEC file's provenance |
| `security_history` | the SEC file's `cik`, `name`, `exchange` per ticker over the stored snapshots; Compustat's identifier history of the linked securities |
| `gics_history` | Compustat's GICS periods of the linked companies |
| `index_membership` | the fund-file members (source `funds/index_holdings`) and Compustat's constituents (source `wrds/idxcst_his`, the check) |
| `etf` | the ETF universe, one row per ticker (#346): Alpha Vantage's active `assetType=ETF` rows ∪ N-CEN's `IS_ETF=Y` series ∪ `security`'s `etp` rows, `source` `both` / `av` / `ncen` / `etp`. Every one is stored: no threshold and no `active` column (a board's thresholds are its settings, applied at read time). `is_etn` by a name rule (ETN, ETRACS, a dated note, a bank issuer's name), never for a row N-CEN calls an ETF: flagged, kept, not counted as ETFs. `is_cef` for a row the spine types `cef` (Alpha Vantage lists some closed-end funds as `ETF`, e.g. DHY; avo, 1 Nov 2026): flagged, kept, not counted as ETFs. From the series' newest N-CEN: `is_index`, `is_lev_inverse` (`IS_MULTI_INVERSE_INDEX`, or true by a name rule: 2x-5x, -1x, ProShares Ultra/Short, inverse, leveraged, a daily long/short/bull/bear fund), `is_money_market`, `is_fund_of_funds` (NULL, not false, for a row N-CEN does not hold) and `aum` with `aum_source` (spine 9, #357): `shares_x_close`, the fund's own Compustat shares (`cshoc`) times its close on `aum_as_of`, else `ncen_series`, N-CEN's `DAILY_AVG_NET_ASSETS` (else `MONTHLY_AVG_NET_ASSETS`), an average over the filing's fiscal year for the whole series, `aum_as_of` that year's end); `inception` from AV's `ipoDate`; `is_option_income` by a name rule (option income, covered call, buffer, autocallable, defined or target outcome; premium income, buy-write, put-write, option strategy, managed floor, defined or structured protection); `asset_class` `commodity` for an `etp` row that is no note and whose CIK files no N-CEN at all (a 1933-Act trust: GLD, SLV, USO; a UIT such as SPY files one as an S-6 registrant), applied only once `sec/ncen` holds four quarters. `adv_usd_20d` is the average of the fund's newest 20 **priced Compustat** sessions' dollar volume (`prccd` times `cshtrd`, US dollars only), read from the raw `comp.secd` Parquet — not from the live `prices.duckdb`, which keeps only today's universe, so a fund that is not stored yet could never earn a volume (#357, avo's decision B of 5 Nov 2026). The fund's series is the one whose own newest priced day carries the ticker (Compustat's `tic`, dots read as dashes) within 14 days of Compustat's newest priced day, the larger average winning where two live series claim one ticker; only a fund with all 20 sessions gets a number — fewer sessions leave it NULL, so `my_universe` reports the fund undecided instead of judging it on a short window. Still NULL: `focus`, `country`, `expense_ratio` and every other `asset_class` (later PRs). Built by `spine/security` (`rcdata/build/spine/etf.py`), after `security` |
| `my_universe` | one row per ticker of **the names RevenueChaser keeps** (avo, 4 Nov 2026): `kind` (`stock` / `etf`), the spine's own `security_type`, `name`, `exchange`, `tier`, the `rule` that let it in (`stock`, `etf_aum`, `etf_adv`, `job_list`, `manual`) with the `reason` behind it, the ETF numbers that rule read (`aum`, `aum_as_of`, `adv_usd_20d`), `entered_on` (the day it first appeared; kept from the live row, not reset by a rebuild) and the source row's provenance. The rule lives in `rcdata/build/spine/my_universe_rules.json` and avo's hand-written adds and removes in `my_universe_manual.json`, both owned by him and changed only by a pull request: a stock is a `security` row of tier A or D typed common, ADR, REIT or MLP (no royalty trust, no BDC, no non-SEC bank; an untyped name stays out, and tier B and C never enter); an ETF is an `etf` row that is no ETN and no CEF with `aum` at or over 100M USD, or `adv_usd_20d` (from Compustat's raw `comp.secd`, #357) at or over 10M USD; either number is enough (avo, 18 Nov 2026; before that a known AUM under the floor was the answer whatever the volume). A fund on the Sector Rotation map list or the industry-group list that those two tests left out enters as `job_list` (rule E, avo, 18 Nov 2026); the rule only adds, so a fund taken off a list stays until a remove line takes it out. A delisted name never enters — neither source holds one. The spine itself stays unfiltered; this table is what a later store is built from. Built by `spine/security` (`rcdata/build/spine/my_universe.py`), after `security` and `etf` |
| `my_universe_changes` | who entered or left the list, when and why: `ticker`, `kind`, `change` (`entered` / `left`), `reason`, `as_of` (the build day) and `run_id`. A leaver's `reason` names the rule it held under, or, where `my_universe_manual.json` took it out, says so with avo's own reason — his line is the one case where the rules were not applied at all (#444). **Append-only across builds:** the live file's rows are carried forward and today's diff against the live `my_universe` is appended, so the Data settings board shows what changed without keeping its own history |
| `etf_class` | one row per ETF of `my_universe` (#1031, avo's decisions of 17 Nov 2026): what the fund is, read from its newest Form N-PORT report in the `sec/nport` Parquet (not from the served `etf_holdings_nport`, which keeps only spine-CUSIP lines). `primary_class` (`sector`, `industry_group`, `industry`, `sub_industry`, `country`, `international`, `broad`, `broad_global`, `diversified`, `low_coverage`, `bond`, `cash`, `commodity`, `crypto`, `real_estate`, `fund_of_funds`, `multi_asset`, `preferred`, `synthetic`, `unclassified`) with its `code` (the GICS code or the country), `label` and a written `reason`; `structure` (`plain`, `leveraged`, `option_income`, `synthetic`), `is_synthetic` and `hidden_by_default` (a board leaves these out unless its filter is on); `asset_class`, `geo`, `country`; `equity_pct` (of net assets) and `coded_pct` (the equity that has a GICS code); the largest `gsector`, `ggroup`, `gind`, `gsubind` each with its share of the coded equity; `benchmark_distance` (sector mix against IVV); `manual` (a line of `etf_class_manual.json` corrected it); `source` (dataset and quarter), `as_of` (the report's day), `since` (the build day the label first held). A fund with no N-PORT report (a unit trust: SPY, MDY, DIA) is read from its issuer's daily file in `funds/index_holdings`, and `source` names that file's day; with neither it is `unclassified` and says why: a label is never typed by hand. A holding the spine does not hold (a foreign share) is coded by its ISIN from Compustat Global (`wrds/co_hgic`), and a GICS code's `label` is the source's name (`comp.r_giccd`). With no N-PORT Parquet in a build the live rows are carried forward. In `serving.SHRINKS_BY_CHOICE`, because its row count follows `my_universe` |
| `etf_class_changes` | which fund's label entered, changed or left, when and why: `ticker`, `change` (`entered` / `changed` / `left`), `was`, `now`, `reason`, `as_of` (the build day), `run_id`. Append-only across builds, like `my_universe_changes`. A fund keeps its live label while it still holds with every cut 10 points lower, so a fund on a cut does not write a row every quarter |
| `etf_theme` | the ETF theme list (spine 8, avo's G of 18 Nov 2026): one row per fund of the theme section of avo's `map` watch list whose `etf_class` label names no GICS branch (`diversified`, `international`): `ticker`, `theme` (the fund's registered name, the issuer's own words) with `theme_source`, the fund's `primary_class` and `geo`, `as_of` (the report the label was read from), `run_id`. A themed fund that sits in one branch (SMH, KRE, XBI) is described by its `etf_class` row and is not here |
| `etf_holdings` | every holding of each ETF of `funds/etf_holdings`, per day: `as_of`, `etf`, `provider`, `ticker`, `cusip`, `name`, `weight` (the relatedness peer sets' source) |
| `fund` | one per share class of the newest SEC fund file: `cik`, `series_id`, `class_id`, the normalised `ticker` and the file's `ticker_raw`, `is_etf` with the `is_etf_source` that decided it, and the file's provenance. `security`'s counterpart: an ETF is a series of a trust, so it has no row of its own in the equity file (the ETF rule, and the two exceptions, below). The kind's only source-owned served table, and the one no other spine dataset feeds (`feeds`, #326), so `allow_shrink` relaxes it for `sec/company_tickers_mf` alone: an empty `fund` refuses the swap for every spine dataset until that one is run, which is the ordinary way out |

**The ETF rule** (`rcdata/build/spine/sec_tickers_mf.py`). The fund file carries no "this is an ETF" field, so
`is_etf` is a rule and says so in `is_etf_source` (`length_rule_v1`): a symbol of five letters ending in X is
the classic open-end mutual-fund share class and is not an ETF. Measured 30 Oct 2026 over the file's 28,614
rows: 22,797 such share classes, leaving 5,811 ETF candidates — the same order as a third-party count of US
ETFs (5,394), which is the only corroboration there is. The rule reads the **normalised** ticker, because 25 of
the symbols are written lower case. It is wrong in both directions, and only one of them is visible to a reader filtering on
`is_etf`: a **fund** whose symbol is not five letters ending in X is in the ETF set (`AAAX`,
`QINSQ`), and an **ETF** whose symbol is five letters ending in X is outside it and so outside
every filter — the error to weigh when the second signal (the ticker trading on an exchange, or
the N-CEN ETF field) is chosen. A symbol that does not parse (`(NWAGX)`, `n/a`, the empty string) is dropped and counted in the
run's note.

**The ETF universe is `etf`** (#346). Before it, a reader took `fund` plus `security`'s `etp` rows: SPY is a unit
investment trust and GLD a commodity trust, so neither has a series and a class in the fund file, and both are rows
of the equity file (GLD, SLV and USO are its `etp` rows). `etf` carries those `etp` rows itself, so a reader after
the ETF universe reads `etf`, never `fund` alone.

**A shrinking list is not a broken build.** `my_universe` is in `rcdata/serving.SHRINKS_BY_CHOICE`: the swap guard's
1 % shrink limit does not apply to it, because a smaller list is the intended outcome of avo editing the rules or
removing a name by hand, and a request-wide `allow_shrink` would relax every other table of the kind with it. The
"a table may not empty" guard still applies — a universe of no names is a bug whoever edited what. And because that
exemption is the only swap-time guard the table had, `my_universe.py` guards the names that entered on their dollar
volume itself, **per fund**: `adv_usd_20d` is NULL for *unknown* and never for zero, so a fund of the live list
that the rule can no longer measure — no `aum` **and** no `adv_usd_20d` today, the case where the rule would in
fact have read the volume — is held on its live row, with no `left` row, and every held fund the manual file did
not then remove is one `my_universe_etf_volume_lost` issue (avo's decision B, #424). A fund N-CEN now prices under
the floor is answered by that AUM and leaves. A volume that is present and under the floor is the
rule's answer and that fund leaves as before. When no `etf` row has a volume at all the source itself is gone and
the build is refused outright: a missing or unreadable Compustat `secd` Parquet is an input outage, not a change of
the rules. The check is per fund because a table-wide one masked the loss: IBIT is on Nasdaq and GLD, SLV, SPY and
DIA are not, so IBIT's volume alone passed it while the Arca funds were dropped.
A live `etf_adv` fund the `etf` table holds **no row for at all** today is held the same way, per fund, and every
such fund the manual file did not then remove is one `my_universe_etf_row_gone` issue (avo's decision A, #432):
`etf` is the union of three sources (Alpha Vantage's listing, N-CEN's `IS_ETF` series, the spine's `etp` rows), so a
fund one of them alone carries — GLD, SLV, USO and the bitcoin trusts file no N-CEN — is missing from the whole
table when that source comes back short, and the `left` row that followed was irreversible. The check asks the whole
`etf` table, so a fund whose row is there and is today flagged an ETN or a CEF is the rule's answer and leaves; an
`etf_aum` fund that vanished leaves too, because that is the AUM source's own story.

**Tier rule v3** (`rcdata/build/spine/tier.py`): A = listed on Nasdaq, NYSE or Cboe and not an excluded type
(`core.identity.types.EXCLUDED_TYPES`; an untyped name stays); B = listed, excluded type; C = OTC, in the SEC file or
(v3, #962) by the linked Compustat security's current `exchg` 19 while the SEC file still names an exchange; D = no
exchange.
So Tier A is not every listed name: listed is A and B. The type comes from `core.identity.types.classify_type` over
OpenFIGI's types and the name (no SEC submissions); a name those leave untyped takes, in order, its ticker's type (a
warrant, unit or right: `-WT`, `-UN`, `-RI`, or a Nasdaq fifth letter W, U, R; a right is typed warrant), Compustat's
`tpci` of its linked security (`0` common, `F` ADR, `%` ETF; others type nothing), then `etp` from its SEC name (ETF
or ETN, a commodity, currency or crypto pool, a pool sponsor's trust, a bank that issues ETNs) or from a Cboe listing;
those two never type the issuers' or Cboe's own shares (JPM, DB, BMO, CBOE). v2 (2026-10-28) types 288 of the 480 Tier A names the first live build left with no Compustat link.

**The link.** CIK -> Compustat companies -> the security whose `tic` is the SEC ticker (`BRK.B` -> `BRK-B`), else
the only active US security of a CIK with one SEC ticker; otherwise no link and no CUSIP.

**Membership** (working choice): a fund file counts on a day when it parsed and holds at least its index's floor;
a name is a member when more than half of the counting files hold it. Each day's lines are matched against that
day's SEC snapshot (the newest on or before it), by CUSIP, then by ticker, then by the ticker without its class
dash; a ticker match is refused when the line's CUSIP is known (Compustat) to belong to another security. A line
the day's snapshot does not match is looked for in the snapshots of the next 21 days (the weekly file's lag and one
missed week), but only by a ticker the SEC
file had not held by that day (a new joiner, a rename the files write first) or held under the same CIK (a name the
file dropped and took back): a later row of a ticker under another CIK is its next owner. A member that later leaves the SEC file keeps its periods,
and a period's CIK is that of the snapshot its last day's lines matched. Compustat constituents get a ticker only through the link (never Compustat's current `tic` alone).

**Build issues** live in `ops.sqlite` `build_issues` (D6), dataset `spine/security`, synced after each swap
(`ops.sync_issues`: found again stays open, gone is resolved). Kinds: `source_never_run`, `tier_a_no_wrds_link`,
`tier_a_no_cusip`, `tier_a_no_figi`, `tier_a_no_figi_by_ticker` (a Tier A name with no FIGI from either
lane opens exactly one of these two, so `issues_open` never counts it twice: the name holds a CUSIP and the
`tier_a_no_figi` message names both lanes' states, or it holds none and the `_by_ticker` kind takes it),
`figi_by_ticker_with_cusip` (the CUSIP, and the ISIN derived from it, came from Compustat while the FIGI came
from a `TICKER` job, because OpenFIGI did not answer for that CUSIP; nothing checks the two name the same
security, so the row is named rather than trusted silently), `tier_a_no_gics`, `figi_ticker_differs`,
`cusip_shared`, `cik_many_gvkeys`,
`index_key_empty`, `fund_file_short`, `member_not_in_sec`, `member_not_tier_a`, `membership_files_disagree`, `membership_wrds_differs`, `etf_class_manual_unknown` (a line of `etf_class_manual.json` naming a ticker that is no ETF of `my_universe`), `my_universe_etf_undecided` (the funds with no AUM and no 20-session dollar volume yet **that the manual file says nothing about** — it is built after the manual adds and removes, #444 — one issue naming them, keyed `etf`; nothing is reported before N-CEN has answered at all), `my_universe_etf_volume_lost` (the live list's `etf_adv` funds Compustat gives no volume for today, held on their live numbers instead of dropped, one issue naming them, keyed `etf`), `my_universe_etf_row_gone` (the live list's `etf_adv` funds the `etf` table holds no row for at all today, held on their live numbers instead of dropped, one issue naming them, keyed `etf`), `my_universe_manual_unknown` (a manual line naming a ticker the spine does not hold), `my_universe_both_kinds` (the stock rule and the ETF rule both claim a ticker; kept as the stock), `my_universe_list_unknown` (the funds on the map list or the industry-group list that the `etf` table does not hold, one issue naming them, keyed `etf`).
A rule needing a source that has never run is not applied; that source is one `source_never_run` row.
`index_key_empty`: an index key with no `comp.idxcst_his` rows in a run that answered rows for another.
A WRDS raw value its schema cannot type (a DATE or INTEGER column) is typed to null and recorded as a `value_untyped` issue of that WRDS dataset, keyed `<table>.<column>`, with the row count; it is a one-off row of its run, not synced.

**The bridge (D4).** Views in `spine.duckdb` under the old names, with the old columns in the old order:
`equity_universe` (the listed rows, Tier A and B), `security_master`, `index_constituents` (current fund-file
members, plus `nasdaq` = listed on Nasdaq; no `dow`) and `index_tickers`. Market cap, price, quote and SIC are
NULL: the spine holds no market data.

**Reading it.** `rcdata.read.universe(tiers=("A",))`, `rcdata.read.identity(tickers)`, `rcdata.read.membership(index,
on, source)`. `core/universe.py` and `core/index_registry.py` read only them since #642 PR 6 (market cap and price
from the prices store, `rcdata.read.latest_market`, each from the old `equity_universe` by ticker where the prices
store has none; quote from the old table; a spine name left untyped takes `security_master`'s type; the index badges
and the filter's index list from the current fund-file members, `nasdaq` from the listed names on Nasdaq, no Dow
Jones). `RC_UNIVERSE_SOURCE` and the old-store universe path are gone: no spine file, or a last `spine/security`
build that failed, gives the boards no names, and the Settings Identity card says why ("Boards read").

### The prices store (`prices.duckdb` in `RC_MARKET_DIR`, PR 7)

The third serving kind: daily bars, corporate actions, the adjustment factors, shares per class, float and market cap,
keyed on the security. Not the old `.store/prices.duckdb` (#156, `RC_STORE_DIR`), which the boards still read until
they move (G3); the two files share a name and nothing else. Built by the same builder, swapped the same way, read
only through `rcdata.read`.

| Dataset | Cadence | Source | Stages |
|---|---|---|---|
| `alpaca/bars` | daily | **the tester, never served** (2 Nov 2026 master plan, item 3): Alpaca `/v2/stocks/bars`, `timeframe=1Day`, feed `sip`, `adjustment` `raw` and `all`, 100 symbols a request, the last 5 complete sessions re-read (the `all` window one session earlier); the daily run asks only the names `prices.tester` finds at risk (a split or `ajexdi` change, an empty `trfd`, a dividend or spin-off going ex, an open `price_gap_no_action`, an exchange move, a close under $5, over the last 10 days); a named request asks what it names | `stg_alpaca_bars`, `stg_alpaca_bar_revisions` |
| `alpaca/corporate_actions` | daily | Alpaca `/v1/corporate-actions`, every symbol, processed from 14 days back to 90 days ahead (a cash dividend is processed on its payable date, weeks after it goes ex; an action is applied once its ex-date has a session, one with no ratio or amount is an issue at once) | `stg_alpaca_actions` |
| `sec/cover_shares` | daily | The SEC daily master index (10-K, 10-Q, 20-F, 40-F and their -T forms of the last 7 days), each filer's submissions file, the filing's inline-XBRL instance: `dei:EntityCommonStockSharesOutstanding` per class member; and, with no request, the cover count history of `sec/companyfacts`'s stored raw (`parquet/sec/cover_shares_dei/cik=<CIK>/`) | `stg_sec_cover`, `stg_sec_dei` |
| `yfinance/float` | daily | Yahoo `floatShares` (`yfinance` `get_info`), 400 Tier A names a run, 1.5 s apart and 2,000 an hour, stopped by a 401 or 429 | `stg_yahoo_float` |
| `uw/total_float` | daily | Unusual Whales `/short_screener` `total_float`, every Tier A name (the client's batching) | `stg_uw_float` |
| `wrds/prices` | daily (avo, 3 Nov 2026), attended unless `RC_WRDS_UNATTENDED=1` | Compustat `comp.secd` from 1983 (its earliest priced day, 1983-12-30) on exchg 11, 12, 14, 17, 21 (NYSE, NYSE American, Nasdaq, NYSE Arca, BATS BZX) with the row's dividend columns, and `comp.sec_divid` per year, CRSP `crsp.dsf_v2` per year to 2025 (stocks and ETFs, from 1983), `comp.sec_split` and `crsp_a_stock.stkdelists` whole; no CCM link (`wrds_history.PERMNO_LINK`) | `stg_wrds_secd`, `stg_wrds_dsf`, `stg_wrds_divid`, `stg_wrds_split`, `stg_wrds_revisions` |
| `prices/serving` | daily | none: builds the served tables from the staging tables; the history is Compustat's (priority 1), CRSP's only on its fallback days, never Alpaca's | the tables below |

**A stored `comp.secd` row is a session only when it carries a price (#423).** Compustat ships its year partition
with its own forward-dated calendar placeholders: measured read-only on 5 Nov 2026, 1,128 rows over 929 tickers,
`datadate` from 2026-11-03 to 2026-12-31, `prccd` and `cshtrd` NULL. Nothing but the NULL price marks them apart, so
`max(datadate)` over the raw partitions answers up to two months past the last real session — and does it again with
every new year partition. The stored file stays a faithful copy of what WRDS answered; the rule lives in
`rcdata/build/secd.py` instead: `secd.PRICED` / `secd.PRICED_USD` is the filter every reader uses, and
`secd.last_session()` is the day a freshness check, a "data as of" label or a window bound must ask for.

**The one primary source (avo, 3 Nov 2026: the whole history and what follows from Compustat).** A served bar is
Compustat's `comp.secd` bar, a priced day up to today on which the security was on exchg 11, 12, 14, 17 or 21 by
`comp.sec_idhist`'s dated EXCHG periods (`SECD_LISTED`; `secd.exchg` is today's code; before 1998 the first known
exchange), from 1983-12-30 (its earliest priced day,
`wrds_history.COMPUSTAT_START`), with its splits from `comp.sec_split` and, where that has none, from a change of
`ajexdi` within one answer (`AJEXDI_SPLITS`), and its dividends from the secd row itself (`div`, `divd`, `divsp`;
`comp.sec_divid` only where the row has none). Its return (`COMP_RETURN`, flagged by `COMP_RETURN_BASIS` and counted
in the build note) is `prccd / ajexdi * trfd` over the previous session's (homesrv: p95 0.000055 % from CRSP's
`dlyret` on dividend days, against 1.5 % for the price alone), and on a day without `trfd` the price plus that day's
cash distribution: an empty `trfd` means no distribution (homesrv, 3 Nov 2026: `trfd` empty on 26.88 % of the priced
rows of 2010-2025; there the price-only return matches CRSP's `dlyret` within 0.01 % on 97.69 % of 4,069,937 matched
days, as the formula does on its rows). **CRSP serves no bar at all** (avo, 4 Nov 2026): the store holds today's
universe, so no delisted name is in it and the earlier narrow fallback — a delisted security's final `dsf_v2` days,
its `stkdelists` delisting bar, and the whole history of a security Compustat never priced — has no name left to
serve. `crsp_a_stock.stkdelists` is still fetched and nothing in the serving build reads it, and
`wrds_price_diff`'s `crsp_delisting_returns` row is gone with it. CRSP is the cross-check: of every Compustat close (`crsp_vs_compustat`) and
of the splits (`CRSP_SPLITS`, `dlycumfacpr` steps): one no Compustat or Alpaca split is within 3 days of is applied
nowhere and is a `split_only_crsp` issue, and the gap it explains is no `price_gap_no_action` (Order 4: 1,167 such
issues on homesrv, 22 Tier A; Compustat's `ajexdi` now carries most of those splits). Because it stands in for the gap,
the issue names the served close's move across its ex-date, and a jump of 1.9 times or more says *Compustat lacks
this split* (the served history is wrong there); the build note `split_only_crsp: ...` counts those first. A CRSP
split with no served bar of the security on both sides within 7 days touches no served bar: counted in the note, not
named (review M6 of #382: build 4 named 1,056 with no way to tell the two apart). **Alpaca is the tester**: its
bars write `alpaca_close_differs` and `adjustment_differs` issues, compared in every serving build, so a session is
checked again once Compustat's update brings it.

**The universe** (`rcdata/build/prices/universe.py`, the one place that rule lives; avo, 4 Nov 2026). What the
store keeps, and what the daily fetches ask for, is **today's universe**: the spine's `my_universe` table when it
has one (avo's own list, 7,363 tickers, which the spine's build resolves from its rule files and his by-hand
additions and removals), and until that table exists the rule itself — the `universe.STOCK_TIERS` names (Tier A,
listed on Nasdaq, NYSE or Cboe, and Tier D, no exchange in the SEC file) of a `universe.STOCK_TYPES` type (ordinary
shares, ADRs, REITs, MLPs; 5,349 on 4 Nov 2026) and the spine's ETFs with `aum` at or over
`universe.ETF_MIN_AUM` = 100 million (2,071; leveraged and inverse funds in, ETNs, closed-end funds and an unknown
AUM out). A SPAC, warrant, unit, right, preferred or closed-end fund is out, and so are a BDC, a royalty trust, a
non-SEC bank (each of them an operating type the identity master keeps, but not one the store keeps) and a Tier A
name with no type yet. Tier B (a listed excluded type) and Tier C (over the counter) are out whatever the type
says. One part of avo's approved rule is **not** in this module: he keeps an ETF whose AUM is unknown when its
20-day average daily dollar volume is at or over 10 million, and that branch is the `my_universe` build's
(`spine/my_universe.py`, #417), which reads the volume from Compustat's **raw** `comp.secd` Parquet
(`spine/etf.adv_usd_20d`, #421), not from this store: a store cut to the universe holds no bar of a fund outside it,
so a new fund could never earn a volume here (review M4 of #382, avo's decision B of 5 Nov 2026). **Nothing
delisted is in it**, because the list is read from the live spine. Kept beside it
(`universe.kept_beside`): the benchmarks (SPY, DIA, QQQ), the rotation ETFs and the stock watch list, which the
boards read from the store, and the other listed classes of a universe issuer that the SEC covers name
(`cover_classes`, Berkshire's A shares). The serving build turns that list into the `_universe` table before
anything else and filters Alpaca's, Compustat's and CRSP's rows through it, so nothing outside it is written. A
universe ETF the spine cannot route to Compustat (no `security` row, or one without a `gvkey`) is found by the ticker
its Compustat series carries on that series' newest priced day, if that day is within
`universe.ETF_TIC_RECENT_DAYS` (14) of Compustat's newest one; a series the spine holds under another ticker stays
out, and a stock is never linked this way (review M3 of #382: Alpaca serves no bar, so such a fund had none). **One
series per ticker** is stored, the one priced most recently, then the larger dollar volume; more than one is the
`universe_etf_ticker_shared` issue (review D1 of #420). A fund is a ticker of the spine's `etf` table or one
`my_universe` holds as an `etf`, and tickers compare upper case with `-` for `.` (D5, NIT 2). The
whole-tier rule it replaced (`DEFAULT_TIERS = ("A", "B")`, 7,670 names, avo 30 Oct 2026) carried every SPAC,
warrant, preferred and untyped name with it and still missed the funds the spine's `security` table does not hold.
The acceptance "Tier A sessions complete >= 99%" is the bars
dataset's coverage: Tier A names with a bar on the asked session over the Tier A names asked, against that real
count; Tier B's own coverage is reported beside it and is not part of the acceptance, because an OTC or fund name
may have no Alpaca bar.

**Survivorship bias: the store is biased on purpose, and it is reversible (#441).** Because the universe is read
from the live spine, the store holds no row of a company that delisted in the past. avo was told and accepted it on
5 Nov 2026 ("Bu senin tercihin ise sorun yok"), so this is a property of the store, not a bug to fix.

- **Harmless for everything the boards show:** the history of today's names, co-movement and relatedness between
  today's names, the charts. The missing names are not part of the question those answer.
- **Harmful and silent for anything measuring the past:** a backtest, AG-score validation, a forecast-ledger hit
  rate. The bad outcomes of companies that died are absent, so a number computed from this store comes out better
  than reality, with nothing warning about it.
- **The delisted history is already on disk**, because the filter is applied only in the serving build
  (`_universe`, `rcdata/build/prices/serving.py`) and **the WRDS fetch has no universe filter at all**:
  `crsp.dsf_v2` is asked with a date range alone (every share type), `comp.secd` by exchange — today's code or any
  dated `comp.sec_idhist` EXCHG period, so a name that was once listed is still fetched — and CRSP's delisting
  table `crsp_a_stock.stkdelists` whole (`rcdata/build/prices/wrds_history.py`, `TABLES`). All of it is under
  `parquet/wrds/prices/<table>/`.
- **So, when it matters:** read that Parquet, or build a separate research file from it, and say in the issue or doc
  that the measurement used it. **Never widen the served universe for a one-off measurement** — no WRDS call and no
  change to the served store is needed, and a widened store would change what every board reads.

**A request's own scope (#265).** A queued request carries what it asks for, so a fill does not mean editing the
code: `{"tiers": ["A", "B"], "names": ["KO"], "include": ["anchors"], "mode": "backfill", "from": ..., "to": ...}`.
A request that names nothing is the daily run: today's universe, the benchmarks and rotation ETFs, the stock watch
list and the Tier A issuers' other listed classes. A request that names `tiers` keeps the tier reading, because
that is what it asked for. **A request that names something gets only what it names**
(avo's Karar 82, 30 Oct 2026): its `tiers`, its `names` (a name the spine does not hold is asked under
`sym:<ticker>`), and whichever of the three groups it lists in `include` — `anchors` (the benchmarks and the
rotation ETFs), `watchlist`, `cover_classes`. So `tiers=["B"]` is 2,015 names, not 2,015 plus every Tier A
issuer's other class plus about 70 ETFs. A narrowed run leaves `coverage` empty on its check row and says so in a
note, because the acceptance is read off that number and a one-name fill is not the universe's share. `rcdata.build.base.parse_scope` is the one reader of it and
`RunContext.scope` is how a dataset sees it; the Fetch route (`POST /api/build/request/<dataset>`) takes the same
keys from its JSON body or query string. Three datasets narrow their fetch to it today: `alpaca/bars` (tiers and
names), `yfinance/float` and `uw/total_float` (a name list; otherwise the scope's Tier A names, because a fund has
no float). Every other dataset asks its names elsewhere (the SEC's daily index, a whole-market file, a calendar
day) and records `scope ignored: ...` on its check row rather than fetching something else;
`rcdata/tests/test_build_scope.py` names each one with the reason.

**The security id** (`rcdata/build/prices/__init__.py`, `security_id`): `<gvkey>:<iid>` when the spine links the name
to Compustat, else `figi:<composite FIGI>`, else `cik:<CIK>:<TICKER>`, else `sym:<TICKER>`; a WRDS bar with no spine
row keeps `<gvkey>:<iid>`. A CRSP PERMNO takes, with no CCM link, the spine security of its CUSIP (CRSP's 8 characters against the spine's first 8), else of its ticker on CRSP's last day when no other PERMNO had it, else the Compustat `<gvkey>:<iid>` of its CUSIP, else `permno:<PERMNO>` (`wrds_history.PERMNO_LINK`, with its measured coverage). A source row stores its provider's facts and the
id of its fetch day; the build resolves each row against the live spine on (ticker, CIK), so a spine change re-keys
the history at the next build without a refetch.

**Bars.** A bar is keyed on the New York session date (`core/calendar.py`; Alpaca stamps a daily bar at midnight New
York, in UTC), and only a complete session is stored (16:30 New York or later). A row that is no session, outside the
calendar's years, outside the asked window or fetched before its session was complete is a run issue, not a row. The
re-read keeps every changed open, high, low, close or volume of a stored bar as a revision (`price_revision`). A
Compustat row with no price (`prccd` NULL; homesrv held 1,128 such rows dated after the newest session, 5 Nov 2026) is
no bar, no newest day and no revision when it later gets a price or goes. Bars are stored **nominal**; the factors are
applied on read.

**Served tables** (`prices` serving schema version `1`). Periods are `valid_from` inclusive, `valid_to` exclusive.

| Table | Rows |
|---|---|
| `price_bar_daily` | one nominal bar per security and session (key `security_id`, `session_date`): Compustat's (only USD rows), never CRSP's and never Alpaca's, and the S&P 500 level from the market store's Cboe Parquet (`index:SPX`); `shares_outstanding` is a WRDS bar's own (`cshoc`) |
| `corporate_action` | Alpaca's actions of the store's securities (resolved to the security that traded under the action's symbol nearest its date, else the spine's ticker) and Compustat's splits (`comp.sec_split`, else an `ajexdi` change) and dividends (the `comp.secd` row's, else `comp.sec_divid`'s) where Alpaca has none of that kind that day; CRSP's `dlycumfacpr` splits are a cross-check, never applied; `ratio` new over old shares, `amount` per share, `applied` and `not_applied_why` |
| `adj_factor` | per security, the periods between two ex-dates and their `split_factor` (the product of `1 / ratio` of every later split and stock dividend) and `tr_factor` (that, times `1 - amount / close before the ex-date` of every later cash dividend, the close at most 7 days before) |
| `shares_class` | `outstanding` per class from the SEC covers (an unlisted class keyed `class:<CIK>:<member>`, and so is an ADR issuer's count of ordinary shares) and, for a single-class issuer, every past cover's count from its company facts (source `sec/companyfacts`), `float` from Yahoo and from UW |
| `security_symbol` | the symbols a security traded under and when |
| `market_cap_daily` | close times shares per security and session (the bar's own shares, else a WRDS source's of that session, else the newest cover on or before it moved over the splits between); `issuer_market_cap` sums the issuer's listed classes, each at its newest cap at most 14 days old (a class with no bar that session still counts); a Compustat class the spine lacks (BRK-A) is the issuer's of the spine classes that share its gvkey A share count of 0 is none; an ADR has no cap until an ADS ratio is read (#711): both keep their row, `market_cap` and `shares` NULL, `shares_source` `zero_count` or `adr_no_ratio` (S18: the store narrows nothing). |
| `price_revision` | the values a later read replaced: Alpaca's re-read and the monthly WRDS refresh, with `revised_at` and `revised_by` |
| `wrds_price_diff` | the WRDS difference report, one row per comparison: CRSP against Compustat and each against Alpaca on the closes of the sessions both have, and each WRDS source's own day return (Compustat's `COMP_RETURN`, CRSP's `dlyret`) against the store's on consecutive sessions; a difference over 1 bp counts; on the `wrds/prices` Settings card |

**The bridge (D4).** Views under the old `.store/prices.duckdb` names with the old columns: `price_bars_daily`
(split-adjusted OHLC, `adj_close` total-return adjusted, both to today, `fetched_at` the build time so the old reader
applies no split a second time), `price_actions` (splits and dividends, the amount in today's share basis) and
`index_levels_daily` (`^GSPC`).

**The two big tables' key.** `price_bar_daily` and `market_cap_daily` are the only tables with a row per security
**and** session, and DuckDB holds a `PRIMARY KEY`'s index in memory for the whole `INSERT`: the scratch build of
3 Nov 2026 ran the 5 GB `memory_limit` out after 4 h 51 min writing `price_bar_daily`. With the store cut to
today's universe the index fits, so **the key stays on** (avo, 4 Nov 2026) and the measured build decides;
`RC_PRICES_NO_PRIMARY_KEY=1` writes those two without it and checks their key with one grouped query instead
(`serving.duplicate_keys`, a `duplicate_key` build issue), so a build that still does not fit needs no code change.

**A whole build in chunks (#654).** A whole build used to stage forty years of `comp.secd`, `crsp.dsf` and
`comp.sec_divid` as TEMP tables and spilled about 80 GB (homesrv, 10 Nov 2026). It now runs the WRDS history in
`RC_PRICES_CHUNKS` chunks (default 16; `1` is the old single pass): the identity (`_universe`, the PERMNO link, the
gvkey aliases) is built whole over the WRDS years read through views, then each contiguous range of security ids
stages only its own series and PERMNOs and adds its bars and WRDS actions; the market caps are built per issuer
bucket from the served tables, and the difference report and the Alpaca close check in a second pass over the chunks
(`serving.Chunked`). Every history computation is per security, so the served tables are those of the single pass;
the build note logs each pass's time and peak spill.

A CRSP ticker link (rule 3) needs, for a spine `gvkey:iid` with a CUSIP, a Compustat row of it on CRSP's last day,
and for an ETF an inception day on or before it (review M1).

**Build issues** (dataset `prices/serving`, synced after each swap): `source_never_run`, `crsp_ticker_link` (the CRSP
PERMNO linked to a spine security, no ETF, by ticker alone: one issue per security, its PERMNO named), `tier_a_no_bar`
and `tier_a_no_shares` (a Tier A name **of the stored universe** with no bar, or no market cap, on the newest served
session: an untyped or excluded Tier A name is left out of the store on purpose; an ADR is `adr_no_ratio` instead: no
market cap until an ADS ratio is read, #711), `universe_etf_no_bar` (a universe
fund, a ticker of the spine's `etf` table or an `etf` of `my_universe`, with no bar on the newest served session;
key: the ticker), `universe_etf_ticker_shared` (more than one recent Compustat series carries a routeless universe
fund's ticker: one is stored, the one priced most recently, then the larger dollar volume; key: the ticker),
`price_gap_no_action` (the split-adjusted close moved by 1.9 times or more, either way, within 7
days, with no split; `price_gap_warrant` when the spine types the security a warrant or a unit), `split_only_crsp` (a split CRSP's `dlycumfacpr` records and no Compustat or Alpaca split is
within 3 days of, between two served bars at most 7 days apart: not applied; the detail names the served close's
move across it; key security:session), `adjustment_differs` (the store's total-return day return more than 1 bp from Alpaca's own, or
more than half a cent on each of Alpaca's cent-rounded adjusted closes, the exact bound, where that is larger: about
1.5 bp at $70, 5 bp at $20, 50 bp at $2, so below a few dollars of adjusted close a dividend-sized gap hides in it;
checked on every served bar Alpaca, the tester, has a bar for), `alpaca_close_differs` (the tester: a Compustat or CRSP
close more than 1 bp and half a cent from Alpaca's close of the session; key security:session:source),
`dividend_not_applied` (one that went ex on or before its security's first served bar adjusts no served bar:
counted in the build note, not named; review D6 of #420), `action_not_applied` (a spin-off, unit split or
rights distribution), `cover_class_unmapped`,
`cover_adr_ordinary` (an ADR's issuer's cover counts ordinary shares, not ADSs: kept under `class:`, no cap from it),
`float_above_outstanding` (over 1.05 times), `float_sources_disagree` (Yahoo and UW more than 25% apart),
`duplicate_key` (a table written without its `PRIMARY KEY` holds a key value twice; key table:value),
`shares_disagree_yahoo` (the cover shares behind a cap and Yahoo's `sharesOutstanding` more than 25% apart, on the
newest cover-sourced cap up to Yahoo's answer: a count from before a dilution or a split; the cap stays, flagged). Run issues of
the sources: `not_a_session`, `symbol_refused`, `action_undated`, `no_inline_xbrl`, `submissions_missing`,
`filing_not_in_submissions`, `instance_unreadable`, `wrds_column_empty` (a WRDS answer with rows and none of a
column the store needs), `wrds_no_access` (a CRSP library the WRDS login cannot read, keyed by the schema: missing
from `information_schema.columns`, lacking a column a query names, or refused with `InsufficientPrivilege`; CRSP is
skipped for the run and the history is Compustat's alone). Before any data query `wrds/prices` asks
`information_schema.columns` once and fails the run when a Compustat table or a Compustat column a query names is
missing.

**Attended (WRDS).** `wrds/prices` runs on the spine's WRDS worker (`RC_WRDS_PYTHON`, `WRDS_USERNAME`) from the CLI
only: `python -m RevenueChaser.rcdata.build backfill wrds/prices`. One query per table and year; a backfill asks the
two snapshot tables, then the years newest first, skips a year partition that exists with no query (a `comp.secd`
year asked with an older query, the exchange filter or the columns before, is asked again; its old answer is renamed
`.superseded-<run id>`, never deleted); the whole history is about 130 queries, asked in one session (WRDS publishes no query
quota, so `wrds` has no day cap; a run that hits one anyway keeps and serves the years it has). The monthly run asks
the last two years again and keeps every bar value it replaces as a revision. The long history run is Phase 8, after
PR 21. `backfill wrds/prices --dry-run` prints each query it would ask (why, expected rows), the query count and the
expected rows, with no login. Every query names its columns (no `SELECT *`).

**Reading it.** `rcdata.read.security_ids(tickers)`, `rcdata.read.bars(security_ids, start, end, basis)` (`nominal`,
`split`: today's share basis, `total_return`: the cash dividends taken out too; each row says its basis and factors),
`rcdata.read.actions(...)`, `rcdata.read.shares(..., measure)`, `rcdata.read.market_cap(...)`, and
`rcdata.read.latest_market(tickers)`: per ticker its newest nominal close and market cap (the class's own and the
issuer's sum over its listed classes), each only when it is at most 14 days before the store's newest session. The
spine universe takes its market cap and price from it (`core/universe.py`): the issuer's cap for a company, as
Yahoo's was, a fund's own (one trust's CIK files many funds), and the old `equity_universe` value by ticker where the
prices store has none.

**The price boards' reads (G3: PR 8, 9, 11).** By ticker, each name's bars under every symbol its security traded as
(`security_symbol`, the current symbol the name): `rcdata.read.ticker_bars(tickers, start, end, basis)` (a frame),
`last_sessions`, `newest_session`, `changed_at` (the newest `fetched_at` of the names' bars and actions),
`bar_sessions` and `bar_facts` (first and last session, the sessions, and the flat-close candidates, where an ex-date
counts as a move). **Co-movement** and the Relatedness peer market cluster `total_return` closes over two years back
from the store's newest session (`module_comovement/ohlc.py`; method 3). **Sector Rotation** and **Money Flow** read
`split` closes and volumes, and the 11 macro series from `market_series` by `(series_id, source)` (FRED's yields and
oil, Cboe's VVIX, Yahoo's MOVE and wheat; `core/rotation_reference.py`), so a Fetch asks no provider for them; the
range backfill stays ETF-only. **Asymmetric Growth** scores its momentum from `split` bars up to SPY's newest session
(`SELECTION_VERSION` 8, after G6's 7); its freeze anchor still asks D01 until the ledger moves (G4). Until the history run (Run H)
the store holds only the bars from the prices layer's deploy day on, so a two-year window or a 52-week high is short
and the boards say so (too few sessions, momentum unknown) rather than reading the empty old D01.

### The ownership store (`ownership.duckdb`, PR 16)

The fourth serving kind: who is short a name, who fails to deliver it, who holds it at quarter end and who trades it
from inside. Built by the same builder, swapped the same way, read only through `rcdata.read`; it reads the spine and
never writes it. Nothing here writes `raw.duckdb`: the old 13F, short and insider tables stay until the boards move
(G9) and the old writers retire (G10).

| Dataset | Cadence | Source | Served table |
|---|---|---|---|
| `finra/short_interest` | daily | FINRA's consolidated short interest API (keyless), one settlement date at a time, pages of 5,000 rows checked against `record-total` | `finra_short_interest` |
| `regsho/threshold` | daily | Nasdaq's `nasdaqth<day>.txt` and NYSE's threshold download (NYSE, NYSE American, NYSE Arca); a day with no list is a 404 or, at Nasdaq, a redirect to its not-found page | `regsho_threshold` |
| `ibkr/shortable` | daily | IBKR's public short-stock file (`usa.txt`, FTP, no account): borrow fee, rebate, shares available | `ibkr_shortable` |
| `sec/ftd` | daily | The SEC's fails-to-deliver halves (`cnsfails<YYYYMM><a/b>.zip`) | `sec_ftd` |
| `sec/13f` | daily | The SEC's Form 13F data sets (the first fill), then 13F-HR and 13F-HR/A through the EDGAR daily index | `sec13f_filings` (holdings stay in Parquet) |
| `sec/13f_sets_listed` | daily (asks only while a set is awaited) | The SEC's Form 13F data-set page: which sets are published, read once a day from the day after a filing deadline until a listed set covers it (#732); nothing is downloaded | `sec13f_data_sets` |
| `sec/form4` | daily | The SEC's Insider Transactions data sets (the first fill), then Form 4 and 4/A through the EDGAR daily index | `insider_transactions` |
| `sec/13dg` | daily | Schedules 13D and 13G, old (`SC 13G`) and new (`SCHEDULE 13G`, XML since 2024-12-18) names | `beneficial_ownership` |
| `uw/institutions` | quarterly | Unusual Whales' `GET /institutions` without a name: every 13F institution by value, 500 to a page (18 pages, 8,875 CIKs on 12 Nov 2026), with its tags, `is_hedge_fund` and its succession | `uw_institution`, `uw_institution_succession` |
| `sec/13f_list` | quarterly (a held quarter is not asked again) | The SEC's Official List of Section 13(f) Securities, one text file per quarter since 2020 Q1 (`13flist<yyyy>q<n>.txt`, fixed width; a `*D*` row was deleted), typed to `parquet/sec/13f_list/part-0.parquet` (#926) | none: `ownership/serve` reads the Parquet for the `cusip_not_listed` issue |
| `sec/nport` | monthly, newest listed quarter only (a held set is not asked again) | The SEC's Form N-PORT data sets (`<yyyy>q<n>_nport.zip`, about 440 MB, streamed to disk): every fund's series and holdings, typed to `parquet/sec/nport/quarter=<set>/` (`fund.parquet`, `holdings.parquet`) (#354) | `etf_holdings_nport` (the spine ETFs' newest report, positions whose CUSIP is a spine ticker) |
| `ownership/serve` | daily | none: derives the 13F totals from the 13F tables and the spine | the four 13F tables below |

**Starts by hand.** Every ownership dataset is not due until it has built once, so the first hourly pass after a
deploy calls no source before the server thread's first runs (after a backup).

**The 13F first fill** (avo, 28 Oct 2026): `python -m RevenueChaser.rcdata.build backfill sec/13f --from
2025-01-01`. The backfill reads the SEC's Form 13F data-set page (`RC_FORM13F_INDEX_URL`), downloads each set whose
filing window meets the range (one zip of about 100 MB per three months, kept under `raw/sec/13f_sets/`, downloaded
once and asked again on a later backfill with `If-Modified-Since`), and types its 13F-HR and 13F-HR/A into the same
day partitions the EDGAR walk writes, marking those days done up to the set's newest filing; the walk then reads the
forms only for the days after it (the hourly runs, 15 minutes each). A set the SEC publishes again reopens the days
it had filled; the days the walk finished stay the walk's. A backfill shorter than 20 weekdays (a re-walk of a few
days) reads the forms only and asks nothing of the sets. On
real data (28 Oct 2026) a set took about 30 seconds: 01jun2026-31aug2026 holds 9,900 13F-HR and 13F-HR/A with 3.8
million holdings. Filings from 2025-01-01 make Q4 2024 the first quarter served, so Q1 2025 has its flows. SEC
requests share `core/sec_throttle.py`'s bucket (9 a second) with every other SEC caller; when it cannot be used each
of the SEC processes (`sec_throttle.SEC_PROCESSES`, six) spaces itself at its share, so together they stay at 10.

**The 13F totals** (`ownership/serve`; the rules in `rcdata/build/ownership/serve.py`): per filer and report period
the amendment walk (`NEW HOLDINGS` adds, any other amendment replaces); long equity only; each CUSIP named through the
spine's dated Compustat history, then its current CUSIP (refused for a period in which Compustat gives the CUSIP to
another security), then its OpenFIGI map behind the reused-ticker guard (#236); VALUE off by 1,000 found from the 13F's own consensus prices and rescaled both ways
(alsanvolkan/RevenueChaser#208); flows and `breadth_chg` against the calendar quarter before; a quarter whose filing deadline passed before the store's
first 13F day is not served; `filing_deadline` (period end + 45 days, rolled past weekends and Washington's
Birthday) says when a quarter is complete; `exited_confidential` and `exited_no_filing` count the exits that may not
be sales.

**The institution map** (`uw/institutions`, #775; owner's decisions A and B of 12 Nov 2026: UW's tags may be stored,
and the read is quarterly). The key is the CIK, which `sec13f_filer_period` already carries per quarter, so an
investor-type slice of a 13F metric needs no per-ticker provider call. Each read is kept whole as its own snapshot
(`snapshot_date`), so a slice can say which snapshot it rests on. Paging is by `limit=500` and `page=N` until a
short or empty page; UW orders the list by value, so a CIK on two pages is kept once (its first row) and counted in
a `moved` build issue, and over 1% of the rows repeating fails the run rather than keeping a moved list. Successions
are read from **both** sides UW names — an institution's `succession.predecessors` and its own
`succession.succeeded_by` — because they do not cover the same pairs: on 12 Nov 2026 only two rows of the whole list
carried a `succeeded_by`, while HPS Investment Partners → BlackRock (effective 2025-09-30) was in BlackRock's
`predecessors` alone.

**Served tables** (`ownership` serving schema version `5`; 5 since #253 and #788: split-adjusted flows,
`sec13f_type_sector`, `sec13f_type_flow`, `sec13f_filer_style` (#779), `sec13f_filer_holdings` (#928) and `etf_holdings_nport` (#354); 4 since #760: `sec13f_type_holdings`; 3 since #775:
`uw_institution` and `uw_institution_succession`; 2 since #732: the continuing-filer flows and `sec13f_filer_breaks`):

| Table | Rows |
|---|---|
| `finra_short_interest` | one per settlement date and symbol: short interest, the previous one, average daily volume, days to cover, the spine ticker |
| `regsho_threshold` | one per list day, market and symbol |
| `ibkr_shortable` | one per snapshot session and symbol: fee, rebate, shares available (`available_capped` above 10,000,000) |
| `sec_ftd` | one per settlement date, CUSIP and symbol as the SEC wrote them |
| `sec13f_filings` | one per 13F-HR and 13F-HR/A: period, filed, amendment and its type, `confidential_omitted`, `value_scale`, `origin` (`edgar` or `set:<zip>`), status |
| `insider_transactions` | one per Form 4 transaction (or one `unparsed` / `no_transaction` row per filing) |
| `beneficial_ownership` | one per Schedule 13D/G reporting person (or one `header_only` / `unparsed` row per filing) |
| `sec13f_cusip_map` | one per CUSIP and period: its ticker and `map_source` (`compustat/owner` when Compustat's one owner of the CUSIP in the period named it, #278), or the refused `candidate_ticker` |
| `sec13f_filer_period` | one per filer and period: names, value, `median_ratio`, `value_factor`, `confidential_omitted` |
| `sec13f_ticker_period` | one per ticker and period: shares, value, holders, top-10 % and HHI (a filer family counts as one holder there, #925: the 13F's List of Other Included Managers guarded by the firm word, plus the sibling filers of a firm in `serve.FAMILY_FIRMS`, which is what joins two filers of one firm that name each other nowhere; a family's key is its largest filer by book), new / exited holders, buy / sell flow (the quarter before put on the quarter's share basis with the prices store's split factor at each quarter end, #253; as filed where the prices store does not place the ticker), breadth and its change, `prev_period`, `filing_deadline`. The period is the calendar quarter end; a filing's period that is not one (09-29 for the September quarter) counts in its quarter, and `sec13f_filings` keeps it as written |
| `sec13f_periods` | one per period: filers, value, value mapped, `coverage`, unmapped CUSIPs, refused value, rescaled filers |
| `sec13f_filer_breaks` | one per ticker, settled period and filer that started (`started`: no report in the quarter before) or stopped (`stopped`: none in the quarter) reporting it, with its shares; the flows leave these filers out (#732). A filer UW lists as succeeded in the quarter it stops (`uw_institution_succession`, newest snapshot), whose successor reports that quarter, is its successor in the quarter before: one filer across the hand-over, no stop and no start (#776) |
| `sec13f_type_holdings` | one per investor type, period and rank: the type's 25 largest tickers (`TYPE_TOP`) by the value its filers hold, each with its `share` (%) of the type's mapped portfolio, and one row with `ticker` NULL for the rest, so a quarter's shares add up to 100; `type_value`, `type_filers` and the `uw_institution` `snapshot_date` its types come from. `holders` and `type_filers` count filer families, so one firm counts once (#925). A type is a tag UW gives the filer's CIK in the newest snapshot (a filer can carry several), plus `all` for every filer (#760) |
| `sec13f_type_sector` | one per investor type, period and sector: the value the type's filers hold in that sector (the spine's `sector` of each ticker, today's for every quarter; `Unclassified` where the spine has none), its `share` (%) of the type's mapped portfolio, `tickers`, `filers` (filer families, one firm once, #925) and the `snapshot_date` its types come from (#788) |
| `sec13f_type_flow` | one per investor type and period: dollars `bought` and `sold` and the `net`, per filer and ticker the split-adjusted change in shares against the quarter before at the quarter's 13F price (value over shares), over the type's `filers` that reported both quarters, as the ticker flows are (#788) |
| `sec13f_filer_style` | one per filer of the newest settled quarter holding 5 or more names, over that quarter and the 7 before it: `quarters` reported, `names` and `value` now, `turnover` (median over its quarter pairs of half the summed change of each name's weight in its book), `top10_share`, and `held_share` (its value in names held every quarter it reported). Data only until the single-investor view (#928) (#779) |
| `sec13f_filer_holdings` | one per filer, period and one of its 25 largest named tickers by value (a ticker's CUSIPs summed): `shares`, `value`, `weight` in percent of the filer's whole long-equity book and `rank`; plus one row with no ticker for the rest of the book, unnamed CUSIPs included, so a quarter's weights add up to 100. One investor's holdings over time (#928) |
| `etf_holdings_nport` | one per spine ETF (`etf.series_id`, no ETN) and position of its newest Form N-PORT report in the newest typed quarter, only positions whose CUSIP is one spine ticker's current CUSIP: `etf_ticker`, `ticker`, `cusip`, `balance`, `currency_value`, `pct_of_net_assets`, `asset_cat`, the report's date and accession (`sec/nport`, #354 C2) |
| `uw_institution` | one per snapshot day and CIK: `name`, `short_name`, `tags` (sorted), `is_hedge_fund`, the report and filing dates, total and share value, `succeeded_by_cik` with its `succeeded_by_report_date`, and `current_holder_cik` |
| `uw_institution_succession` | one per snapshot day, predecessor CIK, successor CIK and `effective_report_date`, with both names and `origin` (`predecessors`, `succeeded_by` or `both`) |
| `sec13f_data_sets` | one per 13F data set the SEC page ever listed: `name`, `first_day`, `last_day`, `url`, `first_seen`; a period is `closed` once a listed set's window holds its filing deadline (#732) |

**The bridge (D4).** Views under the old `raw.duckdb` names: `sec13f_totals` (ticker, `entry` JSON keyed by period
with the old metric keys) and `short_interest` (one row per ticker and session: the newest FINRA short interest
published before it, a settlement date counting from the eighth business day after it because FINRA publishes it
on the seventh after the close (#252), and that session's IBKR fee, rebate and shares available; `si_float` and `total_float` are
NULL).

**Build issues** (`ownership/serve`): `spine_missing`, `cusip_owner_differs`, `cusip_unmapped_large`,
`filings_unparsed`, `value_rescaled` (the 50 filer-quarters whose VALUE rescale, by 1,000 or 1,000,000, or down by 10
or 100 (#934), moved the most dollars, keyed `cik|period`, with the median ratio and the names it rests on),
`value_row_replaced` (the 50 holdings, keyed `cik|period|cusip`, whose value lay 300 times or more from the 13F's own
price and was read at that price, #780; never above 3,000 times its filing's cover total, #932) and `value_suspect`
(the 50 largest filer-quarters left as filed whose median ratio lies over 3 or under 1/3: VALUE or share counts
wrong, listed and not changed; avo, 16 Nov 2026) and `cusip_not_listed` (per period from 2020 Q1 on, keyed by the
period: the CUSIPs and dollars not on that quarter's SEC 13(f) list, `sec/13f_list`, with the largest as the example;
marked, nothing dropped, #926).

**Reading it.** `rcdata.read.holders_13f(tickers, periods)`, `periods_13f()` (both with `complete`),
`finra_short_interest(tickers, start, end)`, `shortable(tickers, session)`, `threshold_list(on)`,
`fails_to_deliver(tickers, start, end)`, `insider_transactions(tickers, start, end)` and
`beneficial_ownership(tickers, start, end)` (each with the dataset's last check as `status` and a filing's own as
`parse_status`).

**Known limits.** The IBKR file is not reachable from the cloud (no FTP egress): its format is checked by the first
server run. Cboe's threshold list is a JavaScript page and is not read. FINRA's revisions are caught for the newest two
stored settlement dates only, which every daily run fetches again (#252). A quarter's flows across a stock split compare unadjusted shares (G2). A store built before
the served-quarter rule shrinks once on its next derive (`allow_shrink`).

### Fundamentals (`fundamentals.duckdb`, PR 12)

The fifth serving kind: what each Tier A company reported, when, and in which filing. Built by the same builder,
swapped the same way, read only through `rcdata.read`. The companies are the spine's Tier A names (their CIKs,
gvkeys and GICS codes), so the spine is built first. Nothing here writes `raw.duckdb` or the old `edgar_financials`
cache; since #642 PR 6 the boards never read that cache: no layer reads as no rows, and the board says why.

| Dataset | Cadence | Source | Stages |
|---|---|---|---|
| `sec/companyfacts` | daily | `data.sec.gov/api/xbrl/companyfacts/CIK*.json`, re-read on the day the SEC daily index shows a statement filing (10-K, 10-Q, 20-F, 40-F and their amendments) whose accession the stored facts do not hold; a new Tier A name up to 500 a run | `stg_sec_facts`, `stg_sec_company` |
| `sec/filings` | daily | `data.sec.gov/submissions/CIK*.json` when the daily index shows a new filing; the primary document of the newest 10-K (else 20-F) and DEF 14A, fetched once each; the list's company header (SEC SIC, state of incorporation, business address) | `stg_sec_filings`, `stg_sec_documents`, `stg_sec_segments`, `stg_sec_customers`, `stg_sec_header` |
| `alphavantage/earnings_calendar` | weekly | Alpha Vantage `EARNINGS_CALENDAR`, three months ahead, one request a run (`ALPHAVANTAGE_API_KEY`); weekly since 21 Nov 2026 (avo), a failed run still due again the next New York day | `stg_av_calendar` |
| `uw/earnings` | weekly | UW `/stock/<ticker>/earnings`, one request per company: the quarterly rows' reported EPS, UW's consensus of that quarter and its surprise, as UW serves them | `stg_uw_earnings` |
| `yfinance/estimates` | weekly | Yahoo's `earningsTrend` per company, its shortest ticker, when its newest answer is 7 days old; 5 failures in a row stop the run | `stg_yf_estimates` |
| `yfinance/fx` | daily | Yahoo's daily close of `<CUR>USD=X` per currency in `fx_rates.CURRENCIES` (USD per unit), the whole history the first time, then from 7 days before the newest stored day. Since #1181 it fills the days `wrds/exrt` does not hold | `stg_yf_fx` |
| `sec/fsds` | monthly | The SEC's quarterly Financial Statement Data Sets zip, the newest quarter published | `stg_sec_fsds` |
| `wrds/fundq` | monthly, attended | `comp.fundq`, the Tier A gvkeys, ten years | `stg_wrds_fundq`, `stg_wrds_fundq_revision` |
| `wrds/funda` | monthly, attended | `comp.funda`, the Tier A gvkeys, ten years | `stg_wrds_funda`, `stg_wrds_funda_revision` |
| `wrds/segments` | monthly, attended | `comp_segments_hist_daily.wrds_segmerged` and `seg_customer`, five years, the newest source date per key where the table has one | `stg_wrds_segments`, `stg_wrds_customers` |
| `wrds/exrt` | monthly, attended | `comp.exrt_dly`, the whole history of `fx_rates.CURRENCIES` and USD. Every row is quoted from GBP, so USD per unit is the cross `exratd(GBP->USD) / exratd(GBP->CUR)` (#1181, step H). **The rate a non-USD filer's values are converted at wherever Compustat has the day** (`fx_rates.held_rates`; DECISIONS F3); `yfinance/fx` fills the days it lacks, which are the newest ones between two monthly snapshots. The check note counts the pairs of the newest year that agree with Yahoo within 1% | `stg_wrds_exrt_raw`, `stg_wrds_exrt` |
| `wrds/audit` | monthly, attended | Audit Analytics' three event feeds in `audit_audit_comp`, whole: `feed39_financial_restatements`, `feed20_nt` (late-filing notices) and `feed17_director_and_officer_chan`. Only the event's own columns are asked (key, `company_fkey`, filing day and form, what happened); no person's name and no disclosure text. `company_fkey` is the CIK (measured 20 Nov 2026, #1181 step C), and the feeds run about two months behind. The flag columns are text in the snapshot; their coding was measured on the first one (20 Nov 2026: every `res_*` and `is_*` flag is 0 or 1, `action` 17 words), and `audit_flag` serves our companies' rows. The check note counts each feed's rows, keys and our companies in it | `stg_wrds_restatements`, `stg_wrds_late_filings`, `stg_wrds_officer_changes` |
| `fundamentals/tables` | daily | none: builds the served tables, the views, the peer groups and the build issues from the staging tables | the tables below |

**A builder that was down still reads what was filed meanwhile.** `sec/companyfacts` and `sec/filings` read the
SEC daily index of the last 7 days, or back to the day after their last whole run when that is longer ago (the run
writes `raw/sec/<name>/_scanned.json` at its end; without one, the day after the oldest newest read), at most 120
days back. A widened or capped window is said in the run's note.

**Every value carries its filing.** A served fact has its accession, filed date and form; a value the SEC
extraction (`core/providers/sec_edgar.py`, so a value here is the value the boards read today) keeps no provenance
for gets the newest fact of its concepts on the period end, and a value still without one is not served. Total debt
is a long-term line plus a current line, each debt line of the balance sheet read once, whichever tags the filer
uses (`LongTermDebtNoncurrent`, `LongTermDebtAndCapitalLeaseObligations` or `LongTermNotesPayable`; then `DebtCurrent`,
else the current portion plus `ShortTermBorrowings` or `CommercialPaper`; a long-term line that includes its current
portion plus the short-term line; no total without a long-term line; #239, measured on the real-data check of
2026-10-28 in PR 249; convertible notes due within the year are the current line when no other current line is
tagged, and are added beside a whole long-term line only when that line is below them, #264). When the filer
tags none of those lines at all, a second pass reads the balances it may tag instead (`SeniorNotes`,
`ConvertibleDebt`, `SecuredDebt`, `NotesPayable`, `LongTermLoansPayable`, `LinesOfCreditCurrent` and their
neighbours, `rcdata/build/fundamentals/facts.py` `FALLBACK_*`), in the same three slots. One line of each ladder
is taken, the largest positive one and never a sum (which of these tags overlap is not readable from the facts); a
zero line is passed over for a positive one of the same ladder, because a `total_debt` of 0 reads as debt-free
while a NULL does not, but it keeps its slot when a positive current line stands beside it, which is the filer
saying nothing is due beyond the year (OKTA's `ConvertibleDebtNoncurrent` 0 next to 350 m of convertible notes);
a second-pass total of 0 is never served; a current line alone is still not the whole; and the pass runs only where the first one gives nothing, so no total that exists
today moves. Measured over the board's 2,131 anchor years on 13 Nov 2026: 157 years gain a total, 0 change value
and 0 are lost (#737). A tag that is a schedule or a contra item
(`FinanceLeaseLiabilityUndiscountedExcessAmount`, `DebtInstrumentCarryingAmount`, `LongTermDebtMaturities...`) is
not read, although edgartools' mapping files it under its debt lines. EPS (basic and diluted) is read too. An accession the SEC's XBRL API does not hold yet after the re-read is asked again
by the next day's run (the run's note says `pending`), so a statement reaches the served table on its filing day or
the day after, never months late (the Fundamentals review's F3).

**Preliminary quarters: none.** The Unusual Whales preliminary-quarters dataset, which served a quarter UW had
and the SEC did not, was removed on 12 Nov 2026 (#659): the assumption it rested on did not hold, UW's fundamentals being Alpha
Vantage's. `fundamental_fact` keeps its `preliminary` column and the CHECK that goes with it — they are the
table's shape, and changing it would bump the serving version and rebuild every file — but nothing writes a
preliminary row any more.

**WRDS is a check, not a served value.** Attended and monthly on the spine's one WRDS worker (one login, at most one
Duo push per builder process): `python -m RevenueChaser.rcdata.build backfill wrds/fundq wrds/funda
wrds/segments wrds/exrt wrds/audit` in a foreground terminal on the server. Each monthly `fundq` / `funda` snapshot is compared with the
one before: every value that changed for a period both hold is a `fundamental_revision` row (a restatement, or a
correction at Compustat; a new period is not one). The check notes count revenue, net income and diluted EPS that
agree with the SEC's facts (Compustat's millions times a million, within 0.5%, EPS within a cent, period ends within
7 days). `funda` is compared with the value the year's own report first gave (`first_value`), because Compustat
keeps that one where the SEC serves a later restatement or split-adjusted figure (#277). The note of each
WRDS dataset's newest run is its card's WRDS difference slot on Settings (`docs/data_health.md`). Compustat segments
are served beside the annual report's inline XBRL ones, each row with its `source`; a Compustat customer's share is
its sales over the SEC's annual revenue of that year, NULL when there is none or the ratio is above 1. The first
real run (2026-10-29, PR 249) found no `srcdate` in `seg_customer`, so `wrds/segments` reads WRDS's own catalog
(`information_schema.columns`, one more query) before its tables: a
column the code can do without that WRDS lacks is not asked and the note says so, and a table in none of its
libraries, or without a column it needs, fails the run naming what WRDS has.

`wrds/firm_ratio` (WRDS's Financial Ratios Suite, `debt_at` against the SEC's total debt over total assets) was
dropped on 5 Nov 2026, avo's decision: it served no table, only that agreement note, and the same real run found no
`wrds_finratio` library for it. The SEC's own as-filed values (`sec/fsds`) are the check that stands.

**The FSDS check** (`sec/fsds`). The SEC's quarterly data sets hold every XBRL value filed in the quarter. The
Tier A companies' own values (no co-registrant, no dimension) of revenue, net income, total assets and diluted EPS
are compared with the served facts of the same period (within 0.5%, EPS within a cent); the note counts them per
field. A difference is a note to look at, not an error: the served value may come from a later filing that restated
the period.

**Starts by hand.** Every fundamentals dataset is not due until it has built once, as the spine's are, so the first
hourly pass after a deploy calls no source before the attended backfill.

**Served tables** (`fundamentals` serving schema version `7`):

| Table | Rows |
|---|---|
| `fundamental_company` | one per Tier A ticker: the spine's identity and GICS codes, the SEC filer's name and kind (`is_us`, `us_gaap`, non-USD units), the newest facts read and statement filed, and `submissions_as_of`, the day its filing list was last read ("no 8-K event" means something only after it, the Fundamentals review's F2); from that list's header (#645, version 3) `sic`, `sic_description`, `state_of_incorporation` (EDGAR's code) and its description, `business_state` (a US state or Canadian province) and `business_country` (ISO 3166-1 alpha-2 from EDGAR's state-or-country code, `rcdata/build/fundamentals/edgar_codes.py`) |
| `fundamental_fact` | one per company, field, period type and period end: value, unit, accession, filed, form, `derived`, `preliminary`, source; and the first-reported value of the same tag and span with its accession and filed date (`first_value`, `first_accn`, `first_filed`; #277 A2, version 3; NULL on a derived row). The fields include `income_tax_expense`, `pretax_income` and `interest_expense` (#645 G1) |
| `filing_item` | one per filing and 8-K item (`kind` `item`). The document texts went with the cut (#858): this layer cuts nothing, `sec/forms` is the one cutter and serves them as `filing_section`. The `kind` column stays, with `item` its only value |
| `earnings_event` | one per report: a coming one from the calendar (`confirmed` false) or one that happened (an 8-K with item 2.02, `confirmed` true, its accession kept) |
| `earnings_reported` | one per company and reported quarter: the EPS the company itself reported as UW serves it, with UW's consensus and surprise. **Not** a `fundamental_fact` row -- it has no filing behind it, and its basis is usually the company's adjusted (non-GAAP) figure, so `source` is on every row (avo, 5 Nov 2026) |
| `earnings_report_history` | one per company and fiscal quarter (version 5, #1181): the day the quarter's results were made public. Compustat's `rdq` (`wrds/fundq`) where it has one; UW's report day (`uw/earnings`) for a quarter Compustat has no date for within 7 days of the same period end. `source` on every row says which, `gvkey` the Compustat company (NULL on a UW row). `fiscal_year` and `fiscal_quarter` (version 7, #1214) are Compustat's `fyearq` and `fqtr`, NULL on a UW row: the label Alpha Vantage asks a transcript by, which no date arithmetic produces for a company whose fiscal year does not end in December. Past dates only: a coming report is `earnings_event`. Read with `rcdata.read.earnings_report_history` |
| `earnings_transcript` | one per turn of an earnings call, in the order spoken (version 7, #1214; `alphavantage/earnings_transcripts`): `ticker`, `quarter` (the company's **fiscal** quarter, `YYYYQn`, which is how Alpha Vantage labels a call -- measured 21 Nov 2026), `seq`, `speaker`, `title`, `content`, and `provider_sentiment`, the provider's own per-turn number, kept as it came and read by nothing. **The halves are not stored apart**, because the provider sends no prepared-remarks / Q&A field: the first analyst turn is the boundary and the caller splits there (`module_earnings_radar/rules/transcript_split.py`). One request per (ticker, fiscal quarter), bounded by the provider's day cap, so coverage climbs day by day and a company without a stored call simply has none yet. Read with `rcdata.read.earnings_transcript` |
| `audit_flag` | one per event Audit Analytics recorded for a company of ours (version 6, #1181 step C; `wrds/audit`): `kind` is `restatement`, `late_filing` (the NT forms) or `officer_change`; `event_key` the feed's own key, `person_key` the officer feed's opaque person number (never a name); `file_date` and `form` the filing; `effective_date` and `action` (Appointed, Resigned, ...) on an officer change; `period_start` and `period_end` the restated span, or the period a late filing is late for; `flags` the list of what the feed marked 1 (restatement: `accounting`, `adverse`, `clerical_error`, `fraud`, `improves`, `other`, `quarterly_only`, `sec_investigation`; officer change: `board_member`, `c_level`, `ceo`, `cfo`, `chair`, `coo`, `interim`, `president`). **A history, not a fresh flag:** the feeds end about two months before the pull, so a company's newest events are `filing_item` first. Read with `rcdata.read.audit_flags` |
| `fundamental_revision` | one per Compustat value that changed between two monthly snapshots |
| `segment_revenue` | revenue by business segment, product or country (`axis`), from the annual report's inline XBRL (its newest year) and from Compustat; `report_tables` (#1048, version 4): the tables of the report, as role ids of the filing's definition linkbase (`<ticker>-<date>_def.xml`, or the schema file `<ticker>-<date>.xsd` when the filing lists no `_def.xml` and embeds its linkbases there, #1066; fetched beside the document, `rcdata/build/fundamentals/linkbase.py`), in which the row's member stands on the row's axis, NULL for a Compustat row and for a filing with no linkbase stored. `rcdata.read.segment_sets(rows, total)` (pure) gives the member sets one table declares together that sum to the revenue within 2% |
| `customer_share` | a major customer's share of revenue (or of receivables, `benchmark`), from `ConcentrationRiskPercentage1` and from Compustat |
| `estimate_latest` | the newest analyst estimates per company, ticker and period (the weekly snapshots stay in Parquet) |
| `peer_group` | each company's 10 nearest companies on the GICS ladder (sub-industry, industry, industry group, sector), the closest annual revenue first within a rung |

**Views** under the old names, with the old shapes, for the boards' move: `edgar_financials` (one row per ticker,
`annual` and `quarterly` as JSON lists of records as `sec_edgar.extract_*` writes them, the SEC's rows only),
`sec_submissions` (one row per filing, the 8-K items as one text), `earnings_calendar` (the old calendar columns; what
this layer does not have is NULL) and `estimate_snapshots` (`session_date` the day it was asked). `fundamental_quarter`
is one row per company and quarter with a column per field.

**Build issues** live in `ops.sqlite` `build_issues`, dataset `fundamentals/tables`, synced after each swap. Kinds:
`source_never_run`, `statement_older_than_120_days`, `annual_statement_older_than_400_days` (an annual-only filer,
also one that moved to IFRS and whose last us-gaap statement is years old), `no_statement`, `no_us_gaap_facts`. A source dataset records its
own row-level problems (`missing_key`, `fetch_failed`) under its name.

**Reading it.** `rcdata.read.statements(cik, period_type, n, fields)`, `rcdata.read.earnings_events(cik, start, end)`,
`rcdata.read.estimates(tickers)`, `rcdata.read.filing_text(cik, item)`, `rcdata.read.segments(cik, axis, source)`,
`rcdata.read.customers(cik)`, `rcdata.read.peers(cik)` and `rcdata.read.revisions(cik, since)`; for many companies
at once `rcdata.read.statements_many(ciks, annual_years, quarters)`, `rcdata.read.fundamental_companies(tickers)`,
`rcdata.read.filing_items(ciks, form, since)`, `rcdata.read.filing_texts(ciks, item)` (which since #858 reads
`filing_section`, the one cutter's table, under its old name and row shape; `filing_text` and `filing_documents`
likewise) and
`rcdata.read.customer_shares(ciks, benchmark)`. Every row carries its provenance and `status`, the newest check of
the dataset its own row came from (a segment row from Compustat reads `wrds/segments`' check, one from the annual
report `sec/filings`'), or `never_checked`, or `status_unknown` when `ops.sqlite` cannot be read (#228).

**The boards on the layer (G6, build plan PR 13-15 and 20).** `core/fundamentals_layer.py` rebuilds the old
`edgar_financials` record shape from `fundamental_fact`, so a board keeps its rules and reads current rows. There is
no switch since #642 PR 6: the boards read the layer only (a failed newer check keeps the last good build), and
without `fundamentals.duckdb` or a first `sec/companyfacts` build they read no rows and `fundamentals_layer.source_state()`
says why. Relatedness and Value Chain alone still read Value Chain's `chain_filings` corpus while the layer is not
served (`fundamentals_layer.reads_layer`). Their filing text on the layer is the SEC form reader's since #858: one
cut, in `filing_section`. What each board reads there:

| Board | Reads on the layer |
|---|---|
| Fundamentals | The statements, and on each card the filing and read days (F3); the 8-K flag from `filing_item`, its checked day the newer of the filing list's read day and `sec/filings`' newest good check (F2); ROIC, gross margin and net debt on the trailing year when the quarters give one (TTM, else the fiscal year, the basis named on the card) |
| Asymmetric Growth | Growth, acceleration, margins, free cash flow, runway and dilution on three trailing years a year apart when the 10-Qs give them, else the fiscal years, one basis per card (`period_ends.basis`; review SF-4) |
| Financials | The SEC's annual statements, the newest five, and **nothing else** — Yahoo was dropped with #645 (avo 10 Nov 2026, option B), so the board makes no provider call at all and a name the fundamentals layer holds no annual year for gets **no model**, the source line saying so (`module_financials/run.NO_SEC_DATA`). A line the SEC facts do not carry stays empty, so the engines' labelled default shows; EBITDA is operating income plus depreciation; the years end on the newest year with revenue. The source line names this filer's own gaps: the tax rate when no year's tax provision over pre-tax income is believable, and the interest expense when the newest year has none (the facts carry all three concepts since #645 G1, so neither is a constant gap; a bank's P/E model reads neither, so it has none). The company name is the SEC entity name, else the spine's row, else the ticker. The price, market cap, shares and beta come from the prices store (`module_financials/market_inputs.py`: the newest close, at most 14 days before today like the rate, its session named on the card and in the source line; the issuer's market cap, a fund's or a trust's its own; the cap over the price as the class-equivalent shares, only when both are of one session; and a 5-year monthly beta of split-adjusted price returns on `index:SPX`, at least 36 months) and nothing stands in for one it does not give — the source line names it as not stored. The enterprise value is the store's bridge (market cap plus total debt minus cash of the newest stored fiscal year), the same one the peers use since #286, so target and comps are on one basis; the peers' EBITDA is operating income plus depreciation and the basis line names the derivation and the shares' side. The risk-free rate is `market_series` DGS10, at most 14 days old. Quick Remarks is the shared `ticker_scores` card alone; a name no Asymmetric Growth Fetch has scored gets an empty section |
| Relatedness | The newest annual report text `sec/filings` serves (Value Chain's splitter and cap, so the same filing gives the same input hash) and the layer's CIK; a profile's cache key and the payload carry the filing it was read from (CIK, accession, form, filed) |
| Value Chain | The newest annual report's business section the SEC form reader serves (`sec/forms`, `filing_section`; `read.form_business`, #841), which is both the corpus its edges are read out of and the filing list the board shows - on the layer the board cuts nothing of its own and no longer reads `sec/filings`' `filing_item`. Plus the annual report's XBRL major customers (`customer_share`, share of revenue): a customer the alias list names is a `customer` edge with its share; a sentence that states the same customer keeps its text and gains the share; an anonymised customer is no edge |

The trailing year (`core/trailing_year.py`) is the fiscal year plus the newest year to date minus the same months a
year earlier, each flow all or nothing, the balance sheet the quarter's.

### The options data (`options.duckdb`, G8)

The sixth serving kind (build plan PR 18): SPX and the option watch list (D2), the screener set of every reporting
name, and the earnings set of the evening. Built by the same builder, swapped the same way, read only through
`rcdata.read`. The tables (`rcdata/build/options/tables.py`; every one carries the provenance `source`,
`source_as_of`, `fetched_at`, `run_id`, `observed`, and the per-strike, per-expiry and print tables a `set_name`,
`list` or `earnings`, that is part of their key):

| Table | Key | Holds |
|---|---|---|
| `option_eod` | session date, option symbol | The chain at the close: every contract of SPX and the list names, and of the earnings set's names on their evenings (the 12 expiries with the most open interest 1 to 365 days out, no greeks; avo's F2, 18 Nov 2026), its quotes, IV, greeks, open interest and its change, volume by side, charm and vanna; the last 30 sessions (the Parquet keeps every one). `cross_volume` is a spec field UW has not sent yet (NULL on all 19,774 rows of the first real run, 2026-10-28) |
| `option_strike` | session date, ticker, set, strike | Spot exposures by strike (the four greeks for calls and puts by OI, volume, bid and ask) and the greek exposure by strike |
| `option_expiry` | session date, ticker, set, expiry | Contracts, open interest and volume per expiry, greek exposure by expiry, the day's flow per expiry, the IV term structure |
| `option_daily` | session date, ticker | The GEX levels, the day's option volume and premium, IV rank, and the chain's size |
| `option_print` | session date, ticker, set, print id | The last 50 prints of the session, with `tags`, `nbbo_bid`/`nbbo_ask`, the side volumes, `multi_vol`, `upstream_condition_detail` and `report_flags`; `side` (`ask`, `bid` or `mid`: UW's side tag, else the side of the quote's midpoint the price is on, NULL without either) and `package` (a multi-leg or stock-tied trade condition); the rule is `core/option_prints.py`, the same one the Earnings Radar R5 lean reads from the live tape |
| `iv_daily` | session date, ticker, source, days | Implied volatility per tenor: UW's interpolated IV (observed by the daily run) and IBKR's 30-day IV year (backfilled, `observed` false) |
| `option_screener_daily` | session date, ticker | Every name reporting in the next 28 days, its report date and session, the calendar item's `expected_move_perc`, `street_mean_est`, `pre_earnings_close`, `post_earnings_close`, `report_time` and `full_name` (`marketcap` the calendar's when the screener has none), its screener row (or `screened` false), and whether it passes the Earnings Radar gates; `dollar_option_volume` is the day's call plus put premium as UW served it (NULL when not served, which fails the gate), not volume x 100 |
| `er_options` | session date, ticker | The earnings set: one row per name and evening (each of the two before its report), with the report date it was fetched for |
| `option_far_print` | session date, print id | The market's far-dated prints (183 days or more, $50k premium or more, every underlying): the print fields of `option_print` plus `next_earnings_date`; the last 5 sessions (the Parquet keeps every one). **The end of the session, not all of it** — the feed is newest-first and a session's three pages hit the 3 x 500 ceiling (#730 item 4). It was the LEAPS scan's input; that scan is switched off (#730) |
| `earnings_tape` | session date, ticker, report date | `/earnings/{ticker}` of an earnings-set name on its evening: one row per report UW lists (EPS and estimate, expected move, the pre- and post-earnings moves, the straddle returns). A name fetched again lists its reports again, so a reader takes the newest session it holds **for that name** — a name is only in the set on the two evenings before its report, so one `max(session_date)` over the table would drop every other name. Read by the Earnings Radar board since #644 (need `earnings.tape`, reader `module_earnings_radar/stored_inputs.earnings_tape`), which asked `/earnings/{t}` live per scored name before that; served newest report first, the order UW's own answer has, because `rows[0]` is the upcoming event. A name whose newest session is more than one session old, or whose event is today or the next trading day, stays live |
| `option_far_contract` | session date, option symbol | The market's far-dated contracts of a session (#746): every contract the contract screener returns at 183 days or more and $50k premium or more, every underlying, with its volume by side, multi-leg volume, premium and the open interest at the session's start; then, read from the name's chain the next morning, `oi_next` and `oi_change`, `opened_share` (`oi_change / volume`, raw, not clipped; `out_of_range` when the change is larger than the volume), `ask_share`, `multileg_share`, and the `roll` flag with its `roll_partner`. `oi_status` is `pending`, `matched`, `stale` or `missing`; only `matched` rows carry the open-interest numbers. Every session is served. No label, no score |

**Datasets.** Four, all daily and all `starts_by_hand` (no automatic first run: the first one is started from the
CLI or Settings):

- `uw/option_list`: SPX and the option watch list, read at each run through `rcdata.read.watched("option")`. Per
  name and session: `expiry-breakdown`, then per expiry `option-contracts` (500 a page) and `greeks`; the stats
  (`spot-exposures/strike`, `greek-exposure/strike` and `/expiry`, `flow-per-expiry`, `volatility/term-structure`,
  `gex-levels`, `options-volume`, `interpolated-iv`, `iv-rank`) and `flow-recent`. Only in the observed window
  (from 30 minutes after the close until the next open, `core/calendar.py`): UW serves the chain and the prints of
  the current session only, so there is no backfill. A run stops starting names after 35 minutes and the next run
  goes on; a paged answer that moves between pages, or reaches the page ceiling, fails that name for the run
  instead of storing part of it.
- `uw/option_screener`: the earnings calendar of the next 28 days (`raw/uw/earnings_calendar/`, premarket and
  afterhours, 100 a page), the screener row of every name on it (`/screener/stocks`, 100 names a call), and the
  earnings set: once per name an evening, on each of the two evenings before its report, each name that passes the Earnings Radar gates and
  is not on the option list gets spot exposures by strike, GEX levels, term structure, options volume, the last
  50 prints, its earnings tape (`/earnings/{ticker}`) and, since avo's F2 (18 Nov 2026, for the Implied Probability
  board), its chain: `expiry-breakdown`, then `option-contracts` (no greeks) for the 12 expiries with the most open
  interest 1 to 365 days out, into `option_eod`. Once a session it also asks
  `/option-trades` for the market's far prints (`min_dte` 183, `min_premium` 50,000, 500 a call), paged back with
  `older_than` (UW ignores `page`), at most 3 calls.
- `uw/far_contracts` (#746, avo's universe "A: Tum piyasa" of 12 Nov 2026): in a session's observed window the
  sweep, `/screener/option-contracts` with `min_dte` 183 and `min_premium` 50,000, `type` call then put, 250 a
  page from page 0 until a short page, and never after 06:00 New York the next morning (UW's open-interest update
  would already show in it); from 07:30 New York on the next trading day until 06:00 the trading day after (before
  UW's next update), each swept ticker's chain, `/stock/{ticker}/option-contracts` with `min_dte` 175 (UW counts it from the day
  of the read), 500 a page. Every returned row is checked against its filter, because UW drops a parameter it does
  not honour without an error: a sweep row that breaks it fails the run, a chain row fails that name. A chain row counts only when its `prev_oi`
  equals the sweep's `open_interest`; a name UW has not updated yet is asked again by the next run, and a chain that
  breaks its filter or is refused fails that name only. About 420 to
  435 UW calls a session (10 for the sweep, one or two a ticker). The rule is the module doc of
  `rcdata/build/options/uw_far_contracts.py`.
- `ibkr/iv_history`: one year of IBKR's daily 30-day option implied volatility per list name, once (a name added
  later gets its year on the next run). Through `core/providers/ibkr_tws.py`, a minimal TWS socket client that sends
  only the handshake, `startApi` and `reqHistoricalData`: market data only, never an account, a position or an
  order (`ib_async`'s connect asks for the positions, so it is not used). 6 requests a minute, one at a time:
  politeness, not a provider limit (IBKR's 60 in 10 minutes binds bars of 30 s or less, not these 1-day bars).

The first week's raw size per list name is in each `uw/option_list` check note ("raw N KB a name"), the plan's F5
measure. Read through `rcdata.read`: `option_chain`, `option_strikes`, `option_expiries`, `option_daily`,
`option_prints`, `iv_history`, `option_screener`, `earnings_options`, `earnings_tape`, `far_contracts`, `far_contract_sessions`; a row of the earnings set carries
`uw/option_screener`'s status, an IBKR row of `iv_daily` `ibkr/iv_history`'s.

**The cards' earnings date (G6).** The next earnings date on the Asymmetric Growth, Fundamentals and Swing cards,
their earnings-week filter (and so the Relatedness and Value Chain filters, which read Asymmetric Growth's
`ticker_scores` dates through `universe.filter_fields`), and LEAPS' near-term event flag come from
`core/earnings_dates.py`. It reads
`option_screener_daily`'s newest session when that session is at most `MAX_AGE_DAYS` (4) calendar days old and not
after today (New York), each name's `report_date` on or after today. With no options file, a stale session or a failed
read, it reads the stored D26 `earnings_calendar` as before (#304), and Yahoo's `earningsTimestampStart` where neither
has the name.

**The Earnings Radar calendar (E5a, #273).** `app/runners._fetch_calendar_days` reads the same
`option_screener_daily` session as the calendar it used to ask UW for, through
`module_earnings_radar/stored_inputs.calendar_items`: one answer per `(report day, report session)` pair the
newest built session covers, empty pair included, with the calendar's own `expected_move_perc`,
`street_mean_est`, `full_name`, `marketcap`, `report_time` and closes. The session may lag the board's day by the
`options.screener` need's one session; the days it covers are `session < day <= session + DAYS_AHEAD` (the
builder's own 28). UW is still called for every pair outside that — and always for today's and the next trading
day's pairs, because UW moves the expected move, the estimate and the report time during the day for the names
about to report. Either path builds the event with `_calendar_event`, so `source` stays `UW` and the run
downstream cannot tell them apart; `input_sources["calendar"]` counts the stored and the live pairs and names the
session.

**The watch-list augmentation (#1138).** A watch-list name whose earnings time is "unknown" or whose date is only
an estimate is missing from the `/earnings/{premarket,afterhours}` calendar, so
`module_earnings_radar/watchlist_augment.augment_calendar` adds it from its screener row (#198 E15). That row came
from one live UW `screener_batch` until #1138 — the last UW calendar call the Earnings Fetch made. It now comes
from the served screener set first (`stored_inputs.screener_rows`: `next_earnings_date`, `er_time`,
`implied_move_perc`, `sector`, `full_name` and `marketcap` off the same `option_screener_daily` session, under the
same freshness gate), and the `screener_batch` is spent only on the names the store does not cover: a name absent
from the set, a row with no `next_earnings_date`, or a name reporting today or the next trading day (live for the
same reason the calendar pairs are). `input_sources["watchlist"]` counts the names each path answered, and a Fetch
whose watch-list names are all covered makes no UW calendar call at all. A store read that fails leaves every name
to UW; a `screener_batch` that fails keeps the names the store answered and the run goes on.

**UW calls a session** (`rcdata/build/options/__init__.py`, the plan's arithmetic: list names x (chain + stats +
prints) + screener pages + earnings-set names + far-print pages). A list name is 11 calls plus 2 per expiry plus one
per extra page; an earnings-set name 6 plus its chain (one `expiry-breakdown` and one call per expiry, 12, plus extra pages); the far prints at most 3; the screener one per 100 reporting names; the calendar one per day and report session plus
one per extra 100 names. With the planning numbers (25 expiries and 5 extra pages a name, SPX 400, 2,500 reporting
names, 150 in the earnings set: assumptions until the first week's real counts replace them) that is 3,343 calls
with SPX alone, 9,943 with 100 list names and 36,343 with 500, and UW has no day cap since avo removed it on
2026-11-10 (`rcdata/caps.py`); the other UW datasets spend the same UW day. The real count is per request, in `provider_calls_daily`, the builder's and the dashboard's together (#166).

**Raw retention.** A hook after each `options` swap (`rcdata/build/options/raw_retention.py`) removes the
per-session raw folders of `raw/uw/option_list/` and `raw/uw/er_options/` older than the retention, once one is
set: `RC_RAW_RETENTION_DAYS`, else the `dataset_schedule` row `retention_days` (`rcdata/retention.py`). It ships
unset, so nothing is removed and the Settings card keeps its warning. The Parquet, the calendar, the screener
answers and the IBKR raw files (the marker that a name has its year) are never removed.

### The SEC form reader (`sec_forms.duckdb`, #757)

One dataset, `sec/forms`, and two served tables (`sec_forms` serving schema version `1`). It **fetches nothing**:
`sec/filings` has already stored every document it reads — the newest annual report of each company of the
universe, its Annual Information Form exhibits, and the newest proxy — so this dataset's `fetch` is the cut itself,
and what it returns is the stale companies' documents for `to_parquet_all` to cut. Nothing reads this file yet; the
board cutovers are their own work.

**Why a kind of its own.** A 10-K's text runs from 160,000 to 870,000 characters, so the stored annual reports are
roughly 2.7 GB of text, and a schema bump rebuilds a kind whole (#649). Inside `fundamentals.duckdb` that text
would be rebuilt every time the Fundamentals board's numbers changed.

**Why one dataset and not two.** `dirty.dataset_dir` is `parquet/<source>/<name>` split on the first `/` only and
the build manifest scans exactly those folders, so two datasets writing into one `parquet/sec/forms/` would leave
both manifests empty and the served tables would freeze after the first build with every test green. One dataset,
one folder, two Parquet files (`sections.parquet`, `notes.parquet`) per `cik=<cik>` partition.

**Served tables.**

- `filing_section` — one row per (filing, document, section): `cik`, `accession`, `document`, `form`,
  `filing_date`, `report_date`, `section` (`whole`, `proxy`, `business`, `release`), `text`, `chars`, `quality`,
  `start_char`, `end_char` (where the section sits in `whole`; NULL for an uncut row and for a cut the rule
  altered), `method` (`uncut`, `header`, `aif` — how it was found, so a weak method can be withdrawn alone),
  `rules_version`, and the provenance `source_as_of`, `fetched_at`, `run_id`.
  A `business` row's `quality` may carry suspect flags after the label — `ok;suspect:S2,S4` — so **read it
  through `sec_sections.base()`, never by comparing the whole string** (#835). The flags are the ones the row's
  `rules_version` defines for the cutter that made the row: `sec_sections.Rule.item_signals` for a `header`
  row, `Rule.aif_signals` for an `aif` one. The two are not the same list — S3 and S6 ask a 10-K item question
  that an Annual Information Form has no answer to, so a correctly cut AIF read `ok;suspect:S2,S3` until #838
  split them. Two caveats, both of the same shape: the flags were added to this table while `ours-1` kept its
  name, and #838 changed `ours-1`'s AIF set under the same name, and the layer re-cuts a company only when its
  documents change or the version string moves — so `ours-1` rows written before those changes carry no flags,
  or the item set's answer for an AIF, and are indistinguishable from rows written after. The population becomes uniform when `SECTIONS_VERSION` next moves and re-cuts everything. `document` is in the key because one
  accession can hold more than one document this cuts: a 40-F's AIF exhibit is a second one.
- `filing_note` — one row per (filing, document, text block), from the filer's own inline-XBRL text blocks
  (`rcdata/sec_notes.py`): `concept` as filed, `taxonomy`, `local_name`, `kind` (`disclosure`, `table`, `policy`),
  `seq`, `top_level` and `parent_seq` (filers tag a note and then its subsections, so the same sentence belongs to
  two or three blocks; a character count that is not to be tripled sums the top-level rows), `context_ref`,
  `period_end`, `text`, `chars`, `rules_version`, and the same provenance.

**How the build loads it.** One `INSERT` per batch of `sec_forms.forms.LOAD_BATCH` (100) company partitions,
in `cik` text order and each batch sorted by `cik, accession, document`, so the rows land in the order one global
sort gave them. Not one statement over the whole tree: that sort carried the corpus' whole text at once — on
20 Nov 2026 the live file held 14,142 `filing_section` rows with 4.1 GB of text — and DuckDB pins a sort's string
blocks, so it had nothing to evict and the spill folder did not save it. Three builds in a row failed on
`failed to pin block of size 612.0 KiB (4.5 GiB/4.5 GiB used)`, the first after 6,590 s, while the day's work was
62 new sections (#1177). The peak of the batched load is a batch of companies' filings, whatever the corpus grows
to.

**The staleness rule.** `dirty.py` reloads Parquet into DuckDB; it does not cut. So the rule is `fetch`'s own: a
company is stale when the `(accession, document)` pairs on disk differ from the ones its `sections.parquet` holds,
or when any of its rows was written by another cutter version. That covers the three cases nothing else triggers —
our own rule version changing, a new proxy or 40-F exhibit appearing, and a document re-fetched under a new
accession. A filing that legitimately yields **no** note rows (no inline XBRL: 0.6 % of the stored annual reports)
does not read as stale on every run, which comparing expected rows against stored rows would have done. The
dataset's check reports rows per `rules_version`, so a half-migrated table is visible rather than inferred.

**Which cutter version.** Sections are cut by `rcdata/sec_sections.py` at `ours-2` and notes read at `notes-1`,
and every row carries the version that wrote it. `ours-2` (#755, #756, pinned here by #845) lifts the
200,000-character cap on the business section — so no row is labelled `capped` — fixes the five item defects, and
carries its own 40-F rule: six start titles and ten stop titles instead of one each, a contents test and a body
minimum in place of the cap, and a label (ok / overrun / missing) read through `cut_aif_business_q`. It was pinned
before the first full cut, because moving the version re-cuts every company in one build and that pass is paid
once. The Value Chain board (`module_value_chain/splitter.py`) pins the version `chain_filings` and `filing_item`
are cut at; neither table carries a version column, so that pin moves on its own change, with its own re-slice.
#847 moved it to `ours-2` as well, which is why the `capped` label and the `filing_item` CHECK that refuses `ok`
at the cap now follow `filings.caps_text()` rather than being written in: `ours-2` caps nowhere, so a long and
correct section is stored whole and `ok`. The stored rows were cut by `ours-1` until `reslice.apply` rewrites
them, which rides the first full SEC cut as one run; `filing_item` is re-cut by the fundamentals build. Since #841
the pin is no longer what the Value Chain board reads its text through: its corpus is `filing_section`, which
carries `rules_version` on every row, so the pin is what a row is compared against, and a row cut with another
version is read and logged rather than dropped.

`ours-3` exists in `VERSIONS` and **nothing reads it**: it is `ours-2` with one start guard added (#407 case (a),
a 20-F cross-reference written as its own line being taken for the start of Item 4), and moving a pin onto it is
its own change with a re-slice and the owner's before/after look, exactly as moving onto `ours-2` was.

**The `release` section** (#1214) is the one that is not a periodic report's: the EX-99.1 earnings press release
of the newest 8-K carrying item 2.02, stored whole and uncut, one row per filing. `sec/filings` fetches it beside
the documents it already fetches, typing the exhibit from the filing's `-index-headers.html` and never from its
file name (measured: on 22 filings a raw regex over those HTML-escaped tags matched 0, `html.unescape` matched 21,
and an `ex99` name filter matched 14). The Earnings board's R8 reads it through
`module_earnings_radar.store_reads.release_texts`, which drops a release filed on or after the report date being
scored, because the board scores before the print.

**Needs.** `sec.whole`, `sec.business`, `sec.proxy`, `sec.release` and `sec.note`, each with its own reader and
`ask_required` (the text is gigabytes: nothing is answered unless a company is named). The four section needs
share one table keyed by `cik`, so each carries a `where` on its own `section`; without it a summary would read
its state off another section's rows. `max_lag` is 400 **days** for the annual report's three and the notes, not
sessions: an annual report is up to fifteen months old by design. `sec.release` is 200 days, a reported quarter's
own bound.

### The SEC reader tool (`sec_reads.duckdb`)

One dataset, `sec/reads`, and one served table (`sec_reads` serving schema version `3`). Plans:
claude-coordinator `plans/2026-11-15_1119_sec-reader-tool-plan.md` and
`plans/2026-11-15_1722_revenuechaser_sec-reading-service-board-needs-plan.md`. `sec/forms` **cuts** a filing; this layer
**asks** a model about it, for every board, so no board keeps its own prompt pool, caps or answers table.

**A question is declared once** (`rcdata/build/sec_reads/questions.py`, `register`): its id and owner, the
companies (`scope`), their text (`inputs`), the system prompt and its `prompt_version`, the parser, the chunk size
(60,000 characters by default) and the writer (EVREN or the DeepSeek API; DeepSeek is always its Flash model). A
board adds one entry and reads the answers through `rcdata.read.sec_reads(ciks, question)` or the need `sec.read`.
`rcdata/build/__init__.py` registers the questions, and registration order is run order: `business.locate` and
`company.profile` (`sec_reads/company_profile.py`), then `debt.note` (#737; `sec_reads/debt_note.py`, prompt and parse
in `rcdata/sec_debt_note.py`), which the Fundamentals board reads through `core/fundamentals_layer.py`. The Value
Chain board reads `company.profile` the same way (#894 step 4, `module_value_chain/engine._service_profiles`),
keyed by the CIK its own corpus row states, so both share classes of one issuer see one answer. Each further
question is its own change. **Each is written by its own chosen writer** (`core/llm_jobs.py`, jobs `sec_reads.*`, #404), read once per
process so one run asks every question through one writer. The default is still the switch (`questions.PROVIDER`):
EVREN, the gate with no fee, unless the process names DeepSeek (`RC_SEC_READS_PROVIDER=deepseek`), which gives each
question its own call cap (avo, 19 Nov 2026: `debt.note` moved off the paid writer with the other two). A stored
`ok` answer is current whoever wrote it, so changing the pair asks nothing again.

**One asker** (`asker.py`), one pool per provider: EVREN 1 request at a time, DeepSeek 8. The HTTP clients are
`core/providers`' own, called with `use_cache=False`, so `llm_cache` gets nothing from the tool. On homesrv the
calls go through LiteLLM by unit setting (the clients' base URL from the environment), not by code. The asker
refuses to start while the Relatedness board's model job holds its `relatedness` lock (one model job at a time),
and stops a run after 15 minutes of asking, leaving the rest stale for the next run, so a queued pass never holds
the builder lock for hours; a long read runs fetch-only (`--no-duckdb`) under its own lock instead.

**Served table.** `read_answer` — one row per (question, prompt version, company, filing, document, input hash,
provider, model): `filing_date`, `status` (`ok`, `partial`, `empty`, `no_text`), `attempts`, `answer` (the parsed
JSON), `raw` (the replies, one per chunk), `missing_chunks`, `rules_version` (the cutter version of the text), and
`fetched_at` (when it was asked) and `run_id`; since version 2 also `form` and `input_kind` (what was read, so a
fallback shows), `calls`, `tokens_in` and `tokens_out` (what the key has cost over every attempt) and `error` (how
the last unusable call failed); since version 3 `report_date` (the period the filing reports on). The input hash is the hash of the system prompt and the question's staleness text:
the text sent, or `Input.hash_text` when a question leaves its context lines out of it, so a changed context does
not pay for the answer again. An `ok` answer on (question, prompt version, company, input hash) is current
**whoever wrote it**: changing a question's writer asks nothing again, and the reader serves every writer's rows,
an answer under the question's current prompt version first, then the newest filing. Every call writes one row to the request log (the
question, the company, the chunk, and `ok`, `unusable reply` or the error with its words). One asker at a time,
whoever started the run (the lock `sec_reads_ask`); a question is finished and in Parquet before the next one is
planned, so registration order is run order. A writer that is down (a 429 after the client's own retries, a 5xx, a capacity
refusal waited out) skips that writer's questions and does **not** fail the run (`reads.outage`): every company stays
stale for the next pass and the note names the writer, because a failed run exits 1 and the chain behind it does not
run that night. A rejected key still fails it, and a run whose first five replies are all unusable stops as failed and
stores none of them; a key at three attempts is tried once more after 30 days; and nothing is asked while the
live table holds more `ok` answers than the Parquet tree (a lost tree must not start a full re-read). **No mass
re-read without the owner's word:** a run asks by itself only a company's new filing and a retry; a DeepSeek question at most
200 companies a day (`RC_SEC_READS_DAY_CAP`; the owner's number, 16 Nov 2026), going on the next day, and an
EVREN question with no day cap, because EVREN has no quota; a changed prompt, a re-cut text, a question's first pass and more than `NEW_COMPANY_LIMIT` (100) companies with no answer at all in one run (a change of scope, not a company's first report) wait for a request that says `"reread": true` (the
CLI's `--reread`). The run writes and converts every answer inside the ask lock and hands the builder no raw file.
**The daily check runs at night:** the dataset's day starts at 19:00 New York (`reads.DUE_FROM_NY`), so the hourly
pass that finds it due is 19:05 New York, 01:05 Amsterdam (the owner, 16 Nov 2026), once its file's automation is on
and a first run was started by hand. The Settings page and the Grafana RC Fetch board show that next pass and the
dataset's `limits` line (EVREN no day cap, DeepSeek 200 companies a day).
**It is also a job of its own:** the registry row RC-SEC-Read (`core/automations.py`, unit `life-rc-sec-read`, daily
01:10 Amsterdam, 90 min limit) runs `rcdata.build run --once --dataset sec/reads`, so the check has a row on the
Settings page and on the Grafana Jobs board, and it runs whatever the file's automation says. 01:10 is five minutes
after the hourly pass, so the two never start together.
A question's
input may carry its own ready user messages (`Input.messages`) when a plain 60,000-character slice is not what
its prompt sends. A transient error (a timeout, a 5xx) gives up on that company only, with no attempt counted,
and stops the run at the fifth such company. A question may name `max_calls`, the most calls one run sends for it
(a DeepSeek question must); the companies past it wait for the next run. An unusable reply is counted on its key; after three the company waits for a new
filing or prompt, and a `partial` answer is never replaced by an `empty` one. Parquet: one partition per question,
`parquet/sec/reads/question=<id>/answers.parquet`, plus one raw file per run under `raw/sec/reads/`; the Parquet
tree is the durable copy of the answers.

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
- **One weekly job (avo, 8 Nov 2026)** — `python -m RevenueChaser.rcdata backup`, registry row RC-Backup-Weekly
  (Sunday 03:30, unit `life-rc-backup-weekly` from the home-server repository, 60 min limit). Everything goes to the
  HDD under `RAW_STORE_BACKUP_DIR` (an empty value is refused: exit 2, nothing written) as `weekly-v2/<step>/`, one
  folder and one `.manifest.json` per step (the sha256, size and table fingerprints of every copy kept, the step's
  `last_run`, `last_ok` and `last_error`). Of every file the newest 4 copies stay, the oldest deleted only after
  the new copy verified. Nothing leaves the machine. Raw downloads and Parquet are not copied: they are fetched
  again. A step that fails is logged and the other steps still run; the exit code is then 1. A failed copy leaves
  nothing (no `.tmp`, no manifest entry) and prunes nothing. The steps, in the order they run:
  - `duckdb` — the DuckDB files the builder writes from Parquet (`serving.KINDS`), each copied from one open handle
    of the live file, verified by row count and `sum(hash(row))` per table, then renamed. `rcdata backup
    --only <kind>` takes a copy by hand before a deploy; it is tagged `manual`, kept on its own count (1) and never
    costs a weekly copy.
  - `boards` — the board-result DuckDBs the dashboard writes live: `raw`, `edgar`, `history` and `prices` while they
    exist, and every `<RC_STORE_DIR>/boards/<board>.duckdb` (DECISIONS S6). Each is first `CHECKPOINT`ed through
    `rawstore`'s peer-lock budget (a dashboard holding the file is waited out), so the `.wal` is in the main file,
    then copied and verified like a `duckdb` copy; the manifest records the file's `store_schema` version.
  - `ops` — the operational state, only the tables worth keeping: the watch lists (`watchlist_*` and their changes),
    `dataset_schedule`, `switch`, `kind_automation`, `dataset_automation`, `job_runs2`. The queue, the read log, the
    counters and the check rows are left empty in the copy. On `ops.sqlite` SQLite's online backup API, built on
    local storage and copied once finished; on Redis a filtered snapshot written in one transaction and compared row
    for row.
  - `uw-raw` — the raw files no provider serves again: `<MARKET_RAW_DIR>/raw/uw/` (Unusual Whales keeps no history)
    and the SPX Gamma tree, into `uw-raw/<ISO week>/`. A file unchanged since last week's folder is hard-linked to
    that copy, so four weeks cost one copy plus the new days; a new or changed file is copied and verified by size
    and SHA-256; a file modified in the last 10 minutes waits for the next run; a `.json` that is not valid JSON
    never replaces a copy that is. The newest 4 week folders stay.
  - `edgar-docs` — the `edgar_docs/` tree as one verified zip (member CRCs and the document count); an unchanged
    tree (same `stat` fingerprint in the zip comment) keeps the newest zip instead of zipping again.
- **One daily step (avo, 17 Nov 2026, decision C of the spine classification plan)** — `rcdata backup --only
  watchlists-daily`, never part of the weekly run: a copy of `boards/watchlists.duckdb` (avo's own lists, which
  nothing rebuilds) into `weekly-v2/watchlists-daily/`, checkpointed and verified like a `boards` copy, tagged
  `daily`, the newest 7 kept. A day the file is unchanged (its table fingerprints equal the newest daily copy's)
  writes no copy and still records a good run. The weekly `boards` copy of the same file goes on. Its card row is
  red once the last good run is older than 2 days. It runs every day at 03:10 (avo, 18 Nov 2026) as registry row
  RC-Backup-Watchlists-Daily (unit `life-rc-backup-watchlists-daily` from the home-server repository, 15 min limit),
  held by the Backup switch like the weekly job.
- **What shows it** — the Data/Health "Weekly backups" card (each step's last good run and the error of a failed
  one, the newest copy of every file and how many are kept; amber past 8 days, red past 15) and the Health strip's
  backup alert; the home-server health board and Grafana read the same manifests per step.
- **Restore** — `python -m RevenueChaser.rcdata restore --step <step> (--file <name> | --latest [KIND])
  (--into <dir> | --live)`. Every restore first verifies the copy against its manifest (sha256, size, the recorded
  table fingerprints or the kept tables' row counts); an unknown or changed copy is refused and nothing is written.
  `--into <dir>` writes the verified copy there (a `uw-raw` week as the whole folder) and touches nothing live.
  `--live` puts a `duckdb` or `boards` copy in place of its live file: it refuses while a RevenueChaser process
  uses that store or a `<name>.duckdb.wal` lies beside the target (#214: DuckDB would replay it on top of the
  restored file), keeps the current file as `<name>.duckdb.pre-restore`, copies and fingerprints the result.
  `ops --live` is a step by hand: on Redis `python -m RevenueChaser.rcdata.ops_copy --sqlite <copy>` into an EMPTY
  namespace (emptying one is a delete and needs avo's word); on `ops.sqlite` copy the file into place with the
  dashboard stopped. `uw-raw` and `edgar-docs` are restored `--into` a folder and moved (or unzipped) by hand.
- **The drill** — `python -m RevenueChaser.rcdata restore-check` (unit `life-rc-restore-check`, the first Sunday of
  the month 02:30) restores the newest `ops` copy and the newest board copy into a scratch folder, verifies both the
  way a restore does, removes what it wrote and exits 2 on any mismatch; the home-server health board shows it.
  Once a quarter, by hand (about 30 minutes): restore the newest `boards` and `duckdb` copies `--into` a scratch
  folder, start a dashboard on it (`RC_STORE_DIR` and `RC_MARKET_DIR` pointed there, another port) and open the
  Relatedness and Settings boards.
- **The start gate (#212)** — no live write without a fresh verified backup. `rcdata/backup.py`
  `stale_problem()` reads `weekly-v2/duckdb/.manifest.json`'s `last_ok` (stamped only by a run in which every copy
  verified) and answers one sentence when the newest verified copy is older than `backup.STALE_DAYS`, or when
  there is none. `rcdata/build/base.py` `backup_problem()` is the builder's name for it: the CLI reads it once
  before the writer turn and exits 3 without touching the store (a stale copy never becomes fresh by waiting), and
  `POST /api/build/request/<dataset>` refuses a Settings Fetch or Backfill press with 409 so nothing sits in the
  queue that the builder would refuse. A fetch-only run (`--no-duckdb`) is not gated: it opens no serving file.
  Pause, Resume, the switches and the timer are not gated either — they only stop writes. Silent where no backup
  folder is configured (a developer's machine, CI) and where `RC_BUILD_BACKUP_GATE=0` turns it off.
- **What is not pruned** — the old `market/`, `market-heavy/`, `weekly/`, `spx_gamma_mirror/` and snapshot files
  the retired nightly jobs left in `RAW_STORE_BACKUP_DIR` (about 55 GB on 8 Nov 2026) are not touched by the weekly
  job, which only looks inside `weekly-v2/`. Removing them is a deliberate step on avo's word.
- **Compaction** — DuckDB re-uses freed blocks but never shrinks the file, so
  every `replace_all`/DELETE leaves it larger than its data. With the dashboard
  and scheduled jobs stopped: `python -m RevenueChaser.scripts.compact_store`
  (applies the manifest first, then `COPY FROM DATABASE` into a fresh file,
  verifies row counts, swaps; the old file stays as `<name>.duckdb.pre-compact`
  until you delete it). On the live store: raw 591 → 429 MB, edgar 507 → 81 MB.
