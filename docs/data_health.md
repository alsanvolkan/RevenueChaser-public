# Data health — the store inventory and the Settings / Data health board

This is the **built contract**, not a plan. It describes what the board does today, what each
number on it means, and what it refuses to claim. Where the board shows words instead of a number,
the words are written down here and in the code that renders them, never made up at read time.

Companions: [`data_architecture.md`](data_architecture.md) (where every table lives),
[`data_retrieval.md`](data_retrieval.md) (Fetch versus Refresh across the app),
[`dashboard_ui.md`](dashboard_ui.md) (the equity filter chrome, which this tab does **not** use).

The code this document is the contract for:

| File | What it owns |
|---|---|
| `core/datasets.py` | the dataset registry: one record per dataset, and the claim rule over `rawstore.MANIFEST` |
| `core/datahealth.py` | the `Health` record: typed completeness, with every sentence derived from the counts |
| `module_settings/inventory.py` | the store read: row counts, newest dates, holes, freshness states, the cards' facts |
| `module_settings/report.py` | the page: summary tiles, filters, the table, the detail rows, the cards |
| `module_settings/boards.py` | the Board inputs cards, built from each board's own last rendered payload |

Sections 2 and 3 describe the board as it stands on `main` plus the three lanes still in review:
the named freshness states and the gap wording, the Board inputs rows, the identity coverage card
and the data-flow diagram, and the read-only expired-backup dry run.

---

## 1. The store inventory contract

### 1.1 One registry, one owner per table

`core/datasets.py` holds `REGISTRY`, a tuple of `DatasetSpec` records. One record is one dataset:
its key, the words shown for it, the store tables it owns, the store file it lives in, its source,
its writer, and the rule by which it may claim freshness.

The claim rule over `core.rawstore.MANIFEST`:

- a change-history table (`MANIFEST` `changes_of`) is listed on the dataset that owns its base table;
- every `history.duckdb` table — `result_runs` and each result-history mirror, eighteen in all — is
  listed on `result_history`. `earnings_history` is not one of them: it is a raw change-history table;
- every other `MANIFEST` table is listed on exactly one dataset;
- `UNCLAIMED_ALLOWLIST` is empty, so a new `MANIFEST` table fails `core/tests/test_datasets.py`
  until a dataset owns it.

A `DatasetSpec` validates itself on construction. A writer of `None` needs a non-empty
`writer_note` saying who writes the table instead. A freshness pair must name one of the dataset's
own tables. A freshness of `from_run_log` must carry the caveat that names issues #82 and #83,
because the run log is not yet trustworthy. `core/datasets.py` reads no store and opens no network.

### 1.2 What freshness a dataset may claim

A table the reader ages row by row carries its own timestamp, and the registry names that
`(table, column)` pair. A table the board only reports on takes its freshness from the run that
wrote it. The registry has four answers and no fifth:

| Registry value | Meaning |
|---|---|
| a `(table, column)` pair, or several | read the newest value of that column; the newest of several pairs wins |
| `from_run_log` | a worked-out table: its freshness is the newest finished run of its writer |
| `no_claim` | the store's own bookkeeping, or a retired table: no freshness is claimed at all |
| `None` | this dataset carries no date of its own; the date lives in a related table |

`_BOOKKEEPING` is the five tables that are store bookkeeping rather than data a board ages:
`store_meta`, `migration_manifest`, `run_registry`, `job_runs`, `store_schema`. `_RETIRED` is the
four tables the `MANIFEST` flags `retired`.

### 1.3 Typed completeness: the `Health` record

`core/datahealth.py` holds `Health`, a frozen record of `covered`, `expected`, `newest`,
`expected_as_of`, an optional `source_share` and the problems the producing module recorded. It is
a pure module: no store, no network. A free-text note cannot be checked, so `Health` carries the
counts and every sentence shown is derived from them.

The rules that keep an unmeasurable dataset from looking like an empty one:

- `expected is None` and `expected == 0` both give a coverage of `None`, which renders as **n/a**
  and never as 0 per cent. A percentage of nothing is not a number.
- `covered` of `None` also gives `None`.
- a coverage above 1 is **not** clamped. `consistency_problems()` names `covered N exceeds expected M`
  with both counts, and names a `newest` date later than `expected_as_of` with both dates.
- `coverage_text` reads `n/a`, or `65.0% (4,999 of 7,689)`. `freshness_text` reads `unknown` when
  there is no newest date, else the whole calendar days from it to `expected_as_of`, or to today when
  no as-of was given. An as-of that cannot be read does **not** fall back to today; it reads `unknown`.

