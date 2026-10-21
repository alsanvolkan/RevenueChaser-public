# Data health — universe refetch + settings board

Source of truth for **store coverage, refetch, and 10-K freshness**.
Companions: [`data_architecture.md`](data_architecture.md) (where data lives),
[`data_retrieval.md`](data_retrieval.md) (Fetch vs Refresh),
[`dashboard_ui.md`](dashboard_ui.md) (equity filter chrome — **not** this tab).

**Status: plan only.** Do not implement until an explicit go. Do not write the
live `.store/`. Do not vendor third-party crawlers.

### Locked answers (this exchange)

- **The 10-K check already exists.** `module_value_chain/sync.py` compares the
  stored `chain_filings.accession` against SEC `submissions.recent`. The gap is
  **trigger + visibility**, not a new crawler.
- **It gets a GUI**, in the **existing dashboard** as a new parent tab
  **Settings / Data health** — not a separate app, not inside Value Chain.
  Refresh = store-only inventory; **Fetch** on the corpus row = the sync job.
- **Header “Refresh Universe” does not sync 10-Ks.** It is Yahoo screener
  membership + (planned) cap stats only.
- **Value Chain / Relatedness Fetch stay cache-first** — they fill tickers with
  no row and do **not** recrawl names that already have a filing. New-filing
  detection is the Settings action.

### Feasibility review (2026-09-24) — accepted decisions

Code check against this plan found four gaps; the fixes below are **binding**
and override older wording further down.

1. **No partial-row upsert.** `equity_universe` is `write: "upsert"` and
   `_apply_write` overwrites **every** non-key column from `r.get(c)`. Writing
   `{ticker, market_cap, price}` would NULL name/sector/GICS/quote. Phase B
   uses a dedicated `UPDATE equity_universe SET market_cap=?, price=? WHERE
   ticker=?` helper (in `rawstore`, chunked, one transaction per chunk).
2. **Phase A carries the previous cap forward** (accepted). When a screener
   quote has no `marketCap`, `_save_universe` keeps the stored
   `market_cap` for that ticker instead of writing NULL. Otherwise every
   Refresh Universe re-wipes MU and Phase B re-does ~1.7k lookups.
3. **No every-ticker `fast_info` pass in v1** (accepted). Phase B only fills
   rows whose `market_cap IS NULL` after Phase A. `fast_info.market_cap` in
   yfinance 1.5.2 costs a shares-history + 1y-price request per ticker, and
   silently falls back to `Ticker.info` when shares are missing — the backfill
   must read `fast_info.shares` / `last_price` itself and skip (leave NULL)
   when shares is None, so `.info` is never triggered.
4. **Provenance: second key, not an overwrite.** `equity_universe_provenance`
   is `write: "replace"` keyed on `key`; membership stays `__meta__`, the
   stats pass writes `__stats__`. No schema bump. The table being empty today
   means the live universe arrived via migration, not a fetch — the next real
   Phase A run populates `__meta__` as the code already does.
5. **Inventory column names are pinned to `MANIFEST` (verified 2026-09-25).** The settings
   inventory must read real columns, because a wrong name degrades silently to `—` and seeded
   tests still pass. Authoritative list: `equity_universe` (`ticker, market_cap, sector, price,
   name, …` — **no date column**), `equity_universe_provenance` (`key, generated_at,
   refreshed_by`), `sec_company_sic` (`…, fetched_at`), `chain_filings` (`…, filed, …`) /
   `edgar_documents` (`…, filed, …`), `edgar_financials` (`ticker, annual, quarterly, meta` —
   **no date column; do not parse the blobs**), `swing_ohlc` (`…, ts, …` — the column is `ts`,
   **not** `date`), `index_constituents` (`slug, ticker`), `sec13f_totals` (`ticker, entry`) +
   `sec13f_holdings` (`accession, cusip, cik, shares, value`) with the latest period from
   `sec13f_submissions.period`, `oipd_meta.as_of` (**not** `oipd_chain`, which is per-contract),
   `relatedness_profiles.fetched_at`. Read via `rawstore.query(sql, params)` /
   `rawstore.get_all(name)`.

Other findings: `sync_filings` already holds `job_lock("edgar_sync")` + the
SEC throttle; its `{"locked": True}` / 0-updated returns must become
`jobs.JobMessage` in the runner. A full sync is ~3.6k SEC GETs (~6+ min) on
the single worker. No iframe→parent `postMessage` bridge exists — settings-row
Fetch buttons must call `/run/*` + poll `/api/job/<id>` from the iframe.
`_STORE_REFRESHABLE` derives from `_MODEL_ONLY_RENDERERS`, so the board needs
a renderer entry there too.

---

## Why this exists

Two different holes look the same in the UI (“ticker missing / filing old”):

1. **Stats never landed.** MU is on `equity_universe` (Micron Technology, Inc.,
   IT, last screener price $925.57) but `market_cap` is NULL because Yahoo’s
   **screener quote had no `marketCap` key**. Boards that gate on
   `classification_universe()` / `$300M` then drop it. Live `yfinance`
   `Ticker.fast_info.market_cap` for MU is ~$1.22T — Yahoo knows the cap; the
   writer never asked that endpoint.
