# End-to-End Testing

The unit suite ([`testing_framework.md`](testing_framework.md)) covers rule
maths and route wiring. It does not run a board. That gap let real regressions
ship green — a rotation range picker that silently rendered the whole store, a
"Load Earnings Today" button that did nothing, a snapshot fetch that deleted its
own cache on failure. All three passed 1100+ unit tests.

The end-to-end tier closes that gap: it boots the **real dashboard as a
subprocess**, against a **throwaway `.store` and `.cache`**, with every provider
pointed at a **local mock server**, and drives each board through the full
lifecycle.

```bash
./app.sh test-e2e         # the tier
./app.sh test-e2e-v       # verbose
./app.sh test-e2e-live    # also hits the REAL provider APIs (needs credentials)
```

It is **opt-in**: `pytest.ini` sets `addopts = -m "not e2e"`, so `./app.sh test`
stays the fast unit run. Nothing here touches your real data or the internet.

---

## The invariant under test

One flow per board, asserting the contract in
[`data_retrieval.md`](data_retrieval.md):

1. `POST /run/<route>` (the harness client sends `X-RC-Panel: e2e`) → poll `/api/job/<id>` to a terminal state
2. the module's rawstore tables gained rows
3. `/api/data_asof/<key>` reports a date — store-backed proof the fetch landed
4. the `.cache/` artifact exists and contains the fixture data
5. **delete the artifact**, snapshot the request counters
6. `GET /view/<key>?refresh=1` → 200, artifact rebuilt, data still present
7. **the request counters did not move**

Step 7 is the point. The mock server counts every request and the yfinance fake
counts every call, so "refresh performs no network" is a fact the suite checks
rather than a comment in a docstring. Several tests also `restart()` the
dashboard between steps, which proves nothing in memory is load-bearing — only
what reached `.store` survives.

---

## How isolation works

`core/config.py` reads two environment variables **at import**:

```
RC_STORE_DIR   ->  DATA_DIR   (.store: raw.duckdb + edgar.duckdb)
RC_CACHE_DIR   ->  OUTPUT_DIR (.cache, rendered artifacts)
```