Seven datasets already have a summary in their own module. Their `coverage` is a callable that
imports that module and returns its `Health`, called only when a reader asks for the row, so
importing the registry opens no store. It takes an optional `now`, the page's own clock, passed on
to a module whose `health` takes one, so the row and its coverage ask one question (#150). `memo_scope()` makes the second ask within one page
read free, without a cache that outlives the read.

### 1.4 The read

`module_settings.inventory.collect(now)` is the whole payload. It opens each store file once
(`rawstore.session()`), opens one `memo_scope()`, builds every row, and records how long it took in
`payload["timing"]["collect_seconds"]`. It makes **zero network calls**.

Honesty rules the read keeps:

- a missing table, an empty table and a column that does not exist each give `None` or the dash,
  never a guessed number;
- a query that **fails** is caught, recorded on the row's own table, and reported as its own state.
  A broken read is never rendered like a table that holds no rows;
- `unclaimed_tables()` is the backstop for a `MANIFEST` table no dataset claims. With nothing
  unclaimed it asks the store nothing at all;
- rows are sorted worst coverage first (`sort_key`); a row whose coverage has no fraction — `n/a`,
  `unknown`, `error` — follows the measured ones, and ties break by label.

The payload keys: `rows`, `tickers`, `boards`, `identity`, `automations`, `backups`,
`other_tables`, `errors`, `generated_at`, `provider_health`, `timing`.

---

## 2. The Settings / Data health board

The tab is `/view/module_settings`, labelled **Settings / Data health**. Its Refresh job is
`settings_refresh` (`app/runners.py` `run_settings`). It is an ops inventory of the local `.store/`,
not an equity board.

| | |
|---|---|
| Equity filter panel, Theme cascade | **no** — this tab has its own filters over the inventory table (see 2.2) |
| Ticker combobox | **one**, the filter kit's `RCCombobox`, which opens the stored-data drill |
| Help block | yes, and it states Fetch versus Refresh for this tab |
| Refresh | re-reads `.store/` with zero network: instant and always safe |
| Fetch | per dataset, one existing asynchronous job; it hits a provider and can take minutes |

Same tokens as the rest of the app (`--bg #05111F`, `--accent #0EA5E9`). There is deliberately no
"fetch everything" button: the locks, the provider quotas and the hours make it a trap.

### 2.0 The "New data system" section (PR 2)

The first section of the tab, above the old inventory (which stays until PR 21, with its drill, flow diagram and
board-input cards). It shows the market store that the builder (`python -m RevenueChaser.rcdata.build`) writes. The
contract below binds the page; decisions D1, D6, D9, F6, F7 A, F8 A, F10 B and the five Settings decisions of
27 Oct 2026 are the source.

**Where the data comes from.** `module_settings/newdata.py` `collect()`, read when the page is rendered (Refresh),
with no network:

- `ops.sqlite` through the read helpers of `rcdata/ops.py` (D6). They never create the file or a table: no file
  yet, or a table an older file lacks, reads as no rows.
- Row counts, the newest date and the last swap from the live serving file. **The handle rule:** every read opens
  its own read-only handle with `rcdata.serving.open_read(kind)`, the path resolved at that moment, and closes it
  before `collect()` returns. No connection is cached, at module level or across requests, because DuckDB keeps
  one database instance per path in a process, and a kept handle would go on serving the build it opened after
  the builder swapped in a newer one. The `/api/build/*` routes open no DuckDB file at all.
- Backfill progress from the Parquet folder listing, backup ages from the market backup's `.manifest.json`.

**One card per dataset**, worst status first: `stale`, `never` (no check yet), `late`, `unchanged`, `fresh`.

| Status | Rule |
|---|---|
| fresh | within its cycle, and the last run changed rows |
| unchanged | within its cycle, and the last run changed nothing |
| late | one cycle missed, or coverage under its target |
| stale | two cycles missed, or the last check failed |
| never | no check row yet |

A daily dataset's cycle is one New York session (`core/calendar.py`), counted from its newest `source_as_of`. A
weekly dataset is in its cycle for 8 days after its last check, a monthly one for 35. A `skipped` check (paused,
or a provider at its day cap) does not reset the clock: the status comes from the newest check that is not
skipped. The cadence is `dataset_schedule.cadence` when set, else the dataset's default (both datasets of PR 1:
daily). Coverage targets are `newdata.COVERAGE_TARGET` (SPX expiry-strike 95%, market series none; the options
data of G8: the option list and the earnings options 95%, the IBKR year none). These are
the proposed alarm limits as set (decision 1), to be checked again after two weeks of real data.

Inside each card: the dataset's checks (decision 2: last run, source as of, coverage, rows removed and added,
calls, next check due, open `build_issues`, with the newest ten issues listed), backfill progress (SPX: sessions
with a partition out of the sessions expected since 16 Jan 2025, the missing ones, the last partition; market
series: partitions per source and year), the WRDS difference slot (decision 3: "no WRDS run yet" until PR 7 and
PR 12 bring the data) and the schedule state.

**The spine (PR 5).** Seven more dataset cards, one per spine dataset (`docs/data_architecture.md`, "The identity
spine"), each with its own cadence (the SEC file and OpenFIGI weekly, WRDS monthly, the fund files and
`spine/security` daily); `spine/security`'s coverage is the share of Tier A with a CUSIP, target 98%. The three WRDS
cards are attended: they show the CLI command and Pause / Resume, and no Fetch or Backfill (a person logs in and
confirms Duo on the server).

**The identity card** (D9) is the first card of the section. It replaces the hand-built Identity coverage card of
the old page (3.6), which is no longer shown. From the same read-only spine handle as the cards: the SEC file's
rows, then the listed ones, then Tier A, then with a CUSIP, with a FIGI, with a GICS classification and in the
S&P 500, 400, 600 or Nasdaq-100, each of those four out of Tier A; the drops (Tier B, C, D); the members per index;
and the open `build_issues` of `spine/security` by kind with the newest ten. Before the first spine build it says
so instead of drawing zeros. Its first row, "Boards read", says which universe the boards read and how that was
chosen: the spine (the default, `RC_UNIVERSE_SOURCE` unset, or the variable set to `spine`), the old store (the
variable set to anything else, `store` by convention), or the old store because the spine was asked for but is
missing or its last build failed (in amber, with the reason).

**Page-level cards:** the queue (queued and running requests, then the last 20 finished), spend (F6: calls per
provider for the New York day and the week from Monday, from `provider_calls_daily`, against the day caps of
`rcdata/caps.py`, and UW's newest `x-uw-*` sample from `uw_remaining`: its daily count, what is left of our own cap
(computed) and the per-minute window), market-store backups (the newest
`ops` and `market` copies in the manifest, the 35-day raw copy dated by its newest copied file, the portable SSD
from a `dataset_check` row of dataset `backup/portable` or "not recorded"; each warns past 35 days or when
missing), the raw option retention warning (on until `RC_RAW_RETENTION_DAYS` or a `dataset_schedule` row
`retention_days`, with the days as its cadence, says how long raw option files are kept; zero or a value that is
not a whole number of days counts as unset, and the builder's retention hook reads the same rule,
`rcdata/retention.py`), retirement (reads of
each old table the new data replaces, from `read_log` over 14 days, with the registry task that still writes it),
and the two watch-list cards (PR 4, below). A "verified running" line on top: the builder's last run
(`job_runs2`), the last swap per kind and the `ops_schema` version; under it the watch-list counts.

