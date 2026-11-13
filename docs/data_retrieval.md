# Data Retrieval — Fetch vs Refresh

Companion to [`data_architecture.md`](data_architecture.md), which says *where*
data lives. This one says *how it gets there and how it comes back out*.

Every board has exactly two ways to produce what you see, and they must never be
confused:

| | **Fetch** | **Refresh** |
|---|---|---|
| Button | `Fetch` (or a board-specific one) | `Refresh Tab` |
| Route | `POST /run/<module>` (with `X-RC-Panel`, see below) | `GET /view/<module>?refresh=1` |
| Network | **Yes** — hits providers | **No — zero calls, always** |
| Writes | `.store/` (raw + edgar DuckDB, `edgar_docs/`), then `.cache/` | `.cache/` only |
| Cost | seconds to minutes, rate-limited | fast, offline |
| Needs credentials | usually | never |

**The contract, stated once:**

> Fetch updates the raw data in the store over the network, then recomputes and
> re-caches the board. Refresh drops the board's `.cache/` artifact and
> re-derives it from the store (`raw.duckdb`, plus `edgar.duckdb` for the
> EDGAR-backed boards) with **zero network calls**.

This is not a guideline. It is asserted for every store-backed board by
[`e2e_testing.md`](e2e_testing.md), which counts requests to a mock provider
server across a refresh and fails if the counter moves.

**A board's payload file (K10, #383 C6a).** A board may keep its rows outside its HTML: it writes them with
`reporting_common.write_board(html_path, html, payload)`, which stores `<stem>.payload.json.gz` next to the
HTML, and the page fetches them from `GET /view/<module>/payload.json` (`rcPayload(mod)` in `PAYLOAD_JS`).
The file is served as stored with `Content-Encoding: gzip`, so what a board view downloads is counted compressed
(card F). Refresh drops the payload file with the HTML, and `write_board` without a payload drops a stale one, so
a rebuilt board never serves old rows. Asymmetric Growth is the first board on it (#380 W1 S7: its cards are
`asymmetric_growth_latest.payload.json.gz`, written by a Fetch and a Refresh alike); Swing and Fundamentals move
in R3. The PC guard
`scripts/research/383_page_weight.py` checks that no board's HTML plus payload grows past its baseline.

**A Refresh shows the last Fetch's notes (#383 C24).** LEAPS and SEC 13F put their Fetch notes on the board (the
scan's notes; names not refreshed and the CUSIPs left out). Their Refresh reads them back from the last run's
result-history header (`runners.last_fetch_notice`, K2 `history.last_run`) and shows them as "Last Fetch (time):
...". The read is local and never creates `history.duckdb`; a failed read shows no line.

