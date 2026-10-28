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
| Derived, recomputed | `forecast_outcomes` (#380 S0): one row per frozen pick and horizon (30 and 90 calendar days), rebuilt from `forecast_ledger` and the D01 bars by `core/outcomes.py`; a row without the first session's open has no reference and stays open; a 2.5x jump from the previous close (a split with no action) is `split_check`, not final; a closed row of the current model version is final, an open one is redone each night |
| Pruned | `spx_gamma_gex_history.json`: newest 10 days (module_spx_gamma/run.py:28, :58-84; each version stays in `spx_gamma_versions/` of the backup folder since #276) |
| Not stored | Earnings run results (only the HTML report), UW daily bars used during runs, the yfinance macro proxies (the 8 FRED series are stored in `macro_series` since E0 D27) |

## 5. Jobs and schedules
- **Scheduled** (Windows Task Scheduler, core/automations.py:117-188): RC-Store-Backup 04:00, RC-Relatedness-OffPeak
  12:30, RC-ValueChain-Sync 13:00, RC-Daily-Bars 22:35 (first, best effort on weekdays, the SEC exchange and fund
  files: `core/sec_files.py`, at most 2 SEC requests a night, never a different exit code, #301 F0; last, best
  effort, the `^GSPC` daily levels: `core/index_levels.py`, at most 1 yfinance request a night, none when current
  or before the attended `--index-fill`, never a different exit code, #293 M-SPX-0),
  RC-Screeners-Daily weekdays 23:00 after RC-Daily-Bars
  (the universe CLI `app/universe_cli.py`, #129 S2; not installed by default), RC-Forecast-Outcomes 23:45 after
  RC-Daily-Bars (`scripts/forecast_outcomes.py --nightly`, #380 S0; not installed by default), RC-Index-Lists Saturday 10:00,
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
  (#129 A1), RC-Forecast-Outcomes after RC-Daily-Bars (#380 S0; it cannot follow RC-Screeners-Daily, which runs on
  weekdays only) and S3 RC-Earnings-Inputs after RC-Daily-Bars (E0 #372); S4 adds its own with its job.
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

## 7. Planned: the data study
Roadmap: phase 1 universe, identity and sectors (done, PR #268); phase 2 who needs what and from which source, then
one common data interface; phase 3 computed results into one common results store; phase 4 derived areas such as
themes; then common filters for all boards.

Phase 2 decisions so far:
- **D1 thresholds:** ≥95% coverage on a seeded 150-ticker sample; all needs on one source ≤50% of its daily limit
  (UW 60,000 of 120,000); a lower-ranked source wins only by ≥5 points; an unattended job never depends on IBKR alone.
- **D2 source order:** UW, then IBKR, then yfinance as fallback only. Specialists lead their field (SEC, FRED, Cboe,
  CFTC, FDA, ClinicalTrials.gov, OpenFIGI). Options from UW. No new paid plans.
- **D3:** dividend-adjusted prices are needed now; level rules keep split-adjusted nominal prices.
- **D4:** one raw table per dataset, with `source` and `fetched_at` on every row; computed results are phase 3.
- **D5:** unscoped planned boards (#2, #4, #5) get one estimate row each.
- **D6:** boards read `equity_universe` and `security_master` through one read view; the tables stay separate.
- **D7:** three theme columns on `security_master`, written by Relatedness (phase 4).
- **D8:** the SEC fund file `company_tickers_mf.json` gets its own raw table.
- **Sector order (#272):** manual, S&P 1500 list, UW, Yahoo, SIC fallback, type rule, Unclassified.

Step 2.4 (avo, 2026-10-04 10:06Z): all 28 PC measurements approved as pre-registered (project files
`data_sources/study/phase2/04_measurements/00_preregistration.md`; #271 comment).
- **G2-03 UW bar coverage: failed.** 140 of the 150 S150 tickers (93.3%) have at least 500 UW sessions, below
  the pre-registered 95%; the rule is not changed. UW therefore does not supply D01's history (#271 rule 4), which
  matches 2.5-5 (one-time yfinance fill). avo confirmed at 10:24Z: continue with yfinance for the history.

Step 2.5 area decisions (one card each, 13 in all):
- **2.5-1 FRED, 2.5-2 Cboe: decided, recorded in the private repo.** Both series sets get their own raw table
  under D4. The decision text and its reasoning stay in the private record and are not carried here.
- **2.5-3 Crypto for #7 (avo, 10:10Z): deferred with #7.** OKX and Deribit (BTC/ETH open interest, funding, DVOL)
  are decided when #7's build is approved; no source work now.
- **2.5-4 Short interest (avo, 10:11Z): stay on UW.** UW `/short_screener` and interest-float are the primary
  source (`core/providers/unusual_whales.py:835-840`, `:850-860`); yfinance is the fallback; no FINRA credentials.
- **2.5-5 Bar history length (avo, 10:13Z): maximum.** D01 is filled once with each ticker's full available
  history from yfinance (the D2 fallback, because UW looks back 2 years), then kept daily from UW and never pruned.
  The fill's time and size are measured and shown to avo before it runs (#156 decision of 2026-10-03).
- **2.5-6 Weekly candles (avo, 10:17Z): D02 dropped.** Rotation moves to daily closes from D01 with #182 and #183;
  any weekly view is derived from D01 with no calls.
- **2.5-7 SEC refetch (avo, 10:18Z): on a new filing, with a 90-day backstop.** A ticker's companyfacts are
  refetched when the submissions cache shows a newer 10-K, 10-Q or 20-F, or its CIK changes (B13), and at least
  every 90 days; rows with no `fetched_at` are refetched in a staggered first run (#161).
- **2.5-8 Estimate snapshots (avo, 10:19Z): earnings-calendar tickers.** D14 takes a daily yfinance snapshot
  (EPS trend, revisions, revenue estimate) for the tickers on the earnings calendar only, in its own raw table (D4);
  whether yfinance fills the fields is measured in G2-22.
- **2.5-9 SmallCap 600 (avo, 10:19Z): yes.** The S&P 1500 list that #272 fetches also writes the SmallCap 600
  members to `index_constituents` under a new slug, so the index filter gains an S&P 600 option (#214). The first
  fill is a store write and needs avo's OK for that run.
- **2.5-10 CUSIP source (avo, 10:36Z): OpenFIGI.** Every CUSIP in the 13F filings is mapped to a ticker through
  OpenFIGI (#207's plan; with the key about 380 requests for 38k CUSIPs, project files `phase2/20_doc_pages.md:21-24`),
  and `security_master` takes its CUSIP from that map. This replaces today's name match, which drops class letters
  (`module_sec_13f/engine.py:346`, `:523-526`). N-PORT is not used for it: XLK's list had a CUSIP on 73 of 79
  holdings (92.4%, G2-24) and covers only ETF holdings. OpenFIGI's match rate is measured on #207's first run.
- **2.5-11 ETF holdings (avo, 10:38Z): UW stays first.** The Rotation board keeps UW `/etfs/{t}/holdings`
  (`core/providers/unusual_whales.py:897-903`, `:1049-1061`) with yfinance as the fallback; SEC N-PORT is not used for it, because
  XLK's newest list was as of 2026-06-30 and filed 2026-08-28, 96 days old on the decision date (G2-24), and GLD, SLV
  and USO have no SEC fund record. How current and complete UW's list is was not measured.
- **2.5-12 adj_close (avo, 10:39Z): yfinance.** D01's price stays UW's nominal bar (split-adjusted, not
  dividend-adjusted, G2-01). `adj_close` history comes from the one-time yfinance fill (2.5-5,
  `history(auto_adjust=False)` keeps `Adj Close` plus dividend and split columns: yfinance 1.5.2 on the PC, G2-21,
  `yfinance/scrapers/history.py:500-501`, and 1.7.0 in the cloud),
  and new dividends and splits come from yfinance daily, in its fallback role (UW `/dividends` is 403; IBKR cannot
  serve an unattended job alone). The factor implied from stored history was clean for only 108 of 149 tickers
  (72.5%, G2-01). The daily yfinance load is designed in step 2.6; it was not measured.
- **2.5-13 Earnings-run budget (avo, 10:40Z, typed "sayacı önemseme direk dal"): no limit.** An earnings run gets
  neither a weekday cap nor a guard on UW's daily counter, and #198 is not pulled forward for the budget. G2-14 showed
  the calendar is not cut at 100 rows (157 and 161 on two heavy sessions); at that density a 5-weekday run is about
  19,000-25,500 calls by estimate (two half-sessions from different days added), against the 60,000 D1 threshold.

The target raw table of each dataset is the "Planned" note per row in [data_flow.md](data_flow.md).

Step 2.6 interface decisions (project files `data_sources/study/phase2/06_interface.md`):
- **2.6-1 File layout (avo, 2026-10-04 11:20Z): `history.duckdb` holds the result history only.** The result-history
  tables of #278 (history cards 4-12) go to a new `.store/history.duckdb`, append-only, with a longer backup retention
  than `raw.duckdb` (review B12 A). Raw datasets, including their event and change-history rows, stay in `raw.duckdb`
  as planned on 2026-10-03 (15:31Z), so boards keep reading them from one file. This narrows #287's checklist line
  "holds the result and raw history tables".
- **2.6-2 Market cap (avo, 11:30Z): Yahoo first.** D04 `quote_snapshot` keeps both vendors' rows each session
  (key ticker, session date, source). Boards and the $300M cut use Yahoo's cap, as today (`yf.screen` and
  `fast_info`, `core/universe_fetch.py:610`, `:327-329`); UW's cap is stored beside it, and gaps over 5%
  are listed on Data Health. G2-07: the UW screener returned 74 of 150 names (fails D1's 95%), Yahoo `.info` 150 of
  150; caps within 5% on 64 of 74 pairs; SKHY's Yahoo cap is about 40 times UW's (financial currency KRW).
  Built in #294 (branch area/spine/294-quote-snapshot): the universe pull writes Yahoo's rows (Phase A quotes,
  Phase B `fast_info` fills), the universe job then writes UW's rows (screener, batches of 100) when a UW key
  verifies; `core/quotes.py`. Boards still read `equity_universe` until the D6 current view (#279).
- **2.6-3 Next earnings date (avo, 11:31Z): UW first.** The AG, Swing and Fundamentals cards and their week filter
  read the next date from the UW earnings calendar (4 weeks ahead, about 40 calls a night by estimate), then the UW
  screener's `next_earnings_date`, then Yahoo where UW has no date. Today they read Yahoo's `earningsTimestampStart`
  (`module_asymmetric_growth/engine.py:246`). G2-15: UW has a date for 74 of 150, Yahoo for 112; neither passes D1;
  the same day on only 18 of 73, and Yahoo's lead was measured on `earningsTimestamp`, not the field the cards read.
- **2.6-4 Statement source (avo, 11:33Z): measure first.** Before D1 picks a leading source per statement field
  (Financials, Earnings Radar, AG's fallback), the PC measures yfinance statements on U150 (G2-29, yfinance reads
  only, no UW call; pre-registered in `04_measurements/00_preregistration.md`). Until then each board keeps its
  source: Earnings Radar UW, Financials yfinance (`module_financials/run.py:59`), AG and Fundamentals SEC first. G2-10
  and G2-25 measured only UW and SEC (UW leads revenue 56.7% vs 47%; SEC leads net income 65% vs 57.3% and total assets
  70% vs 58%), and on an earlier 98-name sample yfinance led UW by at least 5 points on four fields.
  **G2-29 result (11:58Z): yfinance leads every measured field**, so D1 puts it first for Financials, Earnings Radar
  and AG's fallback. Fill % of U150, SEC / UW / yfinance: revenue 47.3 / 56.7 / 83.3; gross profit 28.0 / 51.3 / 60.0;
  EBIT 54.0 / 57.3 / 73.3; EBITDA n/a / 57.3 / 72.7; net income 64.7 / 57.3 / 83.3; interest expense n/a / 48.0 / 68.0
  (genuine "Interest Expense" label only, as after P21 #262; 75.3 with the removed "Net Interest Income" label); total
  assets 70.0 / 58.0 / 89.3; operating cash flow 64.7 / 57.3 / 81.3. Second source per field by the same rule: UW for
  revenue, gross profit, EBITDA and interest expense; SEC for EBIT, net income, total assets and operating cash flow.
  Fill only, values were not compared. AG and Fundamentals keep SEC (D11) first; the card did not cover them.

Step 2.7 issue impact (project files `data_sources/study/phase2/07_issue_impact.md`):
- **Posting approved (avo, 2026-10-04 12:14Z, "Hepsini gönder").** 104 open issues read against the decisions above:
  28 change scope and go back to Reviewed on board #1 (only where Review is "Approved to build"), 43 get a note, 33
  need no change. 71 comments, and 16 new build issues (N1 the one G1 schema bump 9 → 10; N2-N16 the writers no open
  issue builds: D03, D04, D09, D10, D12, D13, D15/D16, D20, D21 with D8, D24, D25, D26, D27-D29, D36, D37).
- **Defaults that follow from earlier decisions:** #142's 142a (industry group and sub-industry fill) is dropped by
  the two-level rule (#265); biotech membership (#3, #188, #190) is `security_master.industry` = "Biotechnology",
  counted read-only on the PC before B2; a board table (#90, #96, #179, #183, #33 VC2) joins the G1 bump if its design
  is final when G1 is built, otherwise it gets its own bump; #182's recompute appends under a new `model_version`;
  `forecast_ledger` stays in `raw.duckdb`, so PR #249 needs no change.
- **Posted (2026-10-04 12:2x-12:4xZ):** 71 comments. Board #1 Review set to Reviewed on the 22 issues that were
  "Approved to build": #90, #118, #129, #156, #179, #181, #182, #188, #189, #190, #192, #199, #207, #208, #209, #214,
  #278, #279, #281, #282, #283, #287 (#3, #142, #143, #161, #162 and #193 kept their value). New issues, Review "Not
  reviewed": N1 #292 (G1 bump), N2 #293 (D03), N3 #294 (D04), N4 #295 (D09), N5 #296 (D10), N6 #297 (D12), N7 #298
  (D13), N8 #299 (D15, D16), N9 #300 (D20), N10 #301 (D21, D8), N11 #302 (D24), N12 #303 (D25), N13 #304 (D26), N14
  #305 (D27-D29), N15 #306 (D36), N16 #307 (D37). Parents: #274, except #293 under #24, #298 under #28, #299 under
  #125 and #305 under #57.

**Open, decided next:** which intermediate (computed) data is kept with history for later backtesting. Raw option
chains and price candles can be fetched again and are large; computed results mostly cannot be rebuilt for a past
day because their inputs keep only the latest value (§4). The architecture review of the data backbone
(2026-10-04, `plans_claude/arch/2026-10-04_data_backbone_review.md`, which lives in the private repo and is
not carried here) lists the findings and the cards
avo answers one at a time; the item list is `data_sources/study/phase3/01_intermediate_inventory.md` in the project
files (61 items). Each answer is recorded here.

Answers so far:
- **Card 1 (avo, 2026-10-04 02:52Z): both.** Board results are kept as session snapshots (review ADR-2), and raw data
  that changes is kept with its history (ADR-1 history modes), so a new rule can be tested on the past.
- **Card 2 (avo, 08:32Z): the app makes the off-machine copy** (review B2 A). After each verified nightly backup,
  the newest snapshot set is copied to a folder avo configures (a Google Drive for desktop folder or an external
  disk), verified there again, and Settings / Data Health shows the age of the last copy.
- **Card 3 (avo, 08:33Z): back up the SPX Gamma folder now** (review B3 A). `module_spx_gamma/data` joins the
  nightly backup as an incremental, verified mirror, and docs/data_architecture.md
  stops calling it external. It moves into DuckDB later with #179.
- **Card 4 (avo, 08:34Z): keep every screener result per session.** Every scored ticker's scores and gates of
  Asymmetric Growth (`ticker_scores`), Swing (`swing_scores`, `swing_gates`) and Fundamentals
  (`fundamentals_scores`, `fundamentals_gates`), each row with session date, run id and threshold version. About
  22k rows per session from the code's caps (estimate, not measured).
- **Card 5 (avo, 08:36Z): keep every Earnings Radar run.** For every calendar ticker: the trade card (rule scores
  R2-R8, composite, action), the NRC score and tier, and the filter outcome, with session date and run time (the
  radar also runs during the day). The raw UW answers behind it are phase 2 datasets (D13 and others).
- **Card 6 (avo, 08:36Z): keep LEAPS footprints and OIPD distributions with history.** LEAPS footprints drop the
  60-per-ticker cap and keep one per watchlist ticker per session; every OIPD fitted distribution is kept per run,
  session-dated.
- **Card 7 (avo, 08:54Z): option data is kept only with the output it produced.** Full option chains are not
  downloaded or stored in bulk (too costly). When a board uses option data to make an output, the option inputs that
  output used are stored with it, under the same run id and session date. First board: Earnings Radar, whose option
  inputs are spot exposures by strike, the IV term structure, recent flow and its aggregate, GEX levels, options
  volume, and the IV rank and expected move it reads (`module_earnings_radar/pipeline.py:164`, `:190`, `:354`,
  `:372`, `:435`, `:676`, `:451`, `:402`). The same rule holds for every board that builds an output from option
  data. Daily bars were not addressed, so they stay as the phase 2 plan has them (D01 `price_bars_daily`, never
  pruned).
- **Card 8 (avo, 08:58Z): universe membership, sector and identity get a change history, with a weekly backup
  until then** (review B4 A, with B as the stopgap). Membership, sector, CIK and type are kept as rows with
  `valid_from`/`valid_to` (ADR-1 change history), designed in step 2.6; boards keep reading a current view. Until
  that lands, one raw backup per week is kept and never pruned (today the backup keeps the last 5,
  `scripts/backup_store.py:228`; raw is about 429 MB after compaction, docs/data_architecture.md:942).
- **Card 9 (avo, 09:01Z): keep the Sector Rotation panels and the universe funnel per session.** Rotation holdings
  RS (`module_sector_rotation/store.py:88-93`, upserted per ETF today), the money-flow leaderboard (one JSON file,
  `app/runners.py:392`), the sector→industry tree (computed at render, `module_sector_rotation/engine.py:222-230`)
  and the universe funnel counts (computed at render, `core/universe.py:83-131`), each with run id and session date.
- **Card 10 (avo, 09:12Z): keep every Relatedness theme run, peer-score run and payload.** The cutover stops
  deleting older theme runs (built in CL-A REL-1: the prune is gone from `cutover_materialize`), so theme runs, themes, tags, members, theme
  ETFs and the ticker→theme map stay per run. Peer scores (`relatedness_scores`, today `replace_all` on every Fetch,
  `module_relatedness/store.py:155-166`) and the Relatedness payload (one JSON file,
  `module_relatedness/engine.py:471`) are kept per run too, with run id and session date.
- **Card 11 (avo, 09:18Z): keep the 13F board metrics and the CUSIP map per quarter.** For each quarter: the board
  metrics (accumulation streak, acceleration, % of shares out, price delta, short %, insider %; today only rendered,
  `module_sec_13f/report.py:228-283`), the shares out, short % and insider % used for them (today latest only,
  `module_sec_13f/enrich.py:32-36`), and the CUSIP→ticker map as it was (today a re-map overwrites,
  `module_sec_13f/engine.py:296-299`), with quarter and run id.
- **Card 12 (avo, 09:25Z): keep Financials reports and the Value Chain data per run.** The numbers of every
  Financials report (DCF value and assumptions, 3-statement projections, comps, credit, sensitivity, Quick Remarks;
  today the dashboard overwrites one `financials_latest.html` (`app/runners.py` passes `report_path`), and only the CLI writes a per-ticker `<TICKER>_report.html`, `module_financials/run.py:124-130`) and the Value Chain edges and payload
  (today `replace_all` on every Fetch, `module_value_chain/engine.py:260-278`, and one JSON file) are kept per run,
  with run id and date.
- **Card 13 (avo, 09:26Z): every finding and the card work get an issue.** Epic #274 holds them: #275 off-machine
  copy (B2, card 2), #276 SPX mirror (B3, card 3), #277 weekly raw backup (card 8), #278 result history (B1, cards
  4-12), #279 raw change history (B4, card 8), #280 provider windows (B5, card 7), #281 history mode per table in
  step 2.6 (B6), #282 run id and model version (B7), #283 session date (B8), #284 empty `replace_all` guard (B9),
  #285 schema drift (B10), #286 append-only guard (B11), #287 `history.duckdb` (B12), #288 docs (B14), #289
  writerless tables (B15). B13 is folded into #161. None is approved to build yet.

## 8. Planned: Settings / Data Health
The data mechanism will also live in the dashboard under Settings / Data Health: this record, the state of each
dataset, a way to update the wanted part, and the board data computed from the raw data, shown in detail and
checkable. A review and an implementation plan for it are written when phase 3 is done.