**The two watch-list cards (PR 4, D2).** The option list (what the options build covers besides SPX) and the stock
list (what LEAPS pins and scans and Earnings treats as watched), read through `rcdata.read.watchlists()`. Each name is
a stacked row: ticker, added on (UTC), by (the board whose button added it), the note when one is set, and a
**Remove** that posts `POST /api/watchlist/<list>/remove/<ticker>` (it never adds, so a stale page cannot put back a
name removed elsewhere). The option card also says which names the next options build takes: SPX and every name on
the option list, until that build exists (build-plan step 11). Names are added from the boards: every board that
renders tickers carries the **Opt** / **Stk** pair (`docs/dashboard_ui.md` §2c). A note is not edited on the page
yet.

**The buttons are requests, nothing else (F7 A, F10 B).**

| Button | What it does |
|---|---|
| Fetch | `POST /api/build/request/<dataset>`: one queued request; the builder runs it at its next pass |
| Backfill | the same with `backfill_from` (a date field). The page first shows the expected calls (SPX: two per missing session from that date, `CALLS_PER_SESSION_ESTIMATE`: an ordinary session is one call and one that fills UW's page of 500 two or three, and the sessions a first backfill leaves missing are mostly the paged ones) and asks for a second click when they are over 1,000, or when the dataset has no estimate |
| Pause / Resume | `POST /api/build/schedule/<dataset>/pause` or `/resume`; the builder skips a paused dataset with a `skipped: paused` check row and sends no call |

**What the page never does:** it writes no data file and no `.store/` table, opens no serving file read-write,
sends no provider call (Refresh makes none, and the e2e tier counts it), and has no drop, restore, edit or compact
control. A request that is already queued or running answers 409.

**Look.** The shell's theme tokens (`THEME_CSS`, `THEME_JS` of `core/reporting_common.py`), scoped to
`#newdata` so the old page keeps its own palette until it goes; light and dark. One column of full-width cards,
label-value rows instead of tables, 44 px targets, bars at the card's width, nothing that scrolls sideways at
390 px. Every string reaches the page through `core/html_safe.py` or `rcEsc`; handlers read `data-*`.

### 2.1 The summary tiles

Five tiles above the table, counted from the rows already collected, so no tile costs a second
query:

| Tile | What it counts |
|---|---|
| Datasets | every dataset in the table below |
| Needs attention | datasets whose coverage is below 70 per cent, or whose query failed |
| Empty | datasets that store nothing yet |
| No time column | datasets whose last data date is a dash |
| Refresh took | the seconds this page spent reading the store, or "not measured" |

### 2.2 The filter bar

The board **does** have a filter panel of its own. Every axis is a fact the row already carries, so
no filter can change what a number means:

- a find box over the dataset names and the store table names;
- **Store file** chips, one per store file present: `raw`, `edgar`, `prices`, `history`;
- **Source** chips, one per source present, spelled out in words;
- **State** chips: *needs attention* (coverage under 70 per cent, or a failed query), *no time column*
  (no last data date, so no freshness can be read from this dataset), *no denominator* (the coverage has
  no definable population, which is **not** nought per cent).

No filter is on when the page loads. The count line always says how many of the total are shown, and
**Clear filters** resets them, so a filter can never quietly hide a broken row and make the board
look fixed. A chip group with no matching row renders nothing rather than a dead chip.

### 2.3 The table

Seven columns: **Dataset**, **Source**, **Coverage**, **Freshness**, **Last data date**, **Rows**,
**Holes**. The Source heading carries a **Group** toggle that groups the rows by source and restores
the rendered order when pressed again.

- **Dataset** is the dataset's words, as a button that opens its detail row (reachable by keyboard),
  then its store-file pill, then its gap tag, then a warning mark when one of its tables failed a query.
  One line under the table names every store-file pill, so a colour is explained once.
- **Source** is the provider the registry names, written out: Unusual Whales, Yahoo Finance, SEC EDGAR
  filings, FINRA and the Commodity Futures Trading Commission, Cboe (Chicago Board Options Exchange),
  FRED (Federal Reserve Economic Data), OpenFIGI, worked out by us from other datasets, entered by
  hand, the store's own bookkeeping, or "more than one provider, or not known".
  The machine value stays in the row's data attribute for the filters; only the visible text is words.
  That list of provider names is the page's only one: this column, the Board inputs rows (3.5) and the
  data-flow diagram (3.7) all read it, so one provider cannot be called three things on one page. A
  source the list does not know is named as unnamed, never under a wrong name.
- **Coverage** is a fill bar and the counts: green at 95 per cent and above, amber from 70, red below.
  The drawn width is clamped to 100 per cent while the label keeps the real counts, so a coverage above
  100 per cent draws a full bar and still says so. A record with no fraction draws **no bar** and reads
  muted — never nought per cent. A failed coverage query keeps a red **error** label.
- **Freshness** is what freshness the registry lets this dataset claim, in words (2.4).
- **Last data date** is the newest date this dataset's own table carries, or the words
  *no date of its own*. The reason is the Freshness cell's business, one column over.
- **Rows** and **Holes** are counts, with the hole's own label beside it ("null market cap",
  "no filing, excluding recorded misses", and so on). A hole count of `None` is a dash: unknown, not nought.

A row whose key the registry does not know keeps everything the read gave it and shows a dash in
Source, Coverage and Freshness. Nothing is invented for it.

### 2.4 The freshness answers, in words

The Freshness column printed a bare dash for four different situations, so "no time column", "the
query failed" and "this table holds no rows" looked identical. Each now carries its own words:

| State | What the cell reads |
|---|---|
| a date | the newest date, for example `2026-10-20` |
| no time column | `no time column - see the provenance rows` |
| taken from the run log | `taken from the run log`, and the date when the run log has one |
| retired | `retired - no freshness claim made` |
| store bookkeeping | `store bookkeeping - no freshness claim` |
| empty | `no date stored yet` |
| query failed | `query failed - this is not an empty dataset`, in colour |

The run log is read at most once per page, and only when a row on that page dates itself by it: one
grouped query over `job_runs` by `registry_name`, which is the same name a `from_run_log` dataset
carries as its writer. An unreadable run log is recorded and reads as no run; no date is invented.
A dataset whose `MANIFEST` tables are flagged retired reads as retired rather than as stale, read
off that flag, so nothing has to be listed twice.

