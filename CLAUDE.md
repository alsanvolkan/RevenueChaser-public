# RevenueChaser — notes for Claude Code

Read [`AGENTS.md`](AGENTS.md) first; it is the source of truth for how data is stored, retrieved and tested. This
file holds the short version plus the rules that are easy to get wrong.

## Where work happens

This repository is where RevenueChaser is developed. The private `alsanvolkan/RevenueChaser` still holds the
project board and two folders that are not published (`.hermes/`, the plan and research history, and
`scripts/pc/`, the machine-specific scripts), but **no new code goes there.** Work is authored, reviewed and
merged here, because this is where CI runs. The issues live here too, and so does `.bookkeeping/`.

- **Branch from `origin/main` of this repository,** push here, open the PR here.
- **Issues live here; the private tracker still holds the old ones.** A bare `#447` means issue 447 of
  *this* repository, so cite an issue that predates the move in full — `alsanvolkan/RevenueChaser#447` —
  which GitHub auto-links across repositories. The private numbering and this repository's do not line up,
  so never assume a number carries over.
- **Few pull requests, each carrying real work.** One per lane; a one-line follow-up rides the next PR that is
  open anyway rather than getting one of its own. An earlier rule of one batched PR per day existed because
  Actions minutes were scarce. They are not scarce here, and the two jobs run concurrently in well under an
  hour, so the reason to batch is now shared files, not cost.
- **Still its own PR, never batched:** a `_SCHEMA_VERSION` bump, a write to the live store, and a change to a
  job the dashboard runs. Each carries a backup and a rollback, and has to be revertable alone.
- **Work that touches only `.hermes/` or `scripts/pc/`** is the one exception to "nothing new in private",
  and it is rare. `.bookkeeping/` is published here, so it is not one of them.

## Language

Code, comments, commit messages, issues and docs are written in **English**.

## Tracking and branches

- Every change maps to a **GitHub issue**, attached as a sub-issue of its area epic. The issues and the board
  live in the private repository — see **Where work happens** for how to reference one.
- Branch naming: `area/<key>/<slice>`, where `<key>` comes from the issue's `area:` label.

## Tests

- **Unit suite:** `python -m pytest -q`.
- **End-to-end tier:** `RC_E2E=1 python -m pytest module_testing/e2e -m e2e`.
- **Both are offline by construction.** The root `conftest.py` replaces the socket primitives and raises
  `NetworkAccessAttempted` on any real connection; the e2e tier is allowed loopback only, because it drives a real
  dashboard against a local mock-provider server. Mock at the HTTP boundary (the `mock_httpx` fixture), never at
  rule-math level.
- `conftest.py` also redirects **both data roots** (`RC_STORE_DIR`, `RC_CACHE_DIR` and the other artifact roots) to a
  per-process temp directory, and the autouse `isolate_rawstore` fixture gives **every** test its own
  `raw.duckdb`. Never make that fixture session-scoped: a shared DuckDB file would serialise on the single-writer
  lock. `rawstore_tmp_db` fills that file by **copying** a per-process template that `ensure_all_tables` built once
  (`rawstore_template`, never opened by a test), not by running the ~1 s apply per test. A test of the schema apply
  or a migration itself is marked `@pytest.mark.rawstore_schema_from_scratch` and gets the apply on its own file.
- CI (`.github/workflows/tests.yml`) runs both tiers on every pull request into `main` and on `main`. The unit tier
  runs under `pytest -n auto --dist loadfile`, because a single pytest process is single-core bound.
- **TDD:** watch each new test fail before implementing it, and run the tests of the modules you touched before
  pushing. Report honestly which tests you saw red first and which mutation checks you ran — "not done" is an
  acceptable answer, an implied "done" is not.
- **A CI failure the diff cannot explain** is a flaky test. Fix it, or remove its host assumption, in the same
  branch; do not re-run until it passes.
- **Docs that state a value the code owns** (schema version, suite tiers, ports) are compared against the code by
  `core/tests/test_doc_constants.py`. A change to such a value updates the doc in the same commit.