2. **Fetch is cache-first, not “is there something newer?”.** Value Chain /
   Relatedness `fetch_corpus` skips any ticker that already has a
   `chain_filings` row. A later 10-K never arrives unless
   `python -m RevenueChaser.module_value_chain --sync-filings` runs. That CLI
   exists (`module_value_chain/sync.py`); **no dashboard job calls it**.
   `docs/classification_design.md` P4c: “Sync trigger mechanism still deferred.”

Chrome “Last run” / `core/provenance.py` only shows the **newest date in the
table**. It does not show holes (null cap, missing filing, stale accession).

Live store snapshot (2026-09-24, read-only):

| Dataset | Count | Hole |
|---|---|---|
| `equity_universe` | 7,403 | **1,710** null `market_cap`; 3,634 ≥ $300M |
| `equity_universe_provenance` | **empty** | last membership write left no `generated_at` |
| `chain_filings` | 3,278 (2,837 10-K + 441 20-F) | newest filed 2026-09-18 |
| MU 10-K | accession `0000723125-25-000028`, filed 2025-10-03, quality ok | **matches** SEC `submissions.recent` (no newer 10-K yet; FY ends late Aug) |
| `ticker_payload_cache` | 7,461 | not used as the universe cap source |
| `swing_info` | 500 | MU absent |
| `swing_ohlc` | 4,044 tickers | — |
| Asymmetric payload cache day | 2026-09-11 | universe stats older than the MU price spike |

Refreshing Universe **as it is written today** can still leave MU uncapped:
`fetch_universe_stats` copies only `q.get("marketCap")` from `yf.screen`.

---

## 1. Equity-universe refetch (all tickers)

Writer: `core.universe_fetch.fetch_universe_stats` (moved out of `module_asymmetric_growth.engine` in #146;
the engine re-exports the name).
Header **Refresh Universe** stays `POST /run/universe`
(`runners.run_universe_refresh` → `refresh=True` then the three scanners).