Import time is not an implementation detail — it is the only workable seam.
Roughly twenty module-level constants derive from those roots
(`app/runners.py`'s `*_LATEST`, `core/rawstore.py`'s `DB_PATH`, each module
config's paths), and several modules re-bind them into their own namespace, so
monkeypatching the origin after import reaches only some of them. The env var is
read once, before anything else, and everything downstream follows.

Two consequences:

- The **root `conftest.py` sets both in `pytest_configure`**, so the *unit*
  suite is isolated too. Before this, `isolate_rawstore` covered only the DuckDB
  file: any test hitting `?refresh=1` unlinked the developer's real rendered
  boards, and any test invoking a runner overwrote them.
- The e2e harness sets them **per test** and launches the dashboard in a
  subprocess, which is also how the "restart" step works.

`./app.sh serve` with no env set resolves to the repo-local `.store`/`.cache`
exactly as before.

### Parallel runs (`-n 4`)

CI runs this tier as `pytest -n 4`; locally `./app.sh test-e2e -n 4` does the same. xdist workers inherit the
controller's environment, so the root `conftest.py`'s redirect is **one folder per run**, shared by every
worker. The tier is safe in parallel because nothing a dashboard writes comes from that shared folder:

- the `dashboard` fixture points `.store`, `.cache` and every side root (`RC_EDGAR_DOCS_DIR`,
  `RC_DISCORD_MEDIA_DIR`, `RAW_STORE_BACKUP_DIR`, `SPX_GAMMA_DATA_ROOT`, `RC_PATREON_DATA_DIR`) at the test's
  own `tmp_path`;
- the dashboard subprocess binds port 0 and writes the port it got to `$RC_E2E_PORT_FILE`. A port picked in
  the test process and released would be free only until another worker bound it;
- the mock-provider server is session-scoped, which under xdist means one per worker, also on port 0.

`test_harness_parallel.py` holds the first two rules. A new folder the dashboard writes to gets its own
variable in that fixture and in that test.

### One Fetch for the read-only tests (`harness/shared.py`)

Most of the tier's time is a dashboard boot plus a Fetch per test. Where several tests only read what one
default Fetch leaves behind, they share both: a module-scoped fixture builds the dashboard with
`shared_dashboard`, seeds it, runs the Fetch once, and the tests read the result. `test_identity.py` (one identity
run), `test_board_sector_rotation.py` and `test_board_comovement.py` do this. The rules:

- **Only read.** A test that changes the routes, the store, the settings, restarts the dashboard or runs the job
  again keeps its own function-scoped `dashboard`. A shared test may empty the cache only when its Refresh
  rebuilds it before the test ends.
- **One `xdist_group` per shared Fetch.** CI runs `-n 4 --dist loadgroup`, so the group's tests land on one worker
  and the Fetch runs once; without the mark each worker would run it again.
- **Every assertion stays.** A test that asserted on the job result reads it from the fixture
  (`identity_run.result`, `fetched.result`); a test that counted provider requests during the Fetch reads the
  requests the fixture kept (`fetched.requests`).

---

## Layout

```
module_testing/e2e/
├── conftest.py                     # temp dirs, mock server, dashboard fixtures
├── harness/
│   ├── dashboard_entrypoint.py     # installs fakes, then serves dashboard.app on port 0
│   ├── dashboard_driver.py         # run_job / view / restart / table_count
│   ├── shared.py                   # one dashboard + one Fetch shared by read-only tests
│   ├── cdp_browser.py              # headless Chrome over DevTools: exact viewport, colour scheme
│   ├── mock_provider_server.py     # loopback HTTP server + request counter
│   ├── yfinance_fake.py            # fake yf.Ticker / yf.screen + call counter
│   └── provider_fixtures/          # hand-written JSON, one file per endpoint
├── test_board_<name>.py            # one per board
├── test_board_html_browser.py      # hostile fixtures in headless Chrome (ADR-X1)
├── test_phone_shell.py             # the shell at 390 px and 1280 px, light and dark (#184)
├── test_cdp_browser.py             # the DevTools harness stays off the network
└── test_live_provider_fetch.py     # the --live tier
```

`test_board_html_browser.py` loads each board's hostile fixture
(`core/tests/board_fixtures.py`) with `chrome --headless --dump-dom` and fails if
any payload ran script. It needs no dashboard; it finds Chrome as `$RC_CHROME`,
`google-chrome`, `google-chrome-stable`, `chromium` or `chromium-browser`. With no
browser it skips locally and fails under `CI=true`. A new board adds an entry to
the registry, or `core/tests/test_board_html_guard.py` fails.

`test_phone_shell.py` drives Chrome through `harness/cdp_browser.py`, a DevTools client over a loopback
WebSocket with no pip dependency. `--window-size` cannot give a phone viewport (new headless Chrome clamps
the window to 500 px), so the harness uses device-metrics emulation for an exact width and media emulation
for `prefers-color-scheme`. Against a live dashboard it checks that at 390 px the page never scrolls sideways,
the nav is reachable through the drawer, the shell's touch targets are at least 44 px, and light and dark both
render: dark on a first visit whatever the device, light and Auto by the toggle (remembered across a reload);
at 1280 px the sidebar stays a column.
It finds Chrome the way the HTML guard does, and fails under `CI=true` without one. Chrome resolves no host
but loopback (`--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1`: the rule matches
an IP literal too, so the dashboard's `127.0.0.1` is excluded by name); `test_cdp_browser.py` holds both halves.

### The mock provider server

A real loopback HTTP server, not an `httpx` patch. That matters: the bugs this
tier exists to catch lived *in* the HTTP layer — a 401 swallowed into an empty
board, a 404 aborting a snapshot with an opaque message. Patching `httpx.get`
skips the code under suspicion (and misses `httpx.post` and every
`httpx.Client`, which is why the unit suite's `mock_httpx` was not a usable
base).

Routing is longest-prefix over the path, so a test overrides one endpoint
without restating the rest:

```python
provider_routes.route("/api/stock/SPX/gex-levels", status=404)
provider_routes.route("/api/stock/", payload=lambda path: {"data": ohlc_for(path)})
```

An unrouted path returns **404, deliberately** — a silent `200 []` would hide a
fixture gap the same way production used to hide a dead API key.

Fixtures are self-describing: each file carries the path it answers, so the
route lives with the data instead of in a mapping table that drifts.

```json
{"path": "/api/option-trades", "status": 200, "payload": {"data": [...]}}
```

### The yfinance fake

yfinance cannot be redirected by URL (see
[`data_retrieval.md`](data_retrieval.md)), so it is replaced on the library
itself — `yfinance.Ticker` and `yfinance.screen`. It must be installed **before**
any RevenueChaser import, because `module_spx_gamma/vix.py` binds `yf`
at module scope; that ordering is the reason
`dashboard_entrypoint.py` exists rather than the test importing the app.

### Credentials in the harness

`UNUSUAL_WHALES_API_KEY` is set to a dummy — the mock accepts any token, and an
unset key would trip the preflight before a board ran. To reproduce the
*rejected-key* state (a live token that has expired), use the `dead_uw_token`
fixture, which makes the auth probe answer 401. That is how the suite proves
which boards still work without credentials.

---

## Adding a board

1. Add fixtures under `harness/provider_fixtures/<provider>/`, each carrying its
   `path`. For parametric endpoints, register a callable route in the test.
2. Copy `test_board_leaps.py` — it is the reference shape.
3. Cover: fetch persists, board renders, `data_as_of` is set, refresh rebuilds a
   deleted artifact, **refresh makes no network calls**, refresh survives a
   restart, empty store renders an empty state rather than a 500.
4. Then do the step below, which is the one that actually matters.

## Prove the test can fail

**A green test that never goes red proves nothing.** Before considering a board
done, reintroduce the bug it is supposed to catch and confirm the test fails
with a message that explains itself.

This is not ceremony. The first version of the sector-rotation range test passed
with the bug fully present: it searched the HTML for out-of-range dates, and the
board never renders dates as literal strings. The assertion had to move to a
real discriminator — the board embeds one `rs_ratio` per (ETF × snapshot-date),
so the count tells you exactly how many stored snapshots the render used:

```
selected 2026-06-01..2026-06-28 (312 stored rows) but the board rendered 624
rows (whole store = 624) — the range picker is being ignored
```

Writing an e2e test that *can* fail is harder than writing one that passes. Budget
for it.

---

## The `--live` tier

`./app.sh test-e2e-live` adds tests that hit the real provider APIs, writing
into the temp store. Its job is catching **schema drift** the hand-written
fixtures cannot: a field renamed upstream, a shape changed. It is opt-in and
skips itself when credentials are missing or rejected, so it can never fail the
default run or burn rate limit by accident.

---

## Limits

Be honest about what this tier does not reach.

- **Browser JavaScript is out of scope.** The week-picker timezone bug lived in
  `isoWeekInfo`'s use of `toISOString()`; a server-side harness can only assert
  on the served source (`app/tests/test_earnings_calendar_ui.py` does). Catching
  the rendered behaviour needs a DOM test.
- **Layout is checked for the shell only.** `test_phone_shell.py` measures the shell around the boards;
  each board's own layout inside its frame is not checked until its own phone PR adds a check.
- **Editor and machine config is out of reach.** A DuckDB lock held by another
  process is covered by its *symptom*, not its cause.