- **Windows-only code** (Task Scheduler, process names) is tested through fakes, because CI runs on Linux.
- **Tests clean up their own scratch.** The root `conftest.py` redirects the store, cache, `edgar_docs`
  and SPX-Gamma roots at one `rc-pytest-*` directory per process and removes it in
  `pytest_sessionfinish`; `pytest.ini` sets `tmp_path_retention_policy = failed`, so a passing test's
  `tmp_path` goes with it and a failing one's is kept to look inside. **Not `none`:** on Windows that also
  removes the numbered basetemp root mid-session and every later `tmp_path` fails with `WinError 3`.
  A test that needs its scratch to outlive the run takes an explicit path, never the session root. Only the
  process that created a root removes it (`RC_PYTEST_SCRATCH_OWNER`), because under `pytest -n auto` every
  xdist worker inherits the controller's root and the first to finish would otherwise delete it for the rest.
  A killed run still leaves scratch behind: `./app.sh clean-scratch` reports it and `--apply` removes it,
  leaving alone anything written to in the last two hours **in its top two levels** — deep enough for
  `<root>/.store/raw.duckdb`, which is why the age is hours and not minutes. Measured 2026-10-23 before
  this landed: 917 leaked `rc-pytest-*` directories and 2.1 GB under pytest's own basetemp.

## Data safety

- **The durable store is `.store/`** (`raw.duckdb`, `edgar.duckdb`, `history.duckdb`, `prices.duckdb`); the
  regenerable caches are `.cache/`. Both are gitignored — this repository is **code only**, and no market data ships
  with it. `docs/data_architecture.md` is the contract.
- **Read-only inspection of a real store** needs `duckdb.connect(..., read_only=True)` **and** the environment
  variable `RC_STORE_AUTO_ENSURE=0`. Without the second one, application code applies the schema manifest on its
  first call and creates tables in the file you meant only to read.
- **A `_SCHEMA_VERSION` bump** needs a store checkpoint taken before the merge; there is no automation for it.
- **The dashboard holds the DuckDB writer** (`python -m RevenueChaser.app.dashboard`, port 8000 by default), so
  start store-writing jobs through it rather than running a second writer:
  `curl -X POST -H "X-RC-Panel: cli" http://127.0.0.1:8000/run/<job>`, then poll `/api/job/<id>`.
  **Every non-`GET`/`HEAD`/`OPTIONS` request needs that header**; without it the dashboard answers
  `403 panel header required` and starts nothing. Any non-empty value works, except
  `POST /api/automations/<name>/<action>`, which needs exactly `X-RC-Panel: automations`.

## Keys and private material

- **No key belongs in this repository.** `.env` is gitignored; `.env.example` lists the variable names with empty
  values. Read credentials through `core/credentials.py`, never by printing or logging a value.
- `scripts/private_check.py` is the guard: it refuses a secret, a personal home path or mailbox, an account id and a
  private folder. Run it with `--all` over the tree, or `--staged` before a commit;
  `git config core.hooksPath scripts/hooks` wires it into a pre-commit hook.
- A paid provider call (the optional DeepSeek profile writer) is gated in its own code — a cost cap and an off-peak
  window, see `core/offpeak.py` — and never by a separate watcher process.

## Research scripts

Research and one-off measurement scripts are **not part of this repository**. A measurement is run once, its
number is recorded in the issue or the doc that needed it, and the script does not stay behind. So this tree
carries no `scripts/research/`, and no test drives one; `scripts/tests/` tests the real jobs in `scripts/`.
Outputs and raw caches belong in `.cache/research/<issue>/`, which is gitignored and regenerable.

A few docstrings in `core/` still credit the research script a number came from (`core/reaction_anchor.py`,
`core/rawstore.py`, `core/providers/openfigi.py`). Those names are provenance, not files in this tree.

## Feedback-thread workflow

When the owner wants problems or improvements collected (standing rule, all repos, 2026-10-24):

1. **Collect.** Open one feedback thread. The owner writes every item there; acknowledge each in one short line and fix nothing until the owner says the list is done.
2. **Reproduce.** Reproduce each item from the code or live data before promising a fix. Cite `file:line` or a measurement.
3. **Review.** One Fable 5.1 review of the list against the code, using the `avo-architecture-review` and `design-suite` skills. It reports findings, order and risks; it implements nothing.
4. **Plan.** Write a plan file in `/mnt/project-files/<app>/` with ordered steps, risks, and the decisions only the owner can make. Ask only those.
5. **Build.** One branch, one pull request, one commit per step. Tests seen red first. A design mock first for anything new.
6. **Phone friendly.** Every UI works on a phone and in light and dark: single column, full-width cards, 44 px touch targets, charts scale to their container, tables stack instead of scrolling sideways.
7. **Hourly.** Scheduled jobs and monitors refresh hourly unless the owner says otherwise.