### 2.5 The detail row: what is missing, and how it improves

Each dataset's detail row is closed until its name is pressed. It states, in this order: **What is
missing**, **How it improves**, **Store table(s)**, **Writer** (with the note a dataset without a
single writer carries), **Action** (its Fetch button, when it has one), the registry's **Freshness
note** when there is one, **Notes**, and any query failure on this row.

The number is measured and the interpretation is written, and neither is ever the other. No written
sentence states a count, and a dataset with no written interpretation draws no tag rather than a
"Complete" it cannot prove.

**What is missing** comes from the coverage record alone: `nothing, by design` for a retired
dataset; `the coverage query failed`; `the coverage could not be read` when the summary returned no
`Health`; `no population is defined` when there is no denominator; otherwise `N of M names have no
row`, with N at nought when coverage is full.

**How it improves** is the dataset's gap kind in words plus its next step. The six kinds, and the
tag each draws beside the dataset name:

| Kind | Words on the tag | Meaning |
|---|---|---|
| `fixable` | Can be fixed now | we can close it with work we already know how to do |
| `in_build` | Being built | something already being built closes it |
| `needs_decision` | Waiting for a decision from you | it waits on a choice, not on work |
| `ceiling` | Cannot be improved | the source cannot give us the rest on the route we use |
| `self_heals` | Fills in by itself over time | it closes as the normal nightly runs happen |
| `none` | Complete | there is no hole |

`fixable`, `in_build`, `needs_decision` and `ceiling` each require a written next step, enforced when
the record is constructed. A `ceiling`'s next step **is** its reason, so the two read as one
sentence: "Cannot be improved: the gold, silver and oil funds hold metal or futures rather than
company shares, so the holdings providers return no list for them." Colour is never the only signal:
the tag always carries its words.

### 2.6 Other store tables

A section below the table for every `MANIFEST` table no dataset claims, with its store file, its
row count and its newest date — nothing else, because the registry cannot speak for them. Newest is
read only from a known time column; a table with none reads `no time column` rather than a guessed
date. With nothing unclaimed the section renders **nothing at all**: no heading and no empty table.
It is normally empty, and `core/tests/test_datasets.py` keeps it so.

### 2.7 The IBKR gateway shortcut

Above the help block, one link that opens the IBKR gateway's own login screen in a new tab. Whether
that login is currently good is a different card, 3.2. The
address is **not** in this repository: it is `RC_IBKR_LOGIN_URL`, read on every payload build, so an
unset variable renders no card at all and no host is pinned in the code. Only an absolute `http` or
`https` address with a host is accepted; anything else (a `javascript:` or `data:` value, a bare
path, a scheme with no host) counts as unset, so a mistyped variable cannot become a live link. The
page writes the link and nothing more: it makes no request to that address, and the gateway's own
login is what asks for credentials.

### 2.8 Fetch versus Refresh on this tab

- **Refresh** recomputes the inventory from `.store/` with zero network. The board is registered in
  `_STORE_REFRESHABLE` and `STORE_BACKED`.
- **Fetch** enqueues that one dataset's job and polls `/api/job/<id>`. A sync that changed nothing
  still writes the artifact and raises `jobs.JobMessage` ("0 updated, 3278 current").
- Every write through the dashboard needs the panel header (`X-RC-Panel`), and
  `POST /api/automations/<name>/<action>` needs exactly `X-RC-Panel: automations`. Without it the
  dashboard answers `403 panel header required` and starts nothing.
