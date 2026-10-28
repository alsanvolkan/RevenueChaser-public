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
| Value Chain | `/run/module_value_chain` | `no_fetch` | model-only (reads `edgar.duckdb`) |
| Relatedness | `/run/module_relatedness` | `no_fetch` | model-only (reads the filings in `edgar.duckdb` and the `relatedness_*` profiles in `raw.duckdb`). Fetch also downloads missing filings from SEC and calls DeepSeek off-peak for new profiles; only Fetch writes `relatedness_scores` and `run_registry` (`module_relatedness/engine.py:81-92`, `:449-466`) |
| Sector Rotation | `/run/rotation` | `start`, `end` (optional) | store |
| Asymmetric Growth | `/run/module_asymmetric_growth` | `refresh_universe`, `no_fetch` | model-only |
| Swing Potential | `/run/swing` | `refresh_universe`, `no_fetch` | model-only |
| Fundamentals | `/run/module_fundamentals` | `refresh_universe`, `no_fetch` | model-only |
| SEC 13F | `/run/module_sec_13f` (Fetch: download → ingest → CUSIP map → derive; no UW key needed) · `/run/module_sec_13f-rebuild` (derive only, zero network) · `/run/module_sec_13f-map` (CUSIP map → derive: OpenFIGI only, at most 400 requests, no SEC or UW call; no button, #207) | — | store |
| LEAPS | `/run/module_leaps` | — | model-only |
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
  generic refresh path: Refresh rebuilds the newest stored raw day of its own tree
  (`SPX_GAMMA_DATA_ROOT`) from disk and makes no network call. The VIX family, the
  CFTC COT, the 8-week SPX closes and the 0-1 DTE dollar gamma the pin and squeeze
  tests size by are read from the market store at render time
  (`module_spx_gamma/store_reads.py` over `rcdata.read`: `market_series` and
  `spx_gamma_expiry_strike`), so a Fetch and a Refresh of a day render the same.
- **SPX Gamma's Fetch (`POST /run/spxgamma`) also asks the builder** (PR 3, decision
  A4): the job of an accepted Fetch first queues one `uw/spot_expiry_strike` and
  one `market_series` build (`ops.enqueue_once`, `requested_by` `spx_gamma`; one
  already queued adds nothing; a refused Fetch queues nothing), then runs the
  board's own UW snapshot as before and renders at once with what is
  stored, saying "builder requested at <time>; the builder runs hourly at :07". It
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
  dataset is queued or running, and for an attended dataset (the spine's WRDS ones, PR 5: only the CLI on the
  server queues them); 403 without the panel header.
- **`GET /api/build/queue`**: `{ok, active, finished}`. `active` holds the queued and running requests, oldest
  first; `finished` the last 20 others, newest first.
- **`POST /api/build/schedule/<dataset>/<pause|resume>`**: sets or clears `dataset_schedule.paused_at` /
  `paused_by`; 404 for another action.