Split **membership** from **stats**. Membership comes from the SEC exchange file (#264); the stats are
Yahoo's.

### Phase A — membership (the SEC list, Yahoo market data joined on)

- **The member list** is a fresh `sec.exchange_map()` pull (`company_tickers_exchange.json`, one request per
  pull to www.sec.gov through the shared SEC client; the User-Agent comes from `RC_SEC_USER_AGENT`). It never
  reads the identity cache. The rows whose exchange is `Nasdaq`, `NYSE` or `CBOE` are the members; OTC rows and
  rows with no exchange are out (2026-10-03: 7,719 of 10,434 rows).
- **Market data** is the Yahoo screener as before: `yf.screen` US `EQUITY` / USD / `US_EXCHANGES`,
  `MC_CHUNKS`, page 250, dedup by ticker. Its quotes join the list by ticker (both spellings meet on
  `norm_ticker`; Yahoo says `BRK-B`). A screener row that is not on the list is dropped.
- **A listed name with no quote** is saved with NULL `market_cap`, `price` and `quote` and the SEC name.
  The stored cap and price still carry forward, and Phase B fills the NULL caps.
- **Insert order:** the screener rows first, in pull order, then the SEC-only rows. Swing and Fundamentals
  shortlist the first 500 rows in store order (no `ORDER BY`), so priced names keep that shortlist.
- **Guards:** a failed SEC map or one listing fewer than `MIN_FULL_UNIVERSE` (3,000) Nasdaq / NYSE / CBOE rows
  (`stats["sec_error"]`) keeps the stored universe and writes nothing. Tested.
- **Full vs partial (D6, #146).** The Yahoo side is *full* only when every `MC_CHUNKS` slice ran to the total the
  screener reported **and** it returned a quote for at least `FULL_PULL_MIN_SHARE` (90%) of the stored names that
  carry a quote (the stored `SEC only` rows never had one, so they are not in the denominator); then `replace_all`
  as before. Anything else (a dead slice, an empty page, `max_pages` exhausted, a missing total, or a short pull) is
  *partial*: it **upserts** the quotes it got and the listed names not stored yet (cap / price / sector carried
  forward), re-applies the identity master, writes the `__fetch__` provenance row (`refreshed_by` = `partial`; a
  full pull writes `full`) and skips Phase B. A stored name the screener missed keeps its row, quote included. It
  never deletes. `stats` carries `partial`, `chunks_complete` / `chunks_total`, `stored_before`, `stored_quoted`,
  `fetched`, `not_seen`. The boards print "partial fetch" in the universe line. This replaced the absolute
  `MIN_FULL_UNIVERSE` test on the screener count, which let a 3,000-name pull replace a 7,366-name table.
- **One writer at a time.** `core.universe_fetch.universe_write_lock()` (a re-entrant `job_lock("universe")`) covers
  the SEC pull, fetch, write, identity apply, provenance and Phase B. A second run returns the stored universe with
  `stats["locked"]` = the holder's pid, calls no network and writes nothing. The universe refresh job holds the same
  lock for its whole run (#244), plus a `universe_run` marker, so the single-board Fetches are refused while it runs
  (not while Phase B runs). A single-board Fetch holds a `board_fetch_<board>` marker, so a universe run is refused
  while one runs (#356). A lock left by a crashed process is broken even when its PID was reused (#355).
- Sector/industry still omitted by the screener; sector and industry come from the identity master
  (`core/identity/apply.py`, R3 #86, #265, #272: the 11 Morningstar sector names, from the S&P 1500 list for its own
  names, then UW, Yahoo, the explicit SIC table and the type rule; industry is UW's text), never from a SIC backfill
  into the universe.

### Phase B — stats backfill (the MU fix)

After a successful membership write (or as a standalone job when membership
is already full):

- Universe of work = **every** `equity_universe.ticker` (not only new names).
- For each ticker with `market_cap IS NULL` **or** when the job is
  `refresh=True` for stats: fill `market_cap` and `price` from
  `yf.Ticker(t).fast_info` (`market_cap`, `last_price`).
- **Do not** call `Ticker.info` for ~7k names (heavy, rate-limited).
- Batch / throttle; persist in chunks (DuckDB writer lock).
- Null after backfill stays null (warrant, OTC leftover, Yahoo gap) — do not
  invent a cap.
- Merge into existing rows (`write_rows` / UPDATE), **not** a second
  `replace_all` that could drop names Phase A just kept.

**Pacing + resumability (live 2026-09-24 run).** At 4 workers / 0.05s the
backfill tripped `YFRateLimitError` after ~100 tickers and Yahoo then refused
every call (`{requested: 1710, filled: 107, skipped: 617, failed: 986}`). Phase
B now pauses `_CAP_BACKFILL_SLEEP` (0.3s) before each ticker, runs
`_CAP_BACKFILL_WORKERS` (2), retries `YFRateLimitError` with
`_CAP_BACKFILL_BACKOFF` (5s, 20s) up to `_CAP_BACKFILL_ATTEMPTS` (3), and after
`_RATE_LIMIT_STREAK_STOP` (5) consecutive exhausted tickers **stops the run
early** — the rest stay NULL and are deferred (`rate_limited` is then True in
the result). Because the work list is null-only, "stop early" costs nothing:
re-running later resumes exactly where it left off. A full ~1.7k pass is
~10-20 minutes; a hard limit ends it politely instead of marking the tail
`failed`.

Optional later (not v1): `cap_source TEXT` (`screener` | `fast_info`) as a
typed column. That is a schema bump → manual `.store/` checkpoint. v1 can
land without it.

### Provenance

Write `equity_universe_provenance` on every successful membership **and**
stats pass (`generated_at`, `refreshed_by`). The table is empty today; the
settings board should show that as a hole, not “unknown”.

### What Refresh Universe will mean after this

1. Phase A: the SEC member list with the Yahoo screener's market data joined on, carrying forward a
   stored cap when the quote omits `marketCap`.
2. Phase B `fast_info` for rows still NULL. (Every-ticker re-fetch: not v1.)
3. Existing scanner chain (Asymmetric / Swing / Fundamentals) unchanged.

A store-only **Refresh Tab** on Implied Probability still does **zero**
Yahoo; it only re-reads `classification_universe()`. MU appears there once
Phase B has written a cap ≥ $300M.

### Tests (TDD, after go)

- Screener quote **without** `marketCap` + stub `fast_info.market_cap` →
  column filled; MU-class ticker survives `classification_universe()`.
- A failed SEC map or one listing fewer than `MIN_FULL_UNIVERSE` rows does not shrink cache.
- Partial screener (a dead chunk, or under 90% of the stored quoted names) upserts, keeps the rest and flags `__fetch__`.
- The SEC list decides membership (OTC and no-exchange rows out, a screener-only row dropped, an SEC-only row
  saved with a NULL cap, price and quote); the insert order is screener rows then SEC-only rows. The SEC map is
  faked in tests (no network).
- Stats backfill does not delete tickers absent from this screener page.
- `no_fetch=True` / Refresh path never calls `yf.screen` or `Ticker`.
- Fake at the Python boundary (`yf.screen`, `yf.Ticker`); yfinance has no
  redirectable base URL ([`data_retrieval.md`](data_retrieval.md)).

---

## 2. Settings / Data health board

A **new parent tab**, not an equity-universe filter board.

| | |
|---|---|
| Filter panel / Theme cascade | **no** (the drill below has one ticker combobox: the filter kit's `RCCombobox`, #383 C1b) |
| `help_html` | **yes** — what Fetch vs Refresh does on **this** tab |
| Refresh | store-only inventory (counts, nulls, last dates, holes) — **zero network** |
| Fetch | **per dataset**, one existing job (or the new stats-backfill / `--sync-filings` job) |

Not a Linear clone. Same tokens (`--bg #05111F`, `--accent #0EA5E9`). Compact
table, one row per dataset.

### Rows (v1)

Each row: dataset name, store table(s), last data date (`provenance` /
`generated_at` / `max(filed)` / `max(ts)`), row count, **hole count**,
Fetch action, notes.

| Dataset | Store | Fetch (network) | Hole to show |
|---|---|---|---|
| Equity universe | `equity_universe` + provenance | `/run/universe` (Phase A+B) | null `market_cap`; empty provenance |
| Identity spine | `security_master`, `identity_conflicts`, `identity_source_cache`, `identity_runs` | `/run/identity` (**Fetch identity**) | `security_master` rows with no sector source (`sector_source` none) at market cap >= $300M |
| 10-K / 20-F corpus | `chain_filings`, `edgar_documents`, `edgar_docs/` | **`sync_filings`** (new job) | missing row vs classification universe; stored accession ≠ SEC latest |
| Companyfacts | `edgar_financials` / `edgar_facts` | existing EDGAR facts path | tickers with no facts |
| Daily OHLC | `swing_ohlc` | Co-movement / Swing Fetch | names in universe with no bars |
| Index constituents | `index_constituents` | existing UW path | — |
| 13F | `sec13f_*`, `cusip_ticker`, `identity_cusip` | `/run/module_sec_13f` (Fetch) · `/run/module_sec_13f-rebuild` (store only) · `/run/module_sec_13f-map` (OpenFIGI CUSIP map, #207) | latest period vs expected quarter |
| OIPD chain | `oipd_chain` / `oipd_meta` | OIPD Fetch | — |
| Relatedness profiles | derived from corpus | Relatedness Fetch (LLM; out of v1 actions) | profiles vs `chain_filings` |

v1 actions on the board: **Universe (A+B)**, **10-K sync**, **Fetch identity**.
Do not add a “fetch everything” button (locks, quotas, hours).

### Per-ticker drill (v1, one combobox on this tab only)

Type MU → show: name, cap (or **null**), price, GICS, last 10-K
form/filed/accession/quality vs SEC `submissions.recent` **only after** a
Fetch of that ticker’s submissions (or from a cached submissions row if we
add one). Refresh of the drill must not hit SEC; show stored corpus only.

### Fetch vs Refresh (this tab)

Same contract as every store-backed board:

- **Refresh** = recompute the inventory HTML from `.store/` with zero
  network. Register in `_STORE_REFRESHABLE` and `STORE_BACKED`.
- **Fetch** = enqueue the dataset job; poll `/api/job/<id>`. Empty/noop
  sync still writes the artifact and raises `jobs.JobMessage` (“0 updated,
  3278 current”).

### Wiring `--sync-filings`

Today: CLI only. Value Chain dashboard Fetch calls `fetch_corpus` (skip if
row exists) — **not** `sync_filings`.

Plan:

- `POST /run/edgar-sync` (name TBD) → `sync.sync_filings()`.
- `requires_uw=False` (SEC only).
- Shares `job_lock.acquire("edgar_sync")` + `sec_edgar` 10 req/s throttle.
- Idempotent: same accession → `current`, no download (already tested in
  `module_value_chain/tests/test_sync.py`).
- After updates, `build_edges()` as today; Relatedness Refresh then reads
  new Item 1 without a new LLM run until the user Fetches Relatedness.

Do **not** change Value Chain Fetch to always recrawl. Cache-first stays
for first-time corpus fill; **new-filing** is the sync job.

**Re-slice apply (#185).** `python -m RevenueChaser.module_value_chain --reslice-apply` re-slices every
stored `chain_filings` row from its stored document (no network), writes it through the slice keep guard,
rebuilds `chain_mentions` and records a result history run. It holds the `edgar_sync` and `relatedness` locks
(the 13:00 sync and the 12:30 Relatedness run); a dashboard Value Chain Fetch holds no lock, so check
`/api/jobs` and `job_lock.holders()` before a live run. Look first with `--reslice-dry-run`. Rollback: stop
the dashboard, restore edgar and history from the backups taken just before, before the next 13:00 sync.

### Board inputs and the funnel

The Board inputs card (Co-movement today) shows the funnel the board itself emitted
(`core/funnel.py`), so the card and the board cannot disagree. Step kinds: `start`,
`drop` (by design, or fixable with a Fetch button), `note`, `result`, with
`start - sum(drops) == result`. A **`note`** is shown but never counted: it says
something about names still in the funnel, sits between the start and the result, and
may carry a `link` to where its cause is shown. The board and the card render a note
with a ⚠ marker, a positive count and that link, never a button: long store-writing
jobs start only from Settings.

Identity R4 (#87): the classification universe (`core/universe.py`) drops the
identity-master types `identity.types.EXCLUDED_TYPES` at every cap. Co-movement states
it as three by-design drops after *No market cap* (*Funds / ETFs*, *SPACs*, *Preferred /
warrants / units*, from `universe.TYPE_GROUPS`), then, when kept names have no
established type (no `security_master` row, NULL, `unknown`, `other`), the note
*Unknown security type — kept* linking to Settings → Identity. A funnel persisted before
R4 has no type rows and no note. The counts come from `universe_counts()`:
`scanned_total = below_floor + no_market_cap + floor_total` and
`floor_total = funds_etfs + spacs + other_securities + classification_total`. The types
are read with a separate query, never a JOIN: `rawstore.query` answers `[]` for a missing
table, so a JOIN would silently empty the universe.

### Automations panel (#130)

`core/automations.py` is the one registry of the scheduled tasks RevenueChaser owns, and the one installer.
Rows: `RC-Store-Backup` (daily 04:00, `scripts.backup_store --keep 5`), `RC-Relatedness-OffPeak` (12:30,
`--run --cap 8`), `RC-ValueChain-Sync` (13:00, `--sync-filings`), `RC-Daily-Bars` (22:35,
`scripts.daily_bars --nightly --sec-files`), `RC-Screeners-Daily` (weekdays 23:00, after `RC-Daily-Bars`, `app.universe_cli`),
`RC-Forecast-Outcomes` (daily 23:45, after `RC-Daily-Bars`, `scripts.forecast_outcomes --nightly`; #380 S0),
`RC-Index-Lists` (Saturday 10:00, `scripts.index_lists --weekly`) and `RC-Earnings-Inputs` (Tuesday to Saturday
01:30, after `RC-Daily-Bars`, `scripts.nightly_inputs earnings`; E0 #372). Each entry carries its schedule, the
tables it writes, whether it is off-peak gated, a `log_path` under `.cache/logs/` and its Windows task
settings (runs on battery, wakes the PC for the backup, catches up a missed run).

- **Nothing installs itself.** `default_install` only marks a row. Install, Enable, Disable, Remove and Run now
  are buttons on the panel (Install, Remove and Run now ask for confirmation); the two `.ps1` files are thin
  callers of the same installer. Nothing installs on import, dashboard start, Refresh, a GET or in a test.
- **Live state is polled, not baked in.** The board is a cached artifact, so the panel reads
  `GET /api/automations` (PowerShell `ScheduledTasks`, exact registry names only) on load, after an action and
  while a row runs. `POST /api/automations/<name>/<action>` takes a registry name and no other argument, and the header
  `X-RC-Panel: automations` (exactly; any other value is a 403).
  Refresh of this board never calls the scheduler.
- **Row state.** Not installed / disabled / enabled / running, last result mapped (0 ok; 0x41303 "not run yet"
  is neutral; 0x41301 is running; anything else is red), plus next run, missed runs and the age of the last
  success (read from the task's log).
- **Exit codes.** A task's CLI exits non-zero when it locked out or failed (Value Chain, Relatedness lock skip).
  Only the Relatedness off-peak deferral exits 0, and it logs `pending` / `next_off_peak`.
- **Nightly chain (#129).** An entry with `after=<registry name>` is a chained step: still its own task with its own
  daily time, which is its not-before time, and its schedule reads "daily 23:00 (local), after RC-Daily-Bars". When it
  starts (also on a catch-up or Run now), `run_task` reads the predecessor's log and waits, up to
  `chain_wait_minutes` (default 120), for the predecessor's run of tonight: the newest run that started at or after
  the predecessor's last scheduled time. Exit 0 releases the step. Any other exit, a step blocked in turn, or no such
  run in time blocks it: its command does not run, the log says `chain: blocked: <why>` and the task exits 5
  (`CHAIN_BLOCKED`), so the row is red with the reason. A step names an earlier registry entry (no cycles), is due 0
  to under 12 h after its predecessor on the clock (a step due before it would be released by last night's run),
  whose predecessor runs daily (the predecessor's time is read as a daily one, so a step after a weekly task would be
  blocked on every other day), and waits a positive time shorter than its time limit (`check_chain`, run on import).
  Two rows are chained. `RC-Forecast-Outcomes` (#380 S0) waits for `RC-Daily-Bars` every day, not before 23:45, and
  recomputes the outcome rows of the frozen picks from the stored bars (no calls; it writes `forecast_outcomes` only).
  S2 `RC-Screeners-Daily` (#129) waits for S1 `RC-Daily-Bars`, Monday to Friday, not before
  23:00, and runs the universe refresh CLI (AG with the universe pull, Swing, Fundamentals, the #128 freeze; exit 3
  when another universe run holds the lock). It wakes the PC, runs on battery and catches up a missed run; its 4 h
  limit counts from the task's start, wait included. It is not installed by default. S3 is `RC-Earnings-Inputs` (below);
  S4 (market) adds its own when its job exists.
- **Nightly inputs steps (C25a/K11, #383).** An entry whose command is
  `scripts.nightly_inputs <step> --max-uw-calls N --max-fred-calls N --max-yf-calls N` runs the sweeps of one
  inputs step (`core/nightly_inputs.py`, `STEPS`). `check_inputs_steps` (run on import) refuses such an entry unless it
  runs after `RC-Daily-Bars`, names a known step, lists its tables and states all three caps. The run resolves one
  New York session; a run inside the US session runs nothing and exits 0. Each sweep runs on its own: one that
  raises is `failed`, the others still run, and the task exits 1 so the row is red. A cap is per night and per
  provider, shared by every sweep: a request past it is refused before it is sent (one log line), and a second run
  of the same session starts from what the first spent (`.cache/nightly_inputs/<step>_calls.json`). A UW call is one
  `get_result` (its retries are the same call). Writes are first-write-wins appends (`record_first`).
  The first step is `RC-Earnings-Inputs` (E0, #372; `module_earnings_radar/inputs/`), Tuesday to Saturday 01:30, so
  each run keys to the US session that just closed. Its sweeps, in order: D26 earnings calendar (UW, ~40 calls), D37
  economic calendar (1 UW call), D27 eight FRED series (8 FRED calls), D10 short interest (UW screener, then
  interest-float per miss, then yfinance for calendar tickers; the calendar tickers before the 13F tickers) and D14
  analyst estimates (one yfinance read per calendar ticker, nearest earnings first; five failures in a row stop it).
  Caps: 200 UW, 10 FRED, 2000 yfinance a night, no SEC call. Not installed by default; the PC installs it and makes
  the first run by hand (a run between 15:30 and 22:30 Amsterdam is inside the US session and writes nothing).
- **SEC exchange and fund files (#301 F0, D21 + D8).** `RC-Daily-Bars` runs `--sec-files`: before the bars, on a
  New York weekday, `core/sec_files.py` asks `company_tickers_exchange.json` and `company_tickers_mf.json` once each,
  with no retry (a marker per file and session under `.cache/sec_files/`, so a rerun the same night asks
  nothing: at most 2 SEC requests a night). It appends their changes to `sec_exchange_file` and `sec_mf_file`
  (change history, rule A); a file under its floor (3,000 Nasdaq / NYSE / CBOE rows, 10,000 fund classes) writes
  nothing. Its result is the `sec_files` key of the daily-bars JSON line and a `SEC files:` stderr line; a failure is
  never a different exit code, so the steps chained on `RC-Daily-Bars` still run. No Data Health row reads the two
  tables yet.
- **Run log.** A task that writes tables leaves one `job_runs` row per run (`trigger` `scheduled`,
  `registry_name`), `running` while its command runs and `done` or `error` after it (a chain-blocked step:
  `chain blocked: <why>`); what the command writes carries that row's `run_id` (#129 A1, see
  `docs/data_architecture.md`). The backup records none.
- **Tests** use `FakeScheduler`; the e2e harness installs it so no test reaches the real Task Scheduler.

### Not this board

- Merging with Implied Probability / Relatedness / Value Chain.
- Putting MC / GICS / Theme filters here.
- Scheduling inside the app (no in-app cron). Scheduled tasks are Task Scheduler tasks listed on the Automations panel (above).
- Downloading 10-Q / 8-K into `chain_filings` (Item 1/4 only).
- Hoberg–Phillips TNIC as a store table (see §3).

---

### Identity refresh job (issues #85, #89)

The **Identity spine** row of the inventory table carries the **Fetch identity** button: it posts
`POST /run/identity` (optional positive `limit`, long lane, `requires_uw=False`) through the shared Settings
poller. It is a **live universe writer**: a successful run (`ok` or `partial`) applies the master's sector and
industry (plus UW's next earnings date) to `equity_universe` (`core/identity/apply.py`, R3 #86, #265), so it changes
sectors on every board; the help text and the card (`#identity-writes`) say so. The job ends with a `JobMessage`,
e.g. `7,366 tickers · 213 funds/ETFs · 116 SPACs · sectors: S&P 1,501 / UW 4,900 / Yahoo 325 / SIC 120 /
unclassified 64 · 266 conflicts · universe 7,000 updated / 3 without a master row · sources ok 7/7`. Each run writes an `identity_runs`
row (per-source status, rows, fetched count, seconds, error). The row's holes are the master tickers with no sector
source at market cap >= $300M; its note is `last run <status>` or `no identity run yet`. See `data_architecture.md`
for the fetch plan.

Below the inventory table, the **Identity health** card (`<section id="identity">`, so `#identity` is linkable) is
rendered from the store only (`inventory.collect()`, zero network; a missing table is an empty section, not an error). With no run and no `security_master` rows it
shows `No identity run yet. Use Fetch identity in the table above.` Otherwise:

- **Last run** (`#identity-run`): status, started and finished, tickers and conflicts, and each selected source's
  status, fetched / rows and error.
- **SPDR line** (`#identity-spdr`): `SPDR check: 478/480 agree (date)`, or `SPDR check failed: <error> (last good:
  <date>, n/m agree)` / `(no good run yet)`, or `not run yet`. It reads the latest run that selected SPDR.
- **Security types** (`#identity-types`): per type, all tickers and those at or above the $300M floor; excluded
  types (`cef`, `etp`, `spac`, `preferred`, `unit`, `warrant`) carry an `(excluded)` tag.
- **Sector source mix** (`#identity-sources`): official (the S&P 1500 list, #272) / provider (UW or Yahoo) / SIC /
  type rule / unclassified, and `other` (`manual`) only when non-zero.
- **Conflicts** (`#identity-conflicts`): "current" means `last_seen` equals the `started_at` of the latest `ok` or
  `partial` run (`refresh._write_conflicts` stamps `last_seen` with it). Counts by kind, an `N older, not seen since
  <date>` line, and a collapsed table of the top 50 by market cap (the source-level `*` row last).
- **Cached source freshness** (`#identity-stale`): newest fetch per cached source; for `sec_submissions` (30
  days), `openfigi` (90 days, 7 for a `no_match` row), `yahoo_sector` and `fdic` (90 days) the number of universe
  tickers past the window, computed with `refresh._stale` and the refresh job's own constants. `sec_exchange`,
  `nasdaq`, `sp1500` and `uw_sector` refetch every run and show `every run`. A cached source the refresh no longer
  fetches is not listed.

**SPDR sector check.** The run fetches the 11 SPDR Select Sector holdings files (SSGA daily xlsx, base URL
`RC_SPDR_BASE`) and compares each S&P 500 member's sector in the master (built in the same run) with the sector of
the fund that holds it, named the Morningstar way (XLY is Consumer Cyclical, XLP Consumer Defensive, XLV Healthcare,
XLF Financial Services, XLC Communication Services, XLB Basic Materials). The S&P 500 members are the
`index_constituents` rows of slug `sp500` (UW's spelling normalised); a ticker is checked only when a fund holds it
and the master has it. It is a *check*, never a value: nothing in `security_master` reads it and its rows are not
cached. A mismatch is an `identity_conflicts` row of kind `spdr` (side A the master sector, side B the SPDR sector) and the counts carry
`spdr_checked` / `spdr_agree`. The source is all or nothing (one fund failing fails the check), it is not counted
in "sources ok n/m", and a failed check writes no conflicts and no counts and leaves the run `ok`.

**SEC submissions stagger.** Besides tickers with no cached row or one older than 30 days, each run refreshes the
oldest 25% of the cached rows that are at least 7 days old (`SEC_STAGGER`, `SEC_STAGGER_MIN_DAYS`), so a universe
that was fetched in a single day does not expire together; the run records `fetched` (CIKs) and `staggered`.

### Discord export readiness (issue #242)

Below the Identity health card, the **Discord export readiness** card (`<section id="discord">`, card
`#discord-readiness`) answers "can I run Discover and Update yet?" before the first export. It reads the environment
and the CLI file: no network call, no `.store/` table. One row per check, in the order the user fixes them
(`module_discord/readiness.py`, `payload["discord"]`):

| Check | `ok` when | Otherwise |
|---|---|---|
| DCE CLI path | `RC_DCE_CLI` is set and the file exists (`credentials.dce_cli_problem()`, the check the jobs use) | `fix`, with that message |
| DCE version | the CLI check is `ok` and the version is at least `exporter.MIN_DCE_VERSION` (the minimum is named in `data_architecture.md`) | `fix` when too old; `unknown` with the reason when it cannot be read; `not checked` while the CLI check fails |
| Discord token | `credentials.key_for("discord")` is non-empty | `fix`, with `credentials.missing_message("discord")` |
| Server id | `RC_DISCORD_GUILD_ID` is set (`credentials.discord_guild_problem()`) | `fix`, with that message |

The summary line says **Ready** only when all four are `ok`, else `Not ready: n checks need attention`.

- **Values never shown.** The token is read only to check that it is set. The token and the server id appear as set
  or not set only: not in the payload, the page or the `errors` list. The card says it cannot tell whether Discord accepts the token; only a Discover or Update can.
- **The version is the one thing that starts the exe.** `exporter.cli_version()` runs `<exe> --version` (offline,
  10 s timeout, no console window on Windows, every `*_KEY` / `*_TOKEN` variable removed from its environment)
  and caches the answer in the dashboard process per exe file
  `(path, size, mtime)`. The first Settings load after a dashboard start, or after the exe file changes, starts it
  once; every later Refresh reads the cache. A hang, a non-zero exit or unreadable output is `unknown` and is cached
  the same way, so it cannot cost a timeout per Refresh. A missing path or a file that vanishes before it is read is
  `unknown` and not cached. Restart the dashboard to force a re-check of an unchanged file.
- **A failed check is recorded, not hidden.** If building the card raises, the card is omitted and the failure is
  listed in the page's errors list (F4).
- The test suite never starts the real exe: the root `conftest.py` clears `RC_DCE_CLI`, `DISCORD_TOKEN` and
  `RC_DISCORD_GUILD_ID` for every test, and the e2e dashboards start with them blank.

## 3. External research — take / reject

Checked 2026-09-24. None of these replace `sec_edgar` + `edgar_text.pick_filing`.

| Source | What it is | Verdict |
|---|---|---|
| [nlpaueb/edgar-crawler](https://github.com/nlpaueb/edgar-crawler) (user link: lefterisloukas/edgar-crawler) | Download + structured JSON items for 10-K / 10-Q / 8-K. GPL-3. WWW 2025. | **Do not vendor** (license). We already pick latest 10-K/20-F from `submissions.recent` and slice Item 1/4. Optional later: 10-Q/8-K item map is a **new** corpus, not a freshness fix. |
| [pChitral/ETL-SEC-EDGAR-10-k-Filings](https://github.com/pChitral/ETL-SEC-EDGAR-10-k-Filings) | MDA (Item 7) → per-ticker CSV. | **Reject.** Wrong section (we need Item 1/4), file-based, not a DuckDB store. |
| [psachdev/sec_data_downloader](https://github.com/psachdev/sec_data_downloader) | Thin wrapper around **sec-api** (paid) + `sec-edgar-downloader`. 0 stars. | **Reject.** We hit SEC directly; no paid proxy. |
| [Hoberg–Phillips Data Library](https://hobergphillips.tuck.dartmouth.edu/) | Academic TNIC / Doc2Vec **product-description** similarity, 1989–**2023**. Annual lag. | **Not a 10-K fetcher.** Relatedness already profiles live Item 1. Optional later overlay to *validate* NMF peers — not SSoT, not v1, coverage stops 2023. |

Idea we **already implemented** (P4c): compare stored accession to
`submissions.recent`; download only on mismatch. The gap is **trigger +
visibility**, not another crawler.

---

## 4. 10-K “is there a newer filing?”

**The check, end to end** (already implemented — `sync_one`):

1. ticker → CIK via `sec_company_tickers` (`sync.universe_ciks`).
2. `GET {SEC_DATA_BASE}/submissions/CIK##########.json`
   (`edgar_text.submissions_url`).
3. `pick_filing(payload)` — newest **10-K**, else newest **20-F**; a 10-K/A
   never outranks a 10-K.
4. Compare that `accession` to `chain_filings.accession`.
   - equal → `{"status": "current"}`, **no download**
   - missing / different → fetch the primary HTML → `edgar_docs/` +
     `edgar_documents` → re-slice Item 1/4 → upsert `chain_filings`
     (`{"status": "updated"}`)
   - no 10-K/20-F in `recent` → `no_filing`

Run: `python -m RevenueChaser.module_value_chain --sync-filings [--limit N] [--force] [--floor M | --tickers A,B]`.
`--floor` (USD millions) or `--tickers` choose the names (#108 S0); without either it covers the $300M
classification universe, and the submissions sweep every universe CIK.
Cost: one submissions call per CIK at 10 req/s, archive GETs only on mismatch.

What to add:

1. Dashboard job for `sync_filings` (settings board Fetch on the corpus row).
2. Inventory column: `classification_universe` tickers with no
   `chain_filings` row; rows whose accession is older than last sync
   **once we cache submissions dates** (v2). v1 sync is live SEC;
   Refresh of the settings board does **not** call submissions.
3. Do not treat FY vs calendar year as a bug (AMZN FY2025 10-K filed 2026-02).
   Compare **accession**, not the year in the filename.

MU today: stored accession = SEC latest. A universe cap backfill will not
change that. Next MU 10-K is the usual October cycle.

---

## 5. Implementation order (after explicit go)

Hermes executes by default (rule E): in-session for light work, or a queued Hermes
one-shot for a slice. OpenCode only for a mature, already-decided plan. Hermes
orchestrates and owns review. TDD. Full unit suite. No live store writes in tests.

1. **Universe Phase B** — failing test: screener omits `marketCap`,
   `fast_info` supplies it → `classification_universe()` includes the name.
   Then writer + provenance. (Fixes MU on OIPD without a settings board.)
   Per the feasibility review: UPDATE-only helper, Phase A cap carry-forward,
   null-only backfill (no every-ticker pass), `.info` fallback never hit,
   `__stats__` provenance key.
2. **Wire `sync_filings`** as `POST /run/edgar-sync` (or equivalent); unit +
   dashboard job tests; e2e: Refresh of Value Chain still zero SEC calls.
3. **Settings board** — store-only inventory first (Refresh); then Fetch
   buttons for universe + edgar-sync. `help_html`. Register
   `_STORE_REFRESHABLE` / `STORE_BACKED`.
4. Per-ticker drill on that tab.
5. e2e: settings Refresh makes zero Yahoo/SEC; universe Fetch fake fills
   a null cap.

Schema: Phase B v1 **no** `_SCHEMA_VERSION` bump if we only fill existing
columns. Adding `cap_source` later **does** bump (currently `"6"`) → warn
to snapshot `.store/` by hand.

---

## 6. Out of scope

- Filter-panel / Theme cascade (still [`dashboard_ui.md`](dashboard_ui.md) §2b; still waiting on go).
- Recrawling every 10-K on Value Chain Fetch.
- 8-K / 10-Q into Value Chain.
- Hoberg TNIC ingest.
- Calling `Ticker.info` for the full universe.
- One-click “refresh all datasets”.
- Push / live Yahoo on the real store until go.

---

## Checklist (when building)

- [x] Phase B backfill + tests; provenance written
- [x] `/run/universe` runs A then B
- [x] `sync_filings` job on the dashboard
- [x] Settings tab: Refresh = inventory, Fetch = per-row job
- [x] `_STORE_REFRESHABLE` + `STORE_BACKED`
- [x] `help_html` on the settings tab
- [x] Full `pytest` (not a `-k` subset) — 1877 passed, 0 failed
- [x] e2e: settings Refresh zero network — `module_testing/e2e/test_board_settings.py`
- [x] Standalone caps job `POST /run/universe-stats` — Phase B only (no screener, no
      scanners), optional positive `limit`, `requires_uw=False`, re-renders Settings
- [x] Per-ticker drill `GET /api/settings/ticker/<t>` — store-only, 404 on unknown, 500 `store read failed` on a failed store read (DF98),
      shared ticker combobox, null cap rendered as "no cap stored (outside the $300M universe or not yet resolved)"
- [x] e2e observability for yfinance — `$RC_E2E_YF_LOG` + `Dashboard.yf_calls()` in the
      harness, so "Refresh made no Yahoo call" is assertable across the process boundary
- [x] Tick this list; do not claim the board works from unit tests alone

