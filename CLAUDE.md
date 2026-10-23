# RevenueChaser — notes for Claude Code

Read [`AGENTS.md`](AGENTS.md) first; it is the source of truth for how data is stored, retrieved and tested. This
file holds the short version plus the rules that are easy to get wrong.

## Where work happens

This repository is where RevenueChaser is developed. The private `alsanvolkan/RevenueChaser` still holds the
issues, the project board and three folders that are not published (`.hermes/`, `.bookkeeping/` and
`scripts/pc/`), but **no new code goes there.** Work is authored, reviewed and merged here, because this is
where CI runs.

- **Branch from `origin/main` of this repository,** push here, open the PR here.
- **Issues live in the private repository.** Reference one in full — `alsanvolkan/RevenueChaser#447`, never
  the bare `#447`, which would point at an issue of *this* repository that does not exist. GitHub auto-links
  the full form across repositories.
- **Few pull requests, each carrying real work.** One per lane; a one-line follow-up rides the next PR that is
  open anyway rather than getting one of its own. An earlier rule of one batched PR per day existed because
  Actions minutes were scarce. They are not scarce here, and the two jobs run concurrently in well under an
  hour, so the reason to batch is now shared files, not cost.
- **Still its own PR, never batched:** a `_SCHEMA_VERSION` bump, a write to the live store, and a change to a
  job the dashboard runs. Each carries a backup and a rollback, and has to be revertable alone.
- **Work that touches only `.hermes/`, `.bookkeeping/` or `scripts/pc/`** is the one exception to "nothing new
  in private", and it is rare.

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
  lock.
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
- **Tests clean up their own scratch.** The root `conftest.py` redirects the store, cache, `edgar_docs`,
  Discord-media and SPX-Gamma roots at one `rc-pytest-*` directory per process and removes it in
  `pytest_sessionfinish`; `pytest.ini` sets `tmp_path_retention_policy = failed`, so a passing test's
  `tmp_path` goes with it and a failing one's is kept to look inside. **Not `none`:** on Windows that also
  removes the numbered basetemp root mid-session and every later `tmp_path` fails with `WinError 3`.
  A test that needs its scratch to outlive the run takes an explicit path, never the session root. A killed run still
  leaves scratch behind: `./app.sh clean-scratch` reports it and `--apply` removes it, leaving anything
  written to in the last two hours alone so a run in progress is safe. Measured 2026-10-23 before this
  landed: 917 leaked `rc-pytest-*` directories and 2.1 GB under pytest's own basetemp.

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