- Per-ticker drill `GET /api/settings/ticker/<t>` — store-only, 404 on an unknown ticker (no row in
  any dataset the drill reads, with every read answering), 500 `store read failed` when the drill
  itself raises. A read that fails inside the drill is recorded, never read as "no row" (#143): the
  answer is a 200 with `errors` (table and message, printed under "Could not read"), the row reads
  "could not read", and a board that reads the table says "unknown — could not read …" (state
  `unreadable`) unless a readable table it needs already has no row, which stays "no — missing …".
  A store file whose table list cannot be read counts as a failed read for every table in it. The
  drill also has the shared ticker combobox, and a null market cap
  rendered as "no cap stored (outside the $300M universe or not yet resolved)". It shows **stored**
  data only: it asks neither SEC nor Yahoo. Price coverage in the drill is the daily bar count and
  newest session from `price_bars_daily`; `swing_ohlc` appears as the legacy cache and is not the
  price of record.

---

## 3. The sections below the table

### 3.1 Identity health

The **Identity spine** row carries the **Fetch identity** button, which posts `POST /run/identity`
(optional positive `limit`, long lane, no Unusual Whales requirement). It is a **live universe
writer**: a run that ends `ok` or `partial` applies the master's sector and industry, plus the next
earnings date, to `equity_universe`, so it changes sectors on every board. The help text and the card
say so. Each run writes an `identity_runs` row with per-source status, rows, fetched count, seconds
and error. The row's holes are the master tickers with no sector source at a market cap of $300M or
more; its note is `last run <status>` or `no identity run yet`.

The card itself (`<section id="identity">`) is built from the store only; a missing table is an empty
section, not an error. With no run and no master rows it reads
`No identity run yet. Use Fetch identity in the table above.` Otherwise it shows the last run, the
SPDR check line, the security-type mix at and above the $300M floor with excluded types tagged, the
sector-source mix (the S&P 1500 list, a provider, the SIC table, the type rule, unclassified, and
`other` only when non-nought), the conflicts by kind with the top fifty by market cap, and the
newest fetch per cached source with the number of universe tickers past each source's window.

The SPDR sector check compares each S&P 500 member's sector in the master against the sector of the
Select Sector fund that holds it, named the Morningstar way. It is a **check, never a value**:
nothing in `security_master` reads it, its rows are not cached, it is all or nothing, it is not
counted in "sources ok n of m", and a failed check writes no conflicts and leaves the run `ok`.

Each run also refreshes the oldest quarter of the cached SEC submissions rows that are at least
seven days old, so a universe fetched in one day does not expire together.

### 3.2 IBKR Gateway login state

The pair of the gateway shortcut in 2.7: that one is the link that logs you in, this one says whether
that login is still good. Answers "is the gateway still logged in, and if not, since when?". Its
session expires and is logged in again by hand, so the state alone is not enough: the card also says
when it dropped and when it was last up.

The two share a page, so they own separate id namespaces — the shortcut `ibkr-login` and
`ibkr-login-*`, this card `ibkr-state*`. One id in two places is invalid HTML, and an assertion on it
would pass against the wrong card; `test_the_two_ibkr_cards_share_a_page_without_sharing_an_id` keeps
them apart. The shortcut keeps its place above the help block because it is one line; this card is
several, so it sits with the other availability cards.

**The card reads a record, not the gateway.** `core/ibkr_login.py` keeps the readings in
`.cache/ibkr_login/state.json` (written `tmp` + `replace`), and the card reads that file and nothing
else. A probe on every Refresh would be a side effect this page must not have, and on a dead gateway
a timeout on every look. So the card's freshness is the job's, and a reading older than 90 minutes is
called out — "no check has run" means the schedule is not running, which is itself the useful fact.

**The probe is market-data safe.** `RC-IBKR-Login-Check` opens a TCP connection to the configured
host and port, sends the TWS API version handshake, and reads the reply. That is all: no account,
position, order or execution is ever asked for, and no request is sent past the handshake. No IBKR
library is a dependency; the handshake is a few lines of `socket` and `struct`, its shape read off
`ib_async` 2.1.0. Three readings, and the card's three states: the connection refused or timed out
(**Login needed** — the gateway is not listening), the port open but the handshake silent (**Port
open, the API is not answering**), and the handshake answered (**Logged in**, with the server version
as the evidence). A reply that arrives but cannot be parsed still counts as answered with the version
unknown: a framing mistake must never read as a false "down".

**No store table, so no schema bump and no live-change gate.** The dashboard process holds the single
DuckDB writer, and a probe meant to run every few minutes must never queue behind it or hold it. The
file survives a restart, which is what is required; it does not survive a `.cache/` wipe, and the card
then reads "no check has run yet" rather than inventing a history. An unreadable or malformed record
reads **unknown**, never "down".

**A weekly login expiry is not a failure.** The job exits 0 whatever it finds, so an expired session
does not turn its scheduler row red for something nobody broke. The state is the output.

**Off until it is switched on.** `RC_IBKR_LOGIN_CHECK` is empty by default: nothing is contacted,
nothing is recorded, and the card says so and names the variable. `RC_IBKR_HOST` and `RC_IBKR_PORT`
then default to `127.0.0.1` and `4001` — loopback, because the gateway's API port should not be
reachable off its own machine.

### 3.3 Backups

Built from the backup job's own records in the local backup folder: both folder sizes, and the last
read-only self-check of the local folder. The off-machine (Google Drive) copy is **never** read,
walked or listed — walking it took about twenty minutes. The card shows the local folder in full, the
off-machine location the job's own setting names, how long backups are kept, and the integrity
self-check in words.

The card also lists **what the retention rule marks expired**, from one read-only listing of the
local folder. This is a dry run: nothing is deleted. The totals read "unknown" with the reason when
the folder could not be read, so an unreadable folder never looks like an empty one. The
**Delete expired backups** control is disabled in the markup itself, and the line beside it says
arming a delete is a separate change that needs a typed confirmation. Nothing on this page deletes
or moves a backup file.

The retention rule has one copy in the code, read by both the nightly prune and this dry run: the
newest five raw snapshots, two EDGAR, two document archives, five history, two prices,
and twelve weekly raw copies. An archive prefix the job no longer writes (`discord_media_*.zip`,
retired by the mirror) has its own line that keeps one copy (#153): the nightly prune never touches
it, so without that line its old archives would never be listed. The newest copy of every list is
**never** offered as expired, whatever a keep count computes, because a restore reads the newest snapshot.

### 3.4 Automations panel

`core/automations.py` is the one registry of the scheduled tasks RevenueChaser owns, and the one
installer. Rows: `RC-Store-Backup` (daily 04:00, `scripts.backup_store --keep 5`),
`RC-Relatedness-OffPeak` (12:30, `--run --cap 8`), `RC-ValueChain-Sync` (13:00, `--sync-filings`),
`RC-Daily-Bars` (22:35, `scripts.daily_bars --nightly --sec-files`), `RC-Screeners-Daily` (weekdays
23:00, after `RC-Daily-Bars`, `app.universe_cli`), `RC-Forecast-Outcomes` (daily 23:45, after
`RC-Daily-Bars`, `scripts.forecast_outcomes --nightly`), `RC-Forecast-Baselines` (daily 23:50, after
`RC-Forecast-Outcomes`, `scripts.forecast_baselines --nightly`), `RC-Index-Lists` (Saturday 10:00,
`scripts.index_lists --weekly`) and `RC-Earnings-Inputs` (Tuesday to Saturday 01:30, after
`RC-Daily-Bars`, `scripts.nightly_inputs earnings`). Each entry carries its schedule, the tables it
writes, whether it is off-peak gated, a log path under `.cache/logs/` and its task settings (named
for Task Scheduler; the Linux backend maps the ones systemd has).

- **Nothing installs itself.** `default_install` only marks a row. Install, Enable, Disable, Remove
  and Run now are buttons on the panel, and Install, Remove and Run now ask for confirmation. The two
  PowerShell files are thin callers of the same installer. Nothing installs on import, on dashboard
  start, on Refresh, on a GET or in a test.
- **Live state is polled, not baked in.** The board is a cached artifact, so the panel reads
  `GET /api/automations` on load, after an action and while a row runs.
  `POST /api/automations/<name>/<action>` takes a registry name and no other argument, and needs the
  header `X-RC-Panel: automations` exactly. Refresh of this board never calls the scheduler.
- **Row state.** Not installed, disabled, enabled or running; the last result mapped (nought is ok,
  "not run yet" is neutral, running is running, anything else is red); next run, missed runs, and the
  age of the last success read from the task's log.
- **Exit codes.** A task's command exits non-zero when it locked out or failed. Only the Relatedness
  off-peak deferral exits nought. Exit 3 from a nightly writer means another run of that writer held
  its lock: nothing was written, and the next run picks it up.
- **The nightly chain.** An entry with `after=<registry name>` is a chained step: still its own task
  with its own daily time, which is its not-before time. When it starts, including on a catch-up or a
  Run now, it reads the predecessor's log and waits, up to
  `chain_wait_minutes` (default 120), for the predecessor's run of tonight: the newest run that
  started at or after the predecessor's last scheduled time. Exit nought releases the step. Any other
  exit, a step blocked in turn, or no such run in time blocks it: its command does not run, the log
  says `chain: blocked: <why>` and the task exits 5, so the row is red with the reason. A step must
  name an earlier registry entry with no cycles, fall nought to under twelve hours after its
  predecessor on the clock, follow a predecessor that runs daily, and wait a positive time shorter
  than its own time limit. `check_chain` runs on import.
- **Nightly inputs steps.** An entry whose command is `scripts.nightly_inputs <step>` with the three
  call caps runs the sweeps of one inputs step. The checker refuses such an entry unless it runs after
  `RC-Daily-Bars`, names a known step, lists its tables and states all three caps. Each run resolves
  one New York session, and a run inside the United States session runs nothing and exits nought. Each
  sweep runs on its own: one that raises is failed, the others still run, and the task exits 1 so the
  row is red. A cap is per night and per provider, shared by every sweep; a request past it is refused
  before it is sent, and a second run of the same session starts from what the first spent.
- **SEC exchange and fund files.** `RC-Daily-Bars` runs `--sec-files`: before the bars, on a New York
  weekday, it asks the two SEC company-ticker files once each with no retry, at most two SEC requests
  a night, and appends their changes as change history. A file under its floor writes nothing. A
  failure is never a different exit code, so the steps chained on `RC-Daily-Bars` still run.
- **Run log.** A task that writes tables leaves one `job_runs` row per run, `running` while its
  command runs and `done` or `error` after it; what the command writes carries that row's run id. The
  backup records none. The **Runs** tab (`/view/runs`) is the reader of that table, dashboard runs and
  scheduled ones together.
- **Two scheduler backends.** On Windows the panel drives Task Scheduler through PowerShell. On Linux
  it drives **systemd user timers** (#158), and only when `RC_SCHEDULER=systemd-user` is set: by
  default Linux reads every row as unavailable, as before, so a server whose jobs already run as
  system timers is never offered a second schedule. Whether a server opts in is the owner's decision.
  Install writes `<name>.service` (`Type=oneshot`, the same `automations run <name>` command,
  `TimeoutStartSec` from the time limit) and `<name>.timer` (`OnCalendar=` from the entry's time and
  days in local time, `Persistent=` from "catches up a missed run") under
  `~/.config/systemd/user/`, then enables the timer; a systemctl failure deletes both files again.
  The service carries an allowlist of variables, never a copy of the environment (unit files are
  world-readable and `.env` puts the keys there): the three data roots as the dashboard resolved them,
  so a job writes the store and logs the panel reads, and `SPX_GAMMA_DATA_ROOT`, `RAW_STORE_BACKUP_*`
  and `TZ` when set. Not mapped: "wakes the PC" (a user timer cannot arm a wake alarm, and asking for
  one leaves it failed) and the restart counts (`Restart=on-failure` would re-run a chained step's
  exit 5 and a busy lock's exit 3, and could repeat provider calls). Enable and Disable switch the
  timer, Run now starts the service without waiting, Remove disables the timer and deletes both files
  even when systemctl cannot be reached. The row reads the timer's next run and the service's last
  exit code from one `systemctl show`; a failed timer is an error row; after the user's systemd
  instance restarted the exit code is gone and the result reads "—", while the last success still
  comes from the log. A user timer runs only while that user's systemd instance runs, so a server
  that runs logged off needs `loginctl enable-linger <user>` once, by hand.
- **Task names for scripts.** `python -m RevenueChaser.core.automations names` prints the registry
  names one per line and never asks the scheduler, so a script that freezes or lists the tasks reads
  them from the registry instead of keeping its own list (#164).
- **Tests** use a fake scheduler; the end-to-end harness installs it, so no test reaches the real
  Windows Task Scheduler or systemd. The Linux backend is tested through a fake `systemctl` and a
  temporary unit folder.

### 3.5 Board inputs

A Board inputs card is built from that board's own last rendered payload in `.cache` and never
recomputes the board, so the card and the board cannot disagree. The funnel shown is the one the
board itself emitted. Step kinds are `start`, `drop` (by design, or fixable with a Fetch button),
`note` and `result`, with `start - sum(drops) == result`. A **note** is shown but never counted: it
says something about names still in the funnel, sits between the start and the result, and may carry
a link to where its cause is shown. A note renders with a warning mark, a positive count and that
link, never a button: long store-writing jobs start only from Settings. A board whose payload emits
no funnel never asks for a Refresh to grow one, and a missing artifact reads as not rendered.

The classification universe drops the excluded identity-master types at every market cap. Co-movement
states that as three by-design drops after *No market cap* — funds and exchange-traded funds, blank
cheque companies, and preferred shares, warrants and units — and then, when kept names have no
established type, the note *Unknown security type — kept* linking to Settings → Identity. A funnel
persisted before that change has no type rows and no note. The counts come from `universe_counts()`,
and the types are read with a separate query, never a join: a missing table answers with no rows, so
a join would silently empty the universe.

**What this board reads.** Each card lists one line per store table the board reads, in the same
language as the diagram below: the provider, then the dataset that owns the table, then the store
file pill and that dataset's coverage bar. The provider, the dataset's words and the store file come
from the registry index the table above already read, and the coverage cell is the one the dataset's
own table row shows, so the card and the table cannot disagree. The whole card is rendered from the
payload it was given and asks the store nothing.

A store table the registry does not place is said in words rather than left out: *not claimed by any
dataset*, or *claimed by more than one dataset*. Those are different facts, and the line says which.

### 3.6 Identity coverage

**Not shown since PR 5:** the identity card of the "New data system" section (2.0) replaced it (D9). The payload
still carries the section until the old inventory goes. What follows describes it as it was.

A card directly under the inventory table: how far the stored identity data reaches, name by name.
Every number comes from the store read, and there are two ways a cell can have no number. *Could not
be read* means the read failed. *Not measured* means the store holds no definition for that
denominator. A nought on this card is always a real nought.

**Where names drop out of the universe** is a funnel over the rules the rest of the store already
applies, counted here and nowhere else: the companies in the SEC ticker and exchange file, less the
names not listed on Nasdaq, the New York Stock Exchange or Cboe, less the names the stored equity
universe does not hold yet, less the names below the $300M floor, less the names with no market cap
stored, less the three excluded security-type groups — funds and exchange-traded funds, special
purpose acquisition companies, and preferred shares, warrants and units — leaving the names the
boards screen.

Two of those drops are **not** rules and the funnel says so: a listed name the stored universe does
not hold is the universe being behind the SEC file after a new listing, and a missing market cap is
the store's own hole. A name the boards screen that the SEC file does not list is a **note**, outside
the arithmetic, never a drop. If a read failed, if the SEC file is not stored yet, or if the steps do
not add up, the funnel is **not drawn** at all and the card says which of the three it was: a funnel
that does not add up is not a funnel.

**How many names carry each input** counts, over the names the funnel keeps: the SEC company number,
a quote and market cap on the newest stored session, daily price bars on the latest completed
session, a CUSIP, a FIGI, EDGAR company facts, an annual report (a 10-K or a 20-F), and ownership.

**Identifier coverage** gives each row its own denominator rather than one shared total:

| Row | Counted out of |
|---|---|
| ISIN | the names that have a CUSIP, because the ISIN is derived from the CUSIP and never fetched |
| CUSIP mapped to a ticker | the CUSIPs the store has asked about |
| CUSIP the store asked about and has no ticker for | the same, which is the store's own unmatched count |
| CUSIP the filings hold that the store has never asked about | the CUSIPs that need a ticker, asked plus waiting |
| CUSIP the 13F board leaves out of its totals | nothing — the period's own CUSIP total is not stored, so this row reads *not measured* |

The three reads behind the CUSIP rows — the map, its waiting queue and the board's own summary —
each carry their own state, so one failing read leaves the other two answering and the failed one
says so instead of counting to nought. The two store summaries this card needs, prices and quotes,
are the same two the dataset rows already asked, and each is asked **once** per page read: a second
full sweep of either store file would spend the page's read budget on a question already answered.

### 3.7 The data-flow diagram

A section below Board inputs, titled *Where the data comes from and where it is stored*. Three
columns of boxes — **Provider**, **Job that writes it**, **Store file** — with one curved line per
route, drawn on the server from the rows the table above already collected. It therefore costs page
weight and **no query at all**: no store read, and no second pass over the registry.

- Boxes are de-duplicated by their words, and every line counts the datasets that take it, so
  pointing at a line names the whole route and says how many datasets use it.
- A dataset the registry gives no writer reads *started by hand from the dashboard*; one with no
  single provider reads *unknown provider*; one whose store file neither the row nor the registry
  names reads *no store file recorded*. None of the three is guessed.
- A line's colour is its store file, in the same hues as the table's store-file pills, and a legend
  under the diagram names all five. A grey line means the registry names no store file, or names one
  outside the five. The colour is never the only signal: the hover title carries the words.
- Provider and job boxes are real buttons, reachable by keyboard. Pressing one follows its lines and
  dims the rest; pressing it again shows every line. Store-file boxes are not buttons, because
  pressing one would select nothing a reader could act on.
- Two routes that take the same pair of boxes are nudged apart, so a later colour cannot draw over
  an earlier one and hide it.
- With no routes there is no section at all rather than an empty frame. Past 60 kilobytes of drawing
  the section starts collapsed, so the table above always paints first.

---

## 4. The universe refetch the board reports on

Writer: `core.universe_fetch.fetch_universe_stats`. The header **Refresh Universe** is
`POST /run/universe`, which runs membership, then the stats backfill, then the three scanners. The
standalone caps job is `POST /run/universe-stats`: the backfill only, with no screener and no
scanners.

### 4.1 Phase A — membership

**Membership** (Phase A) is a fresh pull of the SEC exchange file, one request per pull with the User-Agent the
environment names. It never reads the identity cache. The rows whose exchange is Nasdaq, NYSE or Cboe
are the members; over-the-counter rows and rows with no exchange are out. Market data is the Yahoo
screener, joined on the normalised ticker; a screener row that is not on the SEC list is dropped. A
listed name with no quote is saved with a null market cap, price and quote, and the SEC name; the
stored cap and price still carry forward.

The Yahoo side counts as **full** only when every slice ran to the total the screener reported and it
returned a quote for at least ninety per cent of the stored names that carry one; then it replaces the
table. Anything else is **partial**: it adds what it got and the listed names not stored yet, carries
cap, price and sector forward, re-applies the identity master, writes the provenance row marked
partial, and skips the backfill. It never deletes. The boards print "partial fetch" in the universe
line. This replaced an absolute row-count test that once let a three-thousand-name pull replace a
table of over seven thousand.

**One writer at a time.** A re-entrant universe write lock covers the SEC pull, the fetch, the write,
the identity apply, the provenance row and the backfill. A second run returns the stored universe
with the holder's process id, calls no network and writes nothing. The universe refresh job holds the
same lock for its whole run, plus a marker, so the single-board Fetches are refused while it runs; a
single-board Fetch holds its own marker, so a universe run is refused while one runs. A lock left by
a crashed process is broken even when its process id was reused.

Sector and industry are never in the screener. They come from the identity master: the eleven
Morningstar sector names, taken from the S&P 1500 list for its own names, then Unusual Whales, then
Yahoo, then the explicit SIC table, then the type rule; the industry is Unusual Whales' text. Never
from a SIC backfill into the universe.

### 4.2 Phase B — the market-cap backfill

**The stats backfill** (Phase B) works over every universe ticker whose market cap is null, filling the cap and
price from Yahoo's fast quote. It never calls the heavy per-ticker information endpoint for seven
thousand names. It pauses before each ticker, runs two workers, retries a rate-limit refusal with a
growing wait up to three attempts, and after five consecutive exhausted tickers **stops the run
early**: the rest stay null and are picked up next time. Because the work list is null-only, stopping
early costs nothing. A null after the backfill stays null — a warrant, an over-the-counter leftover, a
Yahoo gap — and no cap is invented. The backfill merges into existing rows with an update helper; it
never replaces the table, which would drop names membership just kept.

### 4.3 Feasibility review — the rules the writer keeps

Four rules came out of the 2026-09-24 code review of this plan, and the writer still keeps them:

- **No partial-row upsert into `equity_universe`.** That table is written as an upsert that overwrites
  every non-key column, so writing a ticker with only a cap and a price would blank its name, its
  sector and its stored quote. The backfill goes through `rawstore.update_columns`, which updates the
  named columns on matched rows and never inserts, so it can neither grow nor shrink the table.
- **Phase A carries the stored cap and price forward** when the screener quote omits them.
- **The backfill is null-only**, not an every-ticker pass, which is what makes stopping early free.
- **Each provenance key is its own row** — `__meta__` for membership, `__stats__` for the backfill,
  `__fetch__` for the pull itself — so a stats pass leaves membership's `__meta__` row intact.

`equity_universe_provenance` is written on every successful membership pass and every stats pass. The
board shows an empty provenance table as a hole, not as unknown.

A store-only Refresh of any board does zero Yahoo work: it re-reads the classification universe.

---

## 5. The newer-filing check

Comparing the stored accession to the SEC's own submissions list, end to end:

1. ticker to central index key, from the stored SEC exchange file rows;
2. `GET {SEC_DATA_BASE}/submissions/CIK##########.json`;
3. pick the newest 10-K, else the newest 20-F; an amended 10-K never outranks a 10-K;
4. compare that accession to the stored one in `chain_filings`:
   - equal, so `current` and **no download**;
   - missing or different, so fetch the primary document, store it, re-slice the two items we keep,
     and upsert the filing row as `updated`;
   - no 10-K or 20-F in the list, so `no_filing`.

Run: `python -m RevenueChaser.module_value_chain --sync-filings [--limit N] [--force] [--floor M | --tickers A,B]`.
Without a floor or a ticker list it covers the $300M classification universe, and the submissions
sweep covers every universe central index key. Cost: one submissions call per key at the shared
SEC rate, and archive downloads only on a mismatch.

The dashboard job is `POST /run/edgar-sync`, on the corpus row's Fetch. It needs no Unusual Whales
key, shares the EDGAR sync lock and the SEC token bucket, and is idempotent. Value Chain's own Fetch
stays cache-first — it fills tickers with no row and does not recrawl a name that already has a
filing. **New-filing detection is this sync job, not Value Chain Fetch.** After updates it rebuilds
the edges; Relatedness then reads the new text on its next Refresh without a new language-model run
until Relatedness is fetched.

A fiscal year that is not a calendar year is not a bug. Compare the accession, never the year in a
file name.

**Re-slicing what is already stored** is `--reslice-apply`: it re-slices every stored filing from its
stored document with no network, writes through the slice keep guard, rebuilds the mentions table and
records a result-history run. It holds the EDGAR sync and Relatedness locks, so check `/api/jobs` and
the lock holders before a live run. Look first with `--reslice-dry-run`. To roll back: stop the
dashboard and restore the EDGAR and history stores from the backups taken just before, before the
next scheduled sync.

---

## 6. External research — take or reject

Checked 2026-09-24. None of these replaces our own SEC client and filing picker.

| Source | What it is | Verdict |
|---|---|---|
| [nlpaueb/edgar-crawler](https://github.com/nlpaueb/edgar-crawler) | Download plus structured items for 10-K, 10-Q and 8-K. GPL-3. | **Do not vendor** (licence). We already pick the newest 10-K or 20-F from the submissions list and slice the two items we use. A 10-Q and 8-K item map would be a **new** corpus, not a freshness fix. |
| [pChitral/ETL-SEC-EDGAR-10-k-Filings](https://github.com/pChitral/ETL-SEC-EDGAR-10-k-Filings) | Management discussion to per-ticker comma-separated files. | **Reject.** The wrong section, file-based, and not a DuckDB store. |
| [psachdev/sec_data_downloader](https://github.com/psachdev/sec_data_downloader) | A thin wrapper around a paid filings interface. | **Reject.** We reach SEC directly; no paid proxy. |
| [Hoberg–Phillips Data Library](https://hobergphillips.tuck.dartmouth.edu/) | Academic product-description similarity, 1989 to 2023, annual lag. | **Not a filing fetcher.** Relatedness already profiles the live text. A possible later overlay to validate peers; never the source of truth, and its coverage stops in 2023. |

---

## 7. Not this board

- Merging with Implied Probability, Relatedness or Value Chain.
- Market-cap, sector or theme filters here. This tab's filters are the ones in 2.2.
- Scheduling inside the application. Scheduled tasks are Task Scheduler tasks or systemd user timers, listed on the
  Automations panel.
- Downloading 10-Q or 8-K filings into the corpus.
- A one-click "refresh every dataset" button.
- The market store's datasets (`uw/spot_expiry_strike`, `market_series`; see "The market store" in
  [`data_architecture.md`](data_architecture.md)). Their `dataset_check` rows live in `ops.sqlite` and reach this
  page in PR 2 of the new data system (#206).
- Deleting or moving a backup file from the page.

---

## 8. Still hand-written

The Stage 2 and Stage 3 dataset tables in [`data_architecture.md`](data_architecture.md) are still
written by hand rather than generated from `core/datasets.py`. That generator is code, not
documentation, so it is not part of this change and issue #146 stays open for it.
