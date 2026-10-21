# RevenueChaser — notes for Claude Code

Read [`AGENTS.md`](AGENTS.md) first; it is the source of truth for how data is stored, retrieved and tested. This
file holds the short version plus the rules that are easy to get wrong.

## Language

Code, comments, commit messages, issues and docs are written in **English**.

## Tracking and branches

- Every change maps to a **GitHub issue** in this repository, attached as a sub-issue of its area epic.
- Branch naming: `area/<key>/<slice>`, where `<key>` comes from the issue's `area:` label.
- One pull request per lane. A `_SCHEMA_VERSION` bump or anything that writes a live store is always its own PR.

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

Research and one-off measurement scripts are not part of the package. Their outputs and raw caches belong in
`.cache/research/<issue>/`, which is gitignored and regenerable.
