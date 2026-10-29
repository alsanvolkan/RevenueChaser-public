# How the RevenueChaser data system works

State: main 7acbe1a (merge of PR #268), read 2026-10-04. Parts marked **Planned** come from the data study and
change as its decisions land. Cites are `path:line` in this repo; `:NNNN` alone is a line of the MANIFEST in
`core/rawstore.py`.

## 1. The short version
- Providers (Unusual Whales, Yahoo/yfinance, SEC, OpenFIGI and others, §6) are called only by **Fetch** jobs. Each
  job writes what it fetched into a DuckDB file under `.store/`, then the board computes its results and writes a
  page to `.cache/`.
- **Refresh** never calls a provider: it rebuilds a board from `.store/` (docs/data_retrieval.md:9-23).
- There are three DuckDB files (`raw.duckdb`, `edgar.duckdb`, `prices.duckdb` for the daily price tables since #156; a fourth, `history.duckdb`, is made by the first recorded board run) plus two kinds of files on disk:
  SPX Gamma day folders and the filing HTML in `edgar_docs/` (§3).
- Most tables keep only the latest value per key; a few keep every run (§4). Several datasets are read during a run
  and never stored at all.
- The full per-dataset trail (source → writer → file and table → key and rule → how long it stays) is in
  [data_flow.md](data_flow.md) and drawn in [data_flow.svg](data_flow.svg).

## 2. Layers
| Stage (docs/data_architecture.md) | What it is here |
|---|---|
| 1 External acquisition (:141) | Provider calls inside Fetch jobs (§5, §6). |
| 2 Raw caches (:146) | Provider answers stored as rows: e.g. `swing_ohlc`, `identity_source_cache`, `leaps_cache`, `edgar_financials`. |
| 3 Normalized / derived (:180) | Computed results: e.g. `security_master`, `ticker_scores`, `rotation_snapshots`, `comov_*`, `relatedness_*`. |
| 4 Rendering (:201) | Board pages in `.cache/`, rebuilt by Refresh. |
| 5 Serving (:210) | The Flask dashboard (usually port 8000) that runs jobs and serves pages. |

Today raw and derived tables share `raw.duckdb`. Only `identity_source_cache` among the tables in use carries both
`source` and `fetched_at` on every row (:1285); `ticker_scores` has `source` only (:1149).

## 3. Store files
| File | Path (code) | Tables | Holds |
|---|---|---|---|
| raw.duckdb | `.store/raw.duckdb` (core/rawstore.py:52) | 60 | Provider answers, computed results, settings, run logs; 7 tables are declared but never written. |
| edgar.duckdb | `.store/edgar.duckdb` (core/rawstore.py:55-57) | 6 | SEC statements, filing metadata, Value Chain filings and mentions. |
| prices.duckdb | `.store/prices.duckdb` (`prices_db_path`, core/rawstore.py) | 4 | Daily prices (#156, its own file since 2026-10-09, before the fill): `price_bars_daily`, `price_actions` and their watermarks `price_checks`, written by `core/prices.py` in one transaction per chunk; the SPX daily levels `index_levels_daily` (D03, #293, moved here 2026-10-10, before its writer), written by `core/index_levels.py` (M-SPX-0 #376): yfinance `^GSPC`, the last step of RC-Daily-Bars (at most 1 request a night) and the attended one-time `period="max"` fill, append, first write of a key wins. Made by the first store call; a store from before has the two tables (from #293 also the empty `index_levels_daily`) moved over from raw once (copied, checked with `EXCEPT`, then dropped; each table added to the marker `price_tables_to_prices_db` in `migration_manifest` in the transaction that drops it; what a second process starting at the same time already moved is left alone, logged at INFO). No schema bump. |
| history.duckdb | `.store/history.duckdb` (`history_db_path`, core/rawstore.py:65-70) | 17 | Result history of #278 (2.6-1, schema 11): `result_runs` plus 16 run-keyed row tables, written only by `core/history.record_run`; so far the AG Fetch (#332, `ag_scores_history`) the Fundamentals Fetch (#278, `fundamentals_scores_history`, `fundamentals_gates_history`) and the Swing Fetch (#278, `swing_scores_history`, `swing_gates_history`) and the Earnings Fetch (#278, `earnings_cards_history`) and the Financials Fetch (#385, `financials_report_history`) call it; the Co-movement Fetch (#379 D2) writes a header only, since its runs stay in raw `comov_*` with the Fetch's `run_id`. The first recorded run creates the file; the nightly backup and restore cover it once it exists. |
| SPX Gamma day folders | `module_spx_gamma/data/raw/<date>/` (module_spx_gamma/config.py:21-25) | files | UW GEX and greek files, flow per strike, VIX family, CFTC TFF, 8-week SPX OHLC per as-of date, and a `manifest.json` (requested and effective date, per-file as-of versus live, rows, limit hit; a backfill never overwrites a complete day). Refresh rebuilds the newest day from these files with no network (module_spx_gamma/run.py:364). A Fetch or calendar click on a stored past date (its manifest exists) rebuilds that day the same way, through the `spxgamma_stored` job that has no UW client (no auth probe), and never rewrites it; only an explicit `force` refetches it, and only the newest day moves `latest.json` (#179 D5, module_spx_gamma/run.py:306-343; app/dashboard.py `run_spxgamma`). In the nightly backup since #276 (see Backups below). A second writer, the `spxgamma_history` job (D1, the 2-year history pull; `module_spx_gamma/history_pull.py`), adds past days that have no manifest through `fetch_snapshot(history_only=True)`: dated calls only, spot from that day's `index_levels_daily` close, no live file, manifest `origin: history_pull`; it never replaces a complete day, never ends after the newest Fetch day, and is never queued or running beside a board Fetch in the dashboard (both routes check and submit under one lock; a CLI run in another process is not covered). It also writes `module_spx_gamma/data/history_pull/` (one iv-rank series per range and one counts file per run). |
| Filing text | `edgar_docs/` | files | 10-K, 20-F and 8-K HTML; metadata in `edgar_documents`. |
| Board pages | `.cache/` | files | Regenerable; Refresh rebuilds them. |

- **Serving and ops state:** the builder-written serving files and `ops.sqlite` (watch lists, status) live under `rcdata/`; see `docs/data_architecture.md` (the `rcdata` section).
- **Schema version:** `_SCHEMA_VERSION = "12"` (core/rawstore.py:118; v12 = see `docs/data_architecture.md`; v11 = history redesign C: 8 `<table>_changes` tables in raw, filled by the store inside every write to the 8 tracked tables and seeded at the first call; `result_runs` plus 16 result tables in history.duckdb; `price_bars_daily` and `orphan_lookups` rekeyed; v10 = the 22 G1 tables of #292, manifest only; #303 added a 9th change table, `etf_holdings_changes`, without a bump: a CHANGES line is an addition). A bump needs a manual `.store/` checkpoint and
  its own gated PR (CLAUDE.md, Data safety).
- **Tables are created on first use** from the MANIFEST unless `RC_STORE_AUTO_ENSURE=0` (core/rawstore.py:2293-2294).
- **Backups:** the RC-Store-Backup task runs daily at 04:00 with `--keep 5` and covers raw, edgar and `prices.duckdb` (2 `prices_*` snapshots with `--keep-prices` and no weekly copy, #156: the price tables are append-only, so the newest snapshot holds every row) (and `history.duckdb` once it exists: 5 `history_*` snapshots with `--keep-history`, plus its own weekly copy, #287) plus a zip of
  `edgar_docs/` (core/automations.py:118-123; scripts/backup_store.py:10-12). Since #336 the zip carries a fingerprint
  of its tree (relative path, size, modified time) in its zip comment, and a run whose tree is unchanged keeps the
  newest archive instead of zipping again (`archive_edgar_docs`; log `backup OK: <path> (unchanged, kept)`). Restore:
  `python -m RevenueChaser.scripts.restore_store --latest raw`. Since lane A of phase 3 the same run also:
  - **mirrors the SPX Gamma tree** (#276) into `spx_gamma_mirror/` in the backup folder, additive, each file verified by
    size and SHA-256 (scripts/backup_store.py:603, :96). `brier.json` and `spx_gamma_gex_history.json` are rewritten in
    place, so each change also leaves a dated copy in `spx_gamma_versions/` that is never replaced or pruned and is
    hashed against the verified mirror copy before it gets its name (scripts/backup_store.py:99-100, :475). Only an SPX Fetch records
    the day's scenario probabilities in `brier.json`; a Refresh rebuilds the day without writing it, and a record that is
    already scored is never replaced (module_spx_gamma/run.py `build_day(record=...)`, module_spx_gamma/brier.py
    `record_probs`; SPX-4, #376). A source file
    that is not valid JSON never overwrites a good mirror copy: the run keeps the old copy, logs a warning and exits 1
    (scripts/backup_store.py:500-542);
  - **keeps the first verified raw snapshot of each ISO week** (#277) in `weekly/`, never pruned, outside the prune and
    restore globs (scripts/backup_store.py:103, :299). Switch off with `--no-weekly` or `RAW_STORE_BACKUP_WEEKLY=0`
    (core/config.py:57). The change history (#279) is meant to replace it;
  - **records every file it writes** (#309 L1) with its size and SHA-256 in `.backup_manifest.json` (and what it
    wrote and kept off-machine in `.offmachine_manifest.json`, both in the local backup folder) and, every 7 days
    (`--scan-every-days`), re-reads the local folder against it, read-only, exit 1 on a mismatch
    (`.integrity_scan.json`); the run ends by logging both folder sizes and writing them to `.backup_sizes.json`,
    which the Settings Backups card and the Automations row read (thresholds `RAW_STORE_BACKUP_WARN_GB` 20,
    `RAW_STORE_BACKUP_MIRROR_WARN_GB` 320; scripts/backup_store.py `integrity_scan`, `record_sizes`). The off-machine
    folder is never walked;
  - **copies the backup folder off this machine** (#275) when `RAW_STORE_BACKUP_MIRROR` names a folder
    (core/config.py:63; empty = off), into the sub-folder `RevenueChaser-backup` of it (the folder may be an existing
    Drive folder: nothing outside the sub-folder is written, pruned or deleted; scripts/backup_store.py:107, :672):
    the newest history snapshot every run (newest `--keep-mirror`, default 3, stay), the newest raw/edgar
    snapshot and edgar_docs zip at most every 7 days (#349: `--mirror-big-every-days`, `--mirror-big-now`; newest
    `--keep-mirror-big`, default 2, stay; Drive keeps pruned copies in its quota-counting trash for 30 days), the
    three mirror folders and, last, the newest prices snapshot once every 7 days with 1 copy kept (#156, avo
    2026-10-09: `MIRROR_PRICES_EVERY_DAYS`, `KEEP_MIRROR_PRICES`; last, so a failed upload of the biggest file costs
    no other copy), each file verified in a temporary name before it is renamed; the weekly copies stay local,
    and nothing else is ever deleted there (scripts/backup_store.py `offmachine_copy`). Its log line
    `off-machine copy OK|FAILED <iso>` feeds the Automations row of RC-Store-Backup, red when no folder is set, no copy
    was made yet, the newest attempt failed or the last good copy is 48 h or older (core/automations.py:592,
    `offmachine_state`). Showing the same age under Settings / Data Health comes with that page;
  - **keeps going after a failed step:** every step runs inside one helper that logs `<step> FAILED: <why>` (any
    exception, an unexpected one with its type and stack) and goes on, the exit code is 1 if any step failed, and only
    the off-machine copy is skipped that night; it then logs
    `off-machine copy FAILED <iso>: skipped, ...` so the row is red at once (scripts/backup_store.py:875-941, `step` and the skipped line in `main`).

## 4. How rows are written and how long they stay
Write rules from the MANIFEST (core/rawstore.py:564-566, `_apply_write` :2486-2533, `replace_all` :2694-2731):
- **upsert:** one row per key; a new fetch overwrites that row.
- **replace per group:** all rows of the group (for example one ticker or one date) are deleted, then the new rows
  are written.
- **History mode (#281, J2):** every MANIFEST entry declares `history_mode` (`event`, `session_snapshot`,
  `change_history`, `latest_cache` or `operational`; `rawstore.HISTORY_MODES`). It is a label: no write rule reads it
  yet. The 69 pre-G1 tables are 27 event, 7 session_snapshot, 13 change_history, 22 latest_cache; the 22 G1 tables
  keep theirs (8 event, 7 session_snapshot, 6 change_history, 1 operational). `core/tests/test_manifest_history.py`
  pins the pre-G1 modes and the 22 G1 entries (against `core/tests/ddl292_frozen.py`).
- **replace all:** the whole table is deleted and rewritten in one transaction. An empty batch is refused unless the
  caller passes `allow_empty=True` (#284): `save_watchlist` and the legacy migration do. Relatedness peer scores
  (`module_relatedness/store.py:155-166`) and Value Chain edges (`module_value_chain/engine.py:260-278`) keep the
  stored rows and log why when a run produces nothing.
- **`equity_universe`, full or partial (#146):** the universe job replaces the table only on a *full* pull: every
  market-cap chunk ran to the total Yahoo reported and the screener returned at least 90% of the stored names that
  carry a quote (`FULL_PULL_MIN_SHARE`, core/universe_fetch.py:68; the decision at :669-672). Anything else is
  *partial*: it upserts the quotes it got and the listed names not stored yet, deletes no stored name, leaves a name
  the screener missed untouched, and does not run the cap backfill (:626-645). The state is `__fetch__` in
  `equity_universe_provenance` (`partial` or `full`, core/universe_fetch.py:75, :178-196) and the boards show "partial
  fetch". A Refresh never writes the universe (#145).
- **append:** rows are added; nothing removes them.
- **history flag (`"history": true`, #286):** `replace_all`, `update_columns`, `delete` and `clear` raise, and so does
  raw SQL that rewrites the table. DuckDB's own parser and binder read that SQL (#308): whatever DuckDB plans as
  `DELETE` or `TRUNCATE`, `UPDATE`, `DROP`, `CREATE OR REPLACE TABLE`, or a merge with an UPDATE or DELETE branch
  (`MERGE INTO ... THEN UPDATE/DELETE`, `INSERT ... ON CONFLICT ... DO UPDATE`, `INSERT OR REPLACE`) on the table, under
  any spelling of its name, is refused, and so is text that names it but that DuckDB cannot parse or plan.
  `write_rows` keeps the first row written for a key whatever the write mode (core/rawstore.py:1821-1826, SQL guard
  :1804-1963, `_apply_write` :2486-2533). Outside the guard: `ALTER` and a caller's own
  `connect()` handle. The schema-11 change and history tables carry the flag, and since #286 so does the forecast
  ledger (its writer since #128 only appends). The flag also blocks a delete by `run_id`, the #128 plan's rollback of a
  bad freeze: that rollback now needs an explicit `rawstore.connect()` handle, outside the guard, with the owner's OK.
- **schema drift (#285):** `_ensure_table` still adds only a missing column. A retyped column, a changed PRIMARY
  KEY and a live column the MANIFEST no longer has are recorded in memory and logged once per process, and
  `rawstore.schema_drift()` lists them (core/rawstore.py:2044-2149). A table with stale `compress` columns is rebuilt
  from the MANIFEST right after, which replaces what the check found: the log says so and the table's records are dropped. Since #312
  item 1, `ensure_all_tables` keeps what its apply saw in `store_meta` key `schema_drift` (`_persist_drift`:
  `{"recorded_at", "drift": [...]}`, written only when the list changed; an empty list clears it; best effort,
  never raises). Nothing else is written to the store.

What that means for history today (details and cites per table in [data_flow.md](data_flow.md)):
| Retention | Tables and files |
|---|---|
| Latest value per key only | `ticker_payload_cache`, `swing_info` (a failed, empty or stub refetch keeps the expired row and serves it marked stale, #259), `leaps_cache`, `oipd_chain`/`oipd_meta`, `etf_holdings` (each change of an ETF's list or provider is also kept in `etf_holdings_changes`, #303), `rotation_holdings`, `sec13f_enrichment`, `sec13f_security_meta`, `identity_source_cache`, `security_master`, `index_constituents` (per index), `sec_company_tickers`, `sec_company_sic`, `chain_filings`, `edgar_financials` (fetched once, never refetched) |
| Whole table rewritten each run | `equity_universe` (membership and quote; a full universe pull only, see §4), `ticker_scores`, `swing_scores`/`swing_gates`, `fundamentals_scores`/`fundamentals_gates`, `sec13f_totals`, `relatedness_scores` |
| `ticker_payload_cache` flags | an AG info-only fetch is stored flagged `info_only` (annual `[]`) when the name has no full row, and merged into the full row (with `_info_fetched_at`) when it has one; `scripts/purge_info_only.py` removes the flagged rows on a code rollback (#150) |
| Gate rows | `swing_gates.passed` and `fundamentals_gates.passed` are NULL when the rule could not be evaluated (a missing input), not 0 (#151). The Rule of 40 gate is one row (`rule_id = ro40`, pass/fail of growth + FCF margin ≥ 40; was two input rows that were always 1, #148), so `fundamentals_gates` has 7 rows per scored name, not 8. No schema change |
| A rolling window | `swing_ohlc`: the last 2 years per ticker, replaced on each fetch, by one writer (`core/ohlc_cache.write`, C8 #383 R3) that only the Swing and Fundamentals SPY fallback still calls; they read it for that fallback only (they score from `price_bars_daily` since #383 R3 C12). Co-movement and the Relatedness peer market read `price_bars_daily` since #379 (D01 switch; module_comovement/ohlc.py), so Co-movement no longer downloads or writes it. A series is reused for 1 day, but a Fetch downloads it again when its last bar is the latest complete session or older and it was cached before that session's close + 30 min, so a Fetch after the close refetches what an earlier Fetch cached mid-session or the day before (module_swing_potential/indicators.py:226-232, core/ledger.py:162; #128). A series with fewer than 252 bars and fewer than stored does not replace the stored bars unless it is a new listing: it shares no session with them and starts more than 30 days after the stored last bar (#261; F2b, card "30 gün şartı"). Swing serves the stored series stale and counts it. A Refresh never downloads |
| Changed in place, every change kept | `watchlist` (a LEAPS pin click adds or removes its own ticker in one transaction, `core/rawstore.toggle_watchlist`) and `board_settings` (a Save upserts and drops in one transaction, a Reset deletes the board's override rows in one statement; a lock keeps the two from overlapping); the store appends 'present' and 'absent' rows to `watchlist_changes` and `board_settings_changes` (source `manual`) in the write's transaction (#306, D36). Data Health row "Your inputs" |
| Kept, growing | `forecast_ledger` (one set of picks per source per session, never updated), `leaps_flow_prints`, `sec13f_holdings`/`sec13f_submissions`, `rotation_snapshots` (one per date), `comov_*` (one per as-of), `edgar_documents` (append, first write wins, #299), Relatedness theme runs (`theme_runs`, `relatedness_themes`, `relatedness_theme_tags`, `relatedness_theme_members`, `relatedness_theme_etfs` and `ticker_themes` keep every run; a cutover moves `theme_runs.is_current` and rewrites only that run's own rows, module_relatedness/themes.py:504-535, :1114-1200; with `--coarse sector` (#108) it first rebuilds that run's coarse level from the 11 Morningstar sectors: each medium is parented to `<run>:sec-<slug>` of its modal sector and the run's other coarse rows are deleted, themes.py:777-861; card 10, CL-A REL-1; a fit covers the $300M classification universe unless `--floor`/`--tickers` widen it, themes.py:166-191, #108 S0; every NMF of a run uses one fit setting, default nndsvda init and 500 iterations since #108 S1 (nndsvd / 60 before), `--rebuild-themes --nmf-init/--nmf-max-iter` change it and the run's summary names it, `theme_runs` has no column for it, themes.py:34-53), `sec_submissions` (one row per filing) and `sec_submissions_cik` (one row per header change, #299), `edgar_facts`, `llm_cache`, SPX day folders, the E0 inputs (RC-Earnings-Inputs, #372: `earnings_calendar`, `short_interest`, `economic_calendar` and `estimate_snapshots` one snapshot per session, `macro_series` one row per observation; first write wins), the SEC files `sec_exchange_file` and `sec_mf_file` (one row per change and one 'absent' row per key that leaves, written by RC-Daily-Bars' first step, #301 F0) |
| Derived, recomputed | `forecast_outcomes` (#380 S0): one row per frozen pick and horizon (30 and 90 calendar days), rebuilt from `forecast_ledger` and the bars by `core/outcomes.py` (since PR 10 in `ledger.duckdb`, from the prices store); a row without the first session's open has no reference and stays open; a 2.5x jump from the previous close (a split with no action) is `split_check`, not final; a closed row of the current model version is final, an open one is redone each night |
| Pruned | `spx_gamma_gex_history.json`: newest 10 days (module_spx_gamma/run.py:28, :58-84; each version stays in `spx_gamma_versions/` of the backup folder since #276) |
| Not stored | Earnings run results (only the HTML report), UW daily bars used during runs, the yfinance macro proxies (the 8 FRED series are stored in `macro_series` since E0 D27) |

## 5. Jobs and schedules
- **Scheduled** (Windows Task Scheduler, core/automations.py:117-188): RC-Store-Backup 04:00, RC-Relatedness-OffPeak
  12:30, RC-ValueChain-Sync 13:00, RC-Daily-Bars 22:35 (first, best effort on weekdays, the SEC exchange and fund
  files: `core/sec_files.py`, at most 2 SEC requests a night, never a different exit code, #301 F0; last, best
  effort, the `^GSPC` daily levels: `core/index_levels.py`, at most 1 yfinance request a night, none when current
  or before the attended `--index-fill`, never a different exit code, #293 M-SPX-0),
  RC-Screeners-Daily weekdays 23:00 after RC-Daily-Bars
  (the universe CLI `app/universe_cli.py`, #129 S2; not installed by default), RC-Index-Lists Saturday 10:00,
  RC-Earnings-Inputs Tuesday to Saturday 01:30 after RC-Daily-Bars (`scripts/nightly_inputs.py earnings`, E0 #372; at
  most 200 UW, 10 FRED and 2000 yfinance reads a night, no SEC call; not installed by default),
  RC-IBKR-Login-Check 09:15 (`scripts/ibkr_login_check.py`, #162: one TCP connection to the configured host and
  port plus the TWS API version handshake and nothing else — no account, position, order or execution is ever
  asked for — writing **no table and no store file**, only its own record under `.cache/ibkr_login/state.json`,
  which the Data Health "IBKR Gateway login" card reads; off until `RC_IBKR_LOGIN_CHECK` is set, exits 0 whatever
  it finds, not installed by default).
- **Nightly chain rule (#129, core/automations.py:197-225, :697-782, :784-871):** an entry with `after=<name>` keeps
  its own time as its not-before time (0 to under 12 h after the predecessor's) and, when it starts, waits up
  to `chain_wait_minutes` for the predecessor's run of tonight (the newest run in the predecessor's log that started
  at or after its last scheduled time) to exit 0; otherwise it exits 5 without running. The predecessor must run
  daily (`check_chain` refuses one with `days`). Three pairs are wired: S2 RC-Screeners-Daily after S1 RC-Daily-Bars
  (#129 A1) and S3 RC-Earnings-Inputs after RC-Daily-Bars (E0 #372); S4 adds its own with its job. (RC-Forecast-Outcomes
  and RC-Forecast-Baselines were retired in PR 10: the outcomes and baselines are builder datasets of the `ledger`
  kind since then.)
- **Nightly inputs steps (C25a/K11, #383, core/nightly_inputs.py; core/automations.py:228-255):** an entry whose
  command is `scripts/nightly_inputs.py <step>` runs that step's independent sweeps under one New York session (an
  intraday run writes nothing and exits 0) and one call budget per provider and night (UW, FRED, yfinance; a second
  run of the same session starts from what the first spent, `.cache/nightly_inputs/<step>_calls.json`). A sweep that
  raises is `failed` and the others still run (exit 1). Its writes are first-write-wins appends (`record_first`).
  `check_inputs_steps` (on import) wants it after RC-Daily-Bars, a known step, its tables and all three caps stated.
- **Scheduled runs in the run log (#129 A1, core/automations.py:784-871; core/rawstore.py:3027-3033; core/ledger.py:249):**
  a task that writes tables upserts its own `job_runs` row (`trigger` scheduled, `registry_name`, `running` then
  `done`/`error`; a chain-blocked step ends `error` "chain blocked: ...") and passes the row's id to its command as
  `RC_RUN_ID` (`RC_RUN_SOURCE` = registry name). There `ledger.new_run_id`, `app/jobs.run_id()` and the change
  capture stamp that id and source, so the rows the run writes join its row. The backup records none. A row that
  cannot be written is a `job_runs: not recorded` log line, never a different exit code.
- **Everything else is manual:** each board's Fetch, the universe job and the identity job. The dashboard starts a
  job with `POST /run/<job>` and the header `X-RC-Panel` (CLAUDE.md, Data safety).
- **One universe writer at a time (#146):** the universe job and the cap backfill take the file lock `universe` in the
  store folder (`universe_write_lock`, core/universe_fetch.py:199; re-entrant in one thread; a second writer is told who
  holds it, makes no network call and writes nothing).
- **The forecast freeze (#128):** the Swing, Fundamentals, AG and Universe Fetch jobs end by freezing the picks their
  boards show into `forecast_ledger` (app/runners.py:859, :910, :931; `core/ledger.py:321`). It writes only when the
  Fetch runs outside 09:30-16:30 ET and every shown pick has a complete bar of the latest complete session; otherwise it
  writes nothing. A Refresh never freezes. Its outcome sentences are the job's `notes` (`/api/jobs`) and a
  `forecast ledger:` line in the Server log; they are not the job's `message`, which the dashboard shows as a popup
  (app/jobs.py:261-273, templates/dashboard.html:900).
- **The run log (#282):** every job writes one `job_runs` row, upserted when it starts running and when it ends
  (`_record`, `_set` in app/jobs.py). An end status is stored before the registry shows it. `run_id` is the job id;
  `run_id()` hands it to later writers (per thread). The write waits at most ~0.35 s for a locked store and a
  failure is a log warning, never a failed job; a row can stay `running` (server stopped, skipped write).
- **Planned:** a nightly order after the US close (22:00 Amsterdam): exchange file and daily bars first, then the
  screener sweep, identity and statements, then earnings and options (phase 2 timing draft).

## 6. Sources and links
| Source | What we take | Where the code calls it | Docs and terms |
|---|---|---|---|
| Unusual Whales | options, flow, GEX, screener, earnings calendar, ETF holdings, index lists, OHLC | core/providers/unusual_whales.py:25 (SPX Gamma reads through it too, #176) | https://api.unusualwhales.com/docs (local spec: docs/openapi.yaml) |
| Yahoo via yfinance | universe screener and quotes, daily bars, statements, `.info`, ETF top holdings | e.g. core/universe_fetch.py:610; module_comovement/ohlc.py:102 | https://github.com/ranaroussi/yfinance |
| SEC EDGAR | universe file, submissions, companyfacts, filing documents, 13F data sets | core/providers/sec_edgar.py:40-41; core/identity/sources/sec.py:42; module_sec_13f/engine.py:57 | https://www.sec.gov/search-filings/edgar-application-programming-interfaces · https://www.sec.gov/files/company_tickers_exchange.json · https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets |
| OpenFIGI | FIGI ids per ticker | core/identity/sources/openfigi.py:82 | https://www.openfigi.com/api/documentation |
| Nasdaq Trader | symbol directory (ETF flag) | core/identity/sources/nasdaq.py:21-22 | https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs |
| FDIC | bank check | core/identity/sources/fdic.py:85 | https://api.fdic.gov |
| SSGA | SPDR sector-fund holdings check | core/identity/sources/spdr.py:33 | the URL in the code |
| CFTC | Commitments of Traders, TFF | module_spx_gamma/cftc.py:15 | https://publicreporting.cftc.gov/resource/udgc-27he.json |
| FRED | Treasury yields, oil and other macro series | module_earnings_radar/macro_watch.py:19 | https://fred.stlouisfed.org/docs/api/terms_of_use.html · https://fred.stlouisfed.org/legal/ |
| FDA OOPD | orphan designations (provider only, no job) | core/providers/fda_orphan.py:27 | https://www.accessdata.fda.gov/scripts/opdlisting/oopd |
| ClinicalTrials.gov | trials (research script only) | scripts/research/186_biotech_sponsor_match.py:22 | https://clinicaltrials.gov/api/v2 |
| DeepSeek | Relatedness profiles and themes | core/providers/deepseek.py:40 | https://platform.deepseek.com |
| Grok (SuperGrok subscription, manual `--provider grok`, #388) | Relatedness profiles and theme names; no off-peak gate, the SuperGrok allowance gate stops a run | core/providers/grok.py:53 | https://grok.com/?_s=usage |
| Cboe (candidate, not called) | VIX family history CSV | none | https://www.cboe.com/tradable-products/vix/vix-historical-data/ · https://www.cboe.com/terms/ |
| IBKR (research only) | contract details | scripts/research/35_identity_r1c_ibkr.py:37 | https://interactivebrokers.github.io/tws-api/historical_bars.html |