- **`GET /api/idle`** (loopback, no header): `{idle, reasons}` from `app/jobs.py` `idle()`. It is false while a
  dashboard job is queued or running, while any `core/job_lock` name is held by any process, or while another
  process's live `.rc-proc-<pid>` marker sits in the store dir or the market dir (F8 A). `stop.ps1` reads it, and
  so does the deploy gate.

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
- **`GET /api/watchlists`**: `{ok, option: [tickers], stock: [tickers]}`, what the board buttons show as pressed.
  The read itself never creates `ops.sqlite`; the read log it counts into is flushed to it once a minute, which
  may, as for every `rcdata.read` call.

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
curl -X POST -H "X-RC-Panel: cli" http://127.0.0.1:8000/run/module_leaps
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
`?refresh=1` (Settings, Co-movement and LEAPS cache their page).

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
| `RC_UW_BASE` | Unusual Whales (`core/providers/unusual_whales.py`, `core/index_registry.py`) |
| `RC_UW_BASE_ROOT` | Unusual Whales for `module_spx_gamma` (no `/api` suffix) |
| `RC_SEC_WWW_BASE` / `RC_SEC_DATA_BASE` | SEC EDGAR + 13F archives; the spine's `sec/company_tickers_exchange` |
| `RC_FORM13F_INDEX_URL` | SEC Form 13F data-set page (zip links are resolved from it) |
| `RC_FRED_BASE` | FRED series (also the market store's FRED source) |
| `RC_CBOE_BASE` | Cboe daily-price index files (`module_earnings_radar/inputs/vol_index.py`, `rcdata/build/sources/cboe.py`) |
| `RC_CFTC_BASE` | CFTC Traders in Financial Futures (also the market store's CFTC source, `rcdata/build/sources/cftc.py`) |
| `RC_CFTC_DISAGG_BASE` | CFTC Disaggregated futures (`rcdata/build/sources/cftc.py`); default: `RC_CFTC_BASE`'s host, dataset `72hh-3qpy` |
| `RC_TREASURY_BASE` | US Treasury daily par yield curve CSV (`rcdata/build/sources/treasury.py`) |
| `RC_FED_BASE` | Federal Reserve site, the FOMC calendar page (`rcdata/build/sources/fed.py`) |
| `RC_BLS_BASE` | BLS site, the release calendar `.ics` (`rcdata/build/sources/bls.py`) |
| `RC_FDA_OOPD_BASE` | FDA Office of Orphan Products Development (OOPD) search |
| `RC_DEEPSEEK_BASE` | DeepSeek chat API (`core/providers/deepseek.py`; read at call time) |
| `RC_GROK_BASE` | SuperGrok chat API, the Relatedness Grok writer (`core/providers/grok.py`, #388; read at call time). Default the Grok CLI chat proxy `https://cli-chat-proxy.grok.com/v1`. Only https on `grok.com` / `*.grok.com` or a loopback mock is accepted, so the subscription token never goes elsewhere (`api.x.ai`, the metered developer API, is refused); the model is `RC_GROK_MODEL` |
| `RC_OPENFIGI_BASE` | OpenFIGI mapping API (identity refresh; the 13F CUSIP map, `core/providers/openfigi.py`, read at call time; the spine's `openfigi/map`) |
| `RC_NASDAQTRADER_BASE` | Nasdaq Trader symbol directory (identity refresh) |
| `RC_FDIC_BASE` | FDIC institution list (identity refresh) |
| `RC_WIKIPEDIA_BASE` | Wikipedia S&P 1500 constituent lists (identity refresh; a sector source for the list's own names, #272) |
| `RC_SPDR_BASE` | SPDR index-ETF holdings files (identity refresh) |
| `RC_SSGA_BASE` | SSGA daily ETF holdings workbooks: SPY, MDY, SPSM, the spine's index membership (`rcdata/build/spine/funds.py`, read at call time) |
| `RC_ISHARES_BASE` | iShares holdings CSVs: IVV, IJH, IJR (`rcdata/build/spine/funds.py`) |
| `RC_VANGUARD_BASE` | Vanguard portfolio-holding API: VOO, IVOO, VIOO (`rcdata/build/spine/funds.py`) |
| `RC_NASDAQ_API_BASE` | Nasdaq's list API, the Nasdaq-100 members (`rcdata/build/spine/funds.py`) |
| `RC_INVESCO_API_BASE` | Invesco's fund holdings API: QQQ, the second Nasdaq-100 file (`rcdata/build/spine/funds.py`) |

`RC_FDA_USER_AGENT` overrides the `User-Agent` header sent to FDA OOPD
(`core/providers/fda_orphan.py`); default `"RevenueChaser research"`.

**yfinance is the exception.** It hardcodes the Yahoo hosts, manages its own
session with cookie+crumb auth, and impersonates a browser TLS fingerprint —
there is no URL to redirect. It must be faked at the Python boundary
(`yf.Ticker` / `yf.screen`); see [`e2e_testing.md`](e2e_testing.md).

If you add a provider, give it a base-URL constant read from an env var in the
same shape. A URL inlined at the call site has no seam and cannot be tested.