**SEC 13F rows on the payload route (#429).** The 13F page is chrome and scripts; its rows are
`sec_13f_latest.payload.json.gz` (`report.board_payload`: per row the ticker, name, sector and one
`[text, sort value, class]` cell per column, the texts formatted in Python as before), written by a Fetch, a
rebuild and a Refresh alike, and the page builds the table with `createElement`/`textContent`. Every row still
ships: with 14,656 tickers the page is about 15 KB and the payload about 2.5 MB gzip (was one 27 MB page), and a
Refresh takes about 3 s (was 79-121 s; the old render grew one page string row by row, which Windows copies on
every append). A NaN or infinite number gets an empty sort value, since `JSON.parse` refuses them.

---

## Why the split exists

The store is the expensive thing to fill and the cheap thing to read. Separating
the two means:

- **You can iterate on presentation offline.** Change a renderer, hit Refresh,
  see the result — no API quota burned, no rate limit, no waiting.
- **A provider outage degrades to "stale", not "broken".** Every store-backed
  board still renders from the last fetch.
- **A dead API key cannot empty a board.** Refresh does not need one.

The third point was learned the hard way: providers used to swallow HTTP errors
and return `{}`, so an expired token rendered as an empty market rather than an
error. See *Credentials* below.

---

## Per-board behaviour

Boards that are **store-backed** support both. Boards that are **live-only** hold
no downloaded state worth re-deriving, so they keep a "click Fetch" empty state.

| Board | Fetch route | Params | Refresh |
|---|---|---|---|
| Earnings Radar | `/run/earnings` | `start`, `end` (ISO) | **live-only** |
| SPX Gamma | `/run/spxgamma` | `date` | bespoke view; see below |
| Financials | `/run/quick` | `ticker` (required) | per-ticker: `?refresh=1&ticker=X` |
| UW Interaction (lab) | `/run/uw` | `tool` (required) + tool params | **live-only** — board renders from its stored `.cache/uw_interaction/` payload |
| Co-movement | `/run/module_comovement` | `no_fetch` | model-only; writes .cache only (runs persist on Fetch) |
| Value Chain | `/run/module_value_chain` | `no_fetch` | model-only (reads `edgar.duckdb`; on the fundamentals layer the 10-K business sections and the XBRL major customers in `fundamentals.duckdb` instead, and a Fetch downloads nothing) |
| Relatedness | `/run/module_relatedness` | `no_fetch` | model-only (reads the filings in `edgar.duckdb`, or on the fundamentals layer the annual report text in `fundamentals.duckdb`, and the `relatedness_*` profiles in `raw.duckdb`). Fetch also downloads missing filings from SEC and calls DeepSeek off-peak for new profiles; only Fetch writes `relatedness_scores` and `run_registry` (`module_relatedness/engine.py:81-92`, `:449-466`) |
| Sector Rotation | `/run/rotation` | `start`, `end` (optional) | store |
| Asymmetric Growth | `/run/module_asymmetric_growth` | `refresh_universe`, `no_fetch` | model-only |
| Swing Potential | `/run/swing` | `refresh_universe`, `no_fetch` | model-only |
| Fundamentals | `/run/module_fundamentals` | `refresh_universe`, `no_fetch` | model-only |
| SEC 13F | `/run/module_sec_13f` (Fetch: names/sectors → shares outstanding → render; no UW key needed, and no SEC call since G9 — the builder owns the numbers) | — | store |
| LEAPS | *(the scan is switched off, #730: `/run/module_leaps` answers `409` and the job is not registered; the tab serves a "no data yet" page)* | — | model-only |
| Watch lists | *(none, #786: a watch list is typed, not downloaded; the board has no Fetch and no job)* | — | store — avo's lists from `boards/watchlists.duckdb`, the numbers beside them from the rcdata layer (`prices.bars`, `prices.latest`, `identity.security`, `market.series`) |
| Forwards Testing | *(none)* | — | always live from files |
| **Universe** (header) | `/run/universe` | — | n/a — SEC member list + market data into `equity_universe`; not a tab |
| **Universe caps** (Settings) | `/run/universe-stats` | — | store → `.cache/` — fills only the NULL `equity_universe.market_cap` rows (Phase B alone) |
| **10-K/20-F sync** (Settings) | `/run/edgar-sync` | — | store-only inventory lives on the Settings tab; see [`data_health.md`](data_health.md) |

`/run/universe` pulls the member list from the SEC exchange file
(`company_tickers_exchange.json`, Nasdaq / NYSE / CBOE rows; one request per pull to www.sec.gov) and the Yahoo
**screener** quotes (`yf.screen`) that join it by ticker, then runs the three scanners. Screener quotes can
omit `marketCap` (MU) and a listed name may have no quote at all, so Phase B
backfills those rows from `fast_info.shares × fast_info.last_price`; a failed SEC map never shrinks the universe.
A partial pull (a market-cap slice that did not finish, or quotes for under 90% of the stored names that have one)
upserts what it got, deletes nothing, skips Phase B and shows "partial fetch" on the boards (#146).
Both phases also write the session's Yahoo rows of `quote_snapshot` (#294). When a UW key verifies, the job then
calls the **UW stock screener** for the same table, 100 tickers per call (about 78 calls for a 7,800-name universe,
fewer on a second run of a session, which asks only the tickers with no UW row yet), before Swing and Fundamentals.
No key, a rejected key or an open breaker skips or stops the UW side with a job note; the universe job stays
`requires_uw=False`. A run inside the US session (09:30-16:30 New York), or one that crosses a session boundary,
writes no `quote_snapshot` rows.
`/run/universe-stats` is that backfill
**on its own** — no screener, no scanners, optional positive `limit`, so the
1,463 null caps can be filled without re-pulling membership; it is `requires_uw=False`
and re-renders Settings when it finishes. Its fills also write Yahoo `quote_snapshot` rows (no UW call).

On the three screener boards (Asymmetric Growth, Swing Potential, Fundamentals) a Refresh, and a plain load that
has to rebuild a missing artifact, read the **stored** universe only (#145): with none stored the board says
*No stored universe yet. Press Fetch Universe* (or *Loading universe…* while a universe job is queued or
running), and nothing starts a job from a GET. Only a Fetch reaches the Yahoo screener and only a Fetch writes
`ticker_scores`, `swing_scores`/`swing_gates` and `fundamentals_scores`/`fundamentals_gates`; a Refresh writes
`.cache/` only. A Fetch that scores nothing ends with the reason as its job message.
Swing and Fundamentals run the same scan body, `core/screen_common.run_screen` (#147): universe, identity-type
exclusion, quote prefilter, the `MAX_PHASE2` shortlist, a thread pool over the tickers, then the payload and, on a
Fetch, the score tables. A ticker whose row raises is counted as `compute_error` in `data_quality.dropped` and the
scan carries on; a short SPY series leaves `rs_6m` empty instead of failing the scan.

Value Chain dashboard Fetch is **cache-first** (`fetch_corpus` skips tickers
that already have a `chain_filings` row). Newer 10-K detection is
`python -m RevenueChaser.module_value_chain --sync-filings`, exposed on the
Settings tab as `POST /run/edgar-sync` (`requires_uw=False`); it is deliberately
**not** triggered by every Value Chain Fetch.

The two refresh flavours differ only in mechanism:

- **model-only** (`_MODEL_ONLY_RENDERERS` in `app/dashboard.py`) re-runs the
  module's own `no_fetch=True` path over the store's caches.
- **store** re-renders from a purpose-built loader (`_render_sec13f_dashboard`,
  `_render_tw_dashboard`, `_render_rotation_dashboard`).

Both are in `_STORE_REFRESHABLE`, and the client mirrors that set as
`STORE_BACKED` in `templates/dashboard.html` — **keep the two in sync**, or the
button silently degrades to a plain iframe reload.

### Routes that are not what their name suggests

- **`/run/module_sec_13f` performs no network at all.** It renders from
  `sec13f_totals` in the store; its fetch and its refresh are the same
  computation. It is registered `requires_uw=False` for exactly that reason —
  gating it on a key made the button fail for users whose token had expired.
  The board's *actual* network path is `/run/module_sec_13f-edgar` (the SEC
  EDGAR quarter backfill), also credential-free.
- **`/run/quick` bypasses the credential preflight** entirely — Financials is
  yfinance- and store-backed.
- **SPX Gamma's `?refresh=1` is its own.** Its view is intercepted before the
  generic refresh path: Refresh rebuilds the newest stored raw day from its board
  file (`.store/boards/spx_gamma.duckdb`) and makes no network call. The VIX family, the
  CFTC COT, the 8-week SPX closes and the 0-1 DTE dollar gamma the pin and squeeze
  tests size by are read from the market store at render time
  (`module_spx_gamma/store_reads.py` through `needs.ask(..., as_of=day)`, #330:
  `market_series` and `spx_gamma_expiry_strike`; VIX and COT judged against the
  day rendered), so a Fetch and a Refresh of a day render the same.
- **SPX Gamma's Fetch (`POST /run/spxgamma`) also asks the builder** (PR 3, decision
  A4): the job of an accepted Fetch first queues one `uw/spot_expiry_strike` and
  one `market_series` build (`ops.enqueue_once`, `requested_by` `spx_gamma`; one
  already queued adds nothing; a refused Fetch queues nothing), then runs the
  board's own UW snapshot as before and renders at once with what is
  stored, saying "builder requested at <time>; the builder runs hourly at :05". It
  never waits on the builder. There is no live fallback any more: no yfinance VIX,
  no Socrata COT, no candle fetch; an input the store does not hold for the day
  shows "<input> not stored for <day>" on its panel and in the data-quality banner,
  and a missing 0-1 DTE slice raises no pin flag and no squeeze trigger (avo,
  28 Oct 2026). The live spot during an open
  session (one yfinance request, decision A7) is unchanged.

### The market-store builder's routes (PR 2)

The builder (`python -m RevenueChaser.rcdata.build`) is the only process that writes the market store. These
routes only enqueue and read `ops.sqlite`. Each call opens its own SQLite connection and no DuckDB file.

- **`POST /api/build/request/<dataset>`**: queues one build (`requested_by` = the `X-RC-Panel` value). With
  `backfill_from=YYYY-MM-DD` (JSON body or query string) it is a backfill request. 202 `{ok, id, dataset}`;
  400 for a dataset the builder does not know or a date that is not a past `YYYY-MM-DD`; 409 while one of that
  dataset is queued or running; 403 without the panel header.
  An attended dataset (the WRDS ones: the three spine datasets, `wrds/prices` and the four fundamentals tables)
  is queued too, with the press's own mode like any other dataset — the daily `fetch` unless `backfill_from` is
  given — and the answer carries `attended: true` and `waits_for`: the login and Duo need a terminal, so the
  request waits in the queue until `python -m RevenueChaser.rcdata.build attended` on the server takes it, and
  the hourly builder leaves it there. A press is never turned into a backfill: each WRDS dataset's `fetch` is not
  its backfill (for `wrds/prices` the monthly refresh of the last two years, where the backfill only fills
  missing partitions).
- **`GET /api/build/queue`**: `{ok, active, finished}`. `active` holds the queued and running requests, oldest
  first; `finished` the last 20 others, newest first.
**Stage only (`--no-duckdb`) and raw only (`--no-parquet`).** The builder's three steps are *fetch* (the
provider's answer into the raw store), *Parquet* (the typed partition) and *build* (the serving DuckDB and its
swap). Each flag names everything it drops:

| flag | fetch | Parquet | serving DuckDB |
| --- | --- | --- | --- |
| *(none)* | yes | yes | yes |
| `--no-duckdb` | yes | yes | no |
| `--no-parquet` | yes | no | no (implied) |

`run --dataset X --no-duckdb` and `backfill ... --no-duckdb` fetch and write Parquet
but do not build the serving file: the request ends `skipped`, with a note that says what the fetch really staged
(`build skipped (--no-duckdb); 3 raw files staged`, or `nothing staged (the fetch returned no file)` when the
dataset's provider window was closed, or `the backfill staged what it fetched` when the count cannot be taken), the serving file is untouched, and the dataset stays due, so the next ordinary pass builds it at no
provider call — the raw file it staged is the resume marker. It is for staging days of data without holding the
builder lock through a build each time (the fetch campaign of a kind that has never been built), or for preparing
the data on one machine and building it on another.

`--no-parquet` is the raw-only mode (#498): the fetch writes what it always writes into the raw store — the
provider's answers and the raw markers beside them (`.none` and `.absent.json` for an answer that was refused,
`start.json` for a walk's first day) — and no Parquet partition and no serving file. The note reads
`build and Parquet skipped (--no-parquet); 3 raw files staged`. It **implies `--no-duckdb`**, because Parquet is
the only input a build reads, so a build with no new partition would be a no-op that still took the builder lock.
The daily mode skips the dataset's `to_parquet_all` call outright; a backfill converts inside the dataset, so
there the switch is a run-scoped guard (`base.raw_only()`) that `base.write_parquet` reads — the one place a
Parquet file is written — and that writes nothing and returns the path it did not write.

**A dataset that writes a "this day is finished" marker in its conversion step skips the whole step in a raw-only
run, not only the write.** The EDGAR feeds (`sec/13f`, `sec/form4`, `sec/sched13dg`) write the day's `.done`
marker in `to_parquet_all` beside the typing, and `.done` is what the daily-index walk skips on, so a marker
written without its partition would hide that day's filings from every later run (only a hand-deleted marker
brings them back). `ownership/edgar.py` `to_parquet_all` and `ownership/sec13f_sets.py` `_fill_one` therefore
return early when `base.raw_only()` holds: the filings and the data-set zip stay in raw, the days stay open, and
the next ordinary run walks and types them at no provider call. A new dataset whose conversion has a side effect
of its own does the same.

For the rest, the raw file is the resume marker, so a later ordinary run (or `--no-duckdb` run) converts what is
already on disk at no provider call. **Two datasets are the exception and should not be fetched raw-only:**
`wrds/prices` and `wrds/fundamentals` resume from their *Parquet* snapshot, not from raw
(`prices/wrds_history.py` `_snapshots`, `fundamentals/wrds_fund.py` `snapshots`), so a second raw-only run asks
the snapshot query again — an attended query with a WRDS login, a Duo confirm and a query budget.

`--no-build` is the old name of `--no-duckdb`, kept as a deprecated alias so a queued request and a runbook line
keep working; a request's params accept `no_build` as well as `no_duckdb`.

Both are a person's switch: the hourly pass queues no params, so
the schedule can never leave a kind unbuilt. Both are fetch-only for the locks — one `rcdata_fetch_<dataset>`
each, never the builder lock — so two datasets fetch side by side and beside a running builder.

- **`POST /api/build/schedule/<dataset>/<pause|resume>`**: sets or clears `dataset_schedule.paused_at` /
  `paused_by`; 404 for another action.
- **`POST /api/build/automation-ds/<dataset>`** `{"mode": "off|raw|parquet|full"}`: what the hourly pass queues on its
  own for that dataset (`ops.dataset_automation`; no row is `off`); 400 for an unknown dataset or mode. The file-level
  `/api/build/automation/<kind>` is gone (avo, 10 Nov 2026).
- **`GET /api/idle`** (loopback, no header): `{idle, reasons}` from `app/jobs.py` `idle()`. It is false while a
  dashboard job is queued or running, while any `core/job_lock` name is held by any process, or while another
  process's live `.rc-proc-<pid>` marker sits in the store dir or the market dir (F8 A). `stop.ps1` reads it, and
  so does the deploy gate.

### The needs routes (#290, renamed in #329)

Read-only HTTP over `rcdata.needs`, the layer a board asks for a named need by (`needs.ask(need)`). They call no
provider, write nothing and open no second DuckDB writer, so a page may ask them on every render. The older
`/api/data` routes are gone; use these.

- **`GET /needs`**: `{needs: [...]}`, the need catalog sorted by name, each with `need`, `kind`, `dataset`,
  `reader`, `max_lag`, `max_lag_unit`, `ask_param`, `providers`, `fallbacks` and `legacy`.
- **`GET /needs/<need>`**: one need's answer, the rows with freshness, coverage and per-row provenance. A `fill`
  in the query string is ignored (a GET never fills). `?summary=1` (with `sample=N`, at most 50) answers the
  need's state without naming a ticker; `limit=N` bounds the rows, never above 5,000. 404 for an unknown need,
  400 for an unknown parameter or for a need that must be asked for something (`market.series`).

### The watch-list routes (PR 4)

The two watch lists (D2) live in `ops.sqlite` (`rcdata/ops.py`); each write is one `BEGIN IMMEDIATE` transaction
with its history row. No provider call, no DuckDB file.

- **`POST /api/watchlist/<option|stock>/toggle/<ticker>`**: puts the ticker on that list or takes it off;
  `{ok, list, ticker, on}`. The `X-RC-Panel` value (the board) is the row's `added_by` and the history row's `by`.
  404 for another list, 400 for a ticker `_valid_ticker` refuses, 403 without the panel header; none writes.
- **`POST /api/watchlist/<option|stock>/remove/<ticker>`**: the Settings cards' Remove; `{ok, list, ticker,
  removed}`. It never adds, so a Remove on a stale page cannot put back a name removed elsewhere.
- **`POST /api/watchlist/toggle/<ticker>`**: the pre-PR 4 route, now the stock list; answers `{ok, ticker,
  in_watchlist}` as before. `rawstore.toggle_watchlist`, `load_watchlist` and `save_watchlist` are the stock list too.
- **`GET /api/watchlists`**: `{ok, option: [tickers], stock: [tickers], lists: [...]}`. `lists` is what every
  board's "add to list" menu offers (#786 step 5, `module_watchlists/menu.py`): avo's own lists first, then
  `jobs.option` and `jobs.stock`, each `{id, name, group, job, count, tickers}`, where `tickers` are the US
  listings on it (the ones a board's ticker can match) and `count` the list's whole size.
- **`POST /api/watchlists/<list_id>/tick/<ticker>`** `{on: true|false}` -> `{ok, list_id, ticker, on}`: the menu's
  write. On one of avo's lists a tick stores the spine's spelling (`NASDAQ:NVDA`) in the unnamed top section and an
  untick removes every US listing of the ticker; on `jobs.option` / `jobs.stock` it is `ops.watch_add` /
  `ops.watch_remove`. Both ways it writes nothing when there is nothing to change. 400 for a bad ticker, a missing
  `on` or a ticker the spine cannot place on a US exchange; 404 for an unknown list; 403 without the panel header.
  The read itself never creates `ops.sqlite`; the read log it counts into is flushed to it once a minute, which
  may, as for every `rcdata.read` call. A flush that fails keeps its counts for the next one and never fails the
  read (#228).

#### The Watch lists board's own routes (#786)

These are avo's named lists (`boards/watchlists.duckdb`, `module_watchlists/store.py`). The two shared
`option` / `stock` lists above are on the same board since 13 Nov 2026 as the list ids `jobs.option` and
`jobs.stock`: of these routes only `symbol/add`, `symbol/remove` and `columns/<tab>` answer for them, and the two
writes go to `ops.sqlite` (`ops.watch_add` / `ops.watch_remove`), not to the store. Every write is store-only — one list, one section or one symbol — and answers
`{ok, payload}` with the list's whole payload again, so the page redraws from what was stored. Each needs
`X-RC-Panel` like every non-GET.

- **`GET /api/watchlists/search?q=`**: `{ok, q, rows}` — the add box's suggestions from the spine
  (`identity.security`), a ticker that starts with the query first.
- **`POST /api/watchlists/new`** `{name, description?}` -> `{ok, list_id}`; 400 for an empty or taken name.
- **`POST /api/watchlists/<list_id>`** `{name?, description?}` — rename the list or re-word its description.
- **`POST /api/watchlists/<list_id>/delete`** — the list, its sections and its symbols; the `wl_change` rows stay.
- **`GET /api/watchlists/<list_id>/export.txt`** — the list in TradingView's one-line format, its own spelling.
- **`GET /api/watchlists/<list_id>/columns/<tab>`** -> `{ok, tab, values}`, keyed by the symbol as avo wrote it:
  one tab's numbers (`financials`, `performance`, `risk`, `technicals`). Not in the first payload, because
  the last three read five years of bars and Financials reads the SEC statements, which a board opened on Price
  would pay for without showing; the page asks the first time the tab is opened and keeps the answer. 404 for an
  unknown list and for `price` (the first payload carries it). Only the US listings get a row. Financials
  (`module_watchlists/financials.py`): revenue and EPS over the trailing twelve months where a 10-Q follows the
  fiscal year, else the fiscal year (`fb` says which), P/E on the newest close (none for a loss), the year's cash
  dividends per share without specials, split-adjusted, their yield, and the one-year beta against SPY.
- **`POST /api/watchlists/<list_id>/symbol/add`** `{symbol, section_id?}`, **`/symbol/remove`** `{symbol}`,
  **`/symbol/move`** `{symbol, section_id, order?}` (a dropped row: its section, then that section's new order).
- **`POST /api/watchlists/<list_id>/section/add`** `{name}`, **`/section/rename`** `{section_id, name}`,
  **`/section/delete`** `{section_id}` (its symbols move to the first remaining section; the last one cannot go),
  **`/sections/order`** `{order}`.

### Known deviation (fixed 2026-09-30)

`module_sec_13f` used to refresh by unlinking the artifact and returning HTML
in-memory without rewriting `.cache/`; it now writes the artifact back like every
other board (backend plan B4).

---

## How a fetch actually runs

Fetches are **asynchronous**. `POST /run/*` returns `202` immediately with a job
id; a single background worker (`app/jobs.py`) drains the queue one job at a
time, so two scans never race on the writer lock or the same API quota.

```
curl -X POST -H "X-RC-Panel: cli" http://127.0.0.1:8000/run/module_sec_13f
                            -> 202 {"ok": true, "job_id": "ab12cd34"}
GET  /api/job/ab12cd34      -> {"job": {"status": "running"}}
GET  /api/job/ab12cd34      -> {"job": {"status": "done", "message": null}}
```

Every write (`POST`, or any method other than `GET`/`HEAD`/`OPTIONS`) must also send an `X-RC-Panel`
header with any non-empty value; without it the server answers `403` and starts
nothing (`{"ok": false, "error": "panel header required: reload this board"}`).
The dashboard's own pages send it, and a page from another localhost port cannot,
because a custom header forces a CORS preflight that the server never answers
(#231). From a shell: `curl -X POST -H "X-RC-Panel: cli" http://127.0.0.1:8000/run/<job>`.
The one stricter route is `POST /api/automations/<name>/<action>`: its header must be exactly
`X-RC-Panel: automations`.
The server answers only requests whose `Host` is a loopback name; anything else gets
`403 {"ok": false, "error": "forbidden host"}`. `RC_ALLOWED_HOSTS` adds extra names,
comma-separated and exact (`RC_ALLOWED_HOSTS=homesrv.tail1234.ts.net`): a wildcard entry is
dropped, so one name cannot widen to a whole suffix, and unset or empty keeps the default of
loopback only. A listed name counts as same-site for the `Origin` check too, so the pages served
over it can write. It widens that check alone — the server still binds `127.0.0.1`, so reaching it
by that name needs something that forwards the port.

A board cached before this rule has the old script baked in: open it once with
`?refresh=1` (Settings and Co-movement cache their page; LEAPS no longer serves a cached page, #730).

Terminal states:

| Status | Meaning |
|---|---|
| `done`, no `message` | the board was rebuilt with data |
| `done` + `message` | **it worked, and there was nothing to show** — e.g. no earnings in the selected range. The artifact is still rewritten with an explicit empty state, and the UI surfaces the message |
| `error` + `error` | the run failed; the string is meant to be actionable |

The `done`-with-message case exists because a silent success over an empty
result is indistinguishable from a broken button. A runner signals it by raising
`jobs.JobMessage`.

`jobs.execute(job_id)` runs one job synchronously and is the seam the
end-to-end tier drives.

---

## Credentials

`core/credentials.py` is the single source of truth for which key each provider
needs, which env var holds it, and where to get one. Two failure modes, two
different messages:

- **missing** — the env var is unset.
- **rejected** — the key is set but the provider returns 401/403 (expired,
  revoked, lapsed subscription).

A Fetch is refused at the button by two checks, in this order, and they answer
different questions:

- **presence** (`jobs.credential_problem`, #26) — a module declares the
  credentials it cannot run without, `jobs.register(..., requires=(...))`, and
  `_submit` answers `400` with `credentials.problem(name)`'s message **before**
  the job is queued. The names are the provider slugs plus the extra checks in
  `credentials.EXTRA_CHECKS`, which is empty today — the Discord archive took the
  only two with it when it became its own app, and the mechanism stays for the next
  one; an unknown name fails `register` at import. Presence only: no request is made and no key value is read, logged
  or returned. A module with `requires=()` is not gated, and neither is an
  optional provider (fred, openfigi, deepseek, the subscription gates) —
  their absence degrades a board rather than breaking a job.
- **validity** (`UWClient.verify()`) — one cheap authenticated probe, which
  catches a key that is set but expired. `requires_uw` still decides whether a
  `UWClient` is built at all (`app/jobs.py`), so the two flags are not
  interchangeable.

`active` only means "a key is present". It is **not** a validity check; use
`verify()`.

Provider errors are logged at WARNING with their HTTP status. A provider client
may still return `{}` on failure (callers depend on it), but the failure is never
silent, and `UWClient.auth_error` records the last credential rejection.

---

## Base URLs are redirectable

Every HTTP provider's base URL is read from an environment variable with the
real endpoint as the default, so a test (or a debugging session) can point the
whole app at a local server:

| Env var | Redirects |
|---|---|
| `RC_UW_BASE` | Unusual Whales (`core/providers/unusual_whales.py`, `core/index_lists.py`; also the fundamentals layer's `uw/financials`, off unless `RC_UW_PRELIMINARY=1`) |
| `RC_UW_BASE_ROOT` | Unusual Whales for `module_spx_gamma` (no `/api` suffix) |
| `RC_SEC_WWW_BASE` / `RC_SEC_DATA_BASE` | SEC EDGAR + 13F archives; the spine's `sec/company_tickers_exchange`; the ownership store's EDGAR daily index, filings (`sec/13f`, `sec/form4`, `sec/13dg`) and fails-to-deliver files (`sec/ftd`); the fundamentals layer's SEC datasets (`rcdata/build/fundamentals/`) |
| `RC_ALPHAVANTAGE_BASE` | Alpha Vantage, the fundamentals layer's `alphavantage/earnings_calendar` (`rcdata/build/fundamentals/earnings.py`; read at call time) |
| `RC_FORM13F_INDEX_URL` | SEC Form 13F data-set page (zip links are resolved from it) |
| `RC_FORM345_INDEX_URL` | SEC Insider Transactions data-set page (`sec/form4`'s first fill; same) |
| `RC_FRED_BASE` | FRED series (also the market store's FRED source) |
| `RC_CBOE_BASE` | Cboe daily-price index files (`module_earnings_radar/inputs/vol_index.py`, `rcdata/build/sources/cboe.py`) |
| `RC_CFTC_BASE` | CFTC Traders in Financial Futures (also the market store's CFTC source, `rcdata/build/sources/cftc.py`) |
| `RC_CFTC_DISAGG_BASE` | CFTC Disaggregated futures (`rcdata/build/sources/cftc.py`); default: `RC_CFTC_BASE`'s host, dataset `72hh-3qpy` |
| `RC_TREASURY_BASE` | US Treasury daily par yield curve CSV (`rcdata/build/sources/treasury.py`) |
| `RC_FED_BASE` | Federal Reserve site, the FOMC calendar page (`rcdata/build/sources/fed.py`) |
| `RC_BLS_BASE` | BLS site, the release calendar `.ics` (`rcdata/build/sources/bls.py`) |
| `RC_CFTC_SCHEDULE_BASE` | CFTC site, the COT release schedule page (`rcdata/build/sources/cftc_schedule.py`) |
| `RC_YAHOO_BASE` | Unset in production: Yahoo is read through yfinance, which has no base URL. When set, the MOVE and wheat history is read as `<base>/<quoted symbol>.json` instead (the CLI test's loopback mock; `rcdata/build/sources/yahoo.py`) |
| `RC_FDA_OOPD_BASE` | FDA Office of Orphan Products Development (OOPD) search |
| `RC_DEEPSEEK_BASE` | DeepSeek chat API (`core/providers/deepseek.py`; read at call time) |
| `RC_EVREN_BASE` | EVREN chat API, the default Relatedness writer (`core/providers/evren.py`, #360; read at call time). Default `https://evren-llmapi.ssyz.org.tr/v1`; the model is `RC_EVREN_MODEL` or the board's `WRITER_MODEL` |
| `RC_GROK_BASE` | SuperGrok chat API, the Relatedness Grok writer (`core/providers/grok.py`, #388; read at call time). Default the Grok CLI chat proxy `https://cli-chat-proxy.grok.com/v1`. Only https on `grok.com` / `*.grok.com` or a loopback mock is accepted, so the subscription token never goes elsewhere (`api.x.ai`, the metered developer API, is refused); the model is `RC_GROK_MODEL` |
| `RC_GROK_CLIENT_VERSION` | The `x-grok-client-version` header the Grok writer sends (`core/providers/grok.py`, #375). cli-chat-proxy answers HTTP 426 to a client below its minimum; the default `CLIENT_VERSION` is the Grok CLI release it was read from (xai-org/grok-build). Set it to the version a 426 names until the default is bumped |
| `RC_OPENFIGI_BASE` | OpenFIGI mapping API (identity refresh; the 13F CUSIP map, `core/providers/openfigi.py`, read at call time; the spine's `openfigi/map`, both its `ID_CUSIP`/`ID_CINS` and its `TICKER` jobs) |
| `RC_NASDAQTRADER_BASE` | Nasdaq Trader symbol directory (identity refresh) |
| `RC_FDIC_BASE` | FDIC institution list (identity refresh) |
| `RC_WIKIPEDIA_BASE` | Wikipedia S&P 1500 constituent lists (identity refresh; a sector source for the list's own names, #272) |
| `RC_SPDR_BASE` | SPDR index-ETF holdings files (identity refresh) |
| `RC_SSGA_BASE` | SSGA daily ETF holdings workbooks: SPY, MDY, SPSM, the spine's index membership (`rcdata/build/spine/funds.py`, read at call time) |
| `RC_ISHARES_BASE` | iShares `latest-holdings.csv` files: IVV, IJH, IJR (`rcdata/build/spine/funds.py`) |
| `RC_ARK_BASE` | ARK's daily holdings CSV: ARKQ, read when UW holds nothing for it (`rcdata/build/spine/etf_holdings.py`, #339) |
| `RC_VANGUARD_BASE` | Vanguard's daily creation baskets (PCF, `etfportal.vanguard.com`): VOO, IVOO, VIOO (`rcdata/build/spine/funds.py`) |
| `RC_NASDAQ_API_BASE` | Nasdaq's list API, the Nasdaq-100 members (`rcdata/build/spine/funds.py`) |
| `RC_INVESCO_API_BASE` | Invesco's fund holdings API: QQQ, the second Nasdaq-100 file (`rcdata/build/spine/funds.py`) |
| `RC_ALPACA_BASE` | Alpaca market data (`data.alpaca.markets`): the prices store's daily bars and corporate actions (`rcdata/build/sources/alpaca.py`, read at call time) |
| `RC_FINRA_API_BASE` | FINRA's keyless API, consolidated short interest (`rcdata/build/ownership/finra.py`, read at call time); default `https://api.finra.org` |
| `RC_REGSHO_NASDAQ_BASE` | Nasdaq Trader's Reg SHO threshold files (`rcdata/build/ownership/regsho.py`, read at call time); default `https://www.nasdaqtrader.com/dynamic/symdir/regsho` |
| `RC_NYSE_BASE` | NYSE's site, the Reg SHO threshold downloads of NYSE, NYSE American and NYSE Arca (`rcdata/build/ownership/regsho.py`, read at call time); default `https://www.nyse.com` |
| `RC_IBKR_SHORT_FTP_HOST` | IBKR's public short-stock FTP server (`rcdata/build/ownership/ibkr.py`, read at call time; the public `shortstock` login, no account); default `ftp3.interactivebrokers.com` |

`RC_FDA_USER_AGENT` overrides the `User-Agent` header sent to FDA OOPD
(`core/providers/fda_orphan.py`); default `"RevenueChaser research"`.

**yfinance is the exception.** It hardcodes the Yahoo hosts, manages its own
session with cookie+crumb auth, and impersonates a browser TLS fingerprint —
there is no URL to redirect. It must be faked at the Python boundary
(`yf.Ticker` / `yf.screen`); see [`e2e_testing.md`](e2e_testing.md). The market store's
`yahoo` source (MOVE and wheat for Sector Rotation, PR 9) goes through it too, behind the
`rcdata/build/sources/yahoo._history` seam: Yahoo's chart endpoint answers the builder's own
User-Agent with HTTP 429, and its `range=max` with monthly bars (measured 29 Oct 2026).

If you add a provider, give it a base-URL constant read from an env var in the
same shape. A URL inlined at the call site has no seam and cannot be tested.
