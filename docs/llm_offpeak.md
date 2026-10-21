# Plan — DeepSeek off-peak gate

DeepSeek bills peak hours at **2×** the off-peak rate. This plan makes the
optional LLM steps of the Relatedness board (profile extraction and theme naming;
`module_relatedness` is the only caller of `core/providers/deepseek.py`) run
**off-peak only**, stop automatically at the peak boundary, and resume on the
next off-peak run. It is deliberately small: a gate, not a scheduler. The
optional Grok writer (#388, below) has no off-peak gate; its gate is the
SuperGrok allowance.

Status: **implemented.** `core/offpeak.py`, `core/providers/deepseek.py`
(durable `llm_cache`), `module_relatedness/store.py` (durable profiles) and the
off-peak CLI are done and tested. Remaining: the live run itself (manual,
off-peak).

## Implemented (2026-09-20)

- `deepseek.complete`: order **cache → gate → HTTP**; at most **2 attempts** (one
  retry, only after an HTTP 429 or an error raised before the request left the
  machine; a 5xx or a failure after the send is final, it may already be billed);
  cache is the durable `llm_cache` table (no `.cache/llm_cache/*.json` files). Rows
  carry `cache_key`, `ticker`, `cik`, `model`, `thinking`, `effort`,
  `prompt_version`, `input_hash`, `response`, `fetched_at`, `status`.
- `module_relatedness/store.py`: durable profiles (`relatedness_profiles` +
  `relatedness_attributes`), structured `cache_key`, mode stamp in `run_registry`.
  Absence of a profile row = **pending** (resume is automatic and cache-first).
- `module_relatedness/engine.run`: catches `PeakHoursError` at the boundary, renders
  from the durable profiles already written, and returns `pending` + `next_off_peak`;
  records a `relatedness_run` marker in `run_registry`.
- **$ hard-stop**: `config.USD_CAP` (env `RC_RELATEDNESS_USD_CAP`, default 8.0) caps
  LLM calls per run at `USD_CAP / EST_USD_PER_CALL`; the rest stay pending. `--cap 0`
  means no LLM call at all. The budget counts calls, the thinking retry included
  (`profiles.CallBudget`, #118 PR C).
- **Stop events**: the first worker that hits the peak boundary (`peak`), a rejected
  key (`auth`) or repeated transient failures (`transient`) records the reason; the
  run stops submitting groups, renders from the profiles already written and returns
  `stopped_reason` (plus `error`, and `next_off_peak` for a peak stop). An auth or
  transient stop is recorded `partial` in the result history, like a peak stop.
- CLI: `python -m RevenueChaser.module_relatedness --run [--limit N] [--cap USD]`
  (and `--status`). A stopped run or one with work left prints one JSON object
  (`done`, `pending`, `next_off_peak`, `stopped_reason`, `error`). Exit **0** for a
  finished run, a peak stop, the cost cap and pending names (the normal night-to-night
  state, so the scheduled task stays green); exit **1** for an `auth` or `transient`
  stop and for the lock skip (another run holds the lock, nothing ran).

## Decisions (locked)

| Topic | Decision |
|---|---|
| Scheduling | **(1a)** gate + manual; Task Scheduler is a **visible, installable row** on the Settings Automations panel (`RC-Relatedness-OffPeak`), **not installed by default**; no self-deferring job |
| Peak | LLM **must not run** |
| Cache | DuckDB `llm_cache` (model + prompt hash → response) |
| Peak behaviour | A **cache hit is served at peak** (it costs nothing); only a cache **miss** stops. The gate sits after the cache in `deepseek.complete` |
| Boundary | leaving off-peak stops the run; resume on the next off-peak run |

## Windows

Defined in **UTC** so a local daylight-saving change cannot shift them.

| | Peak (UTC) | Off-peak |
|---|---|---|
| Mon–Fri | 01:00–04:00 and 06:00–10:00 | all other hours |
| Sat–Sun | — | 24 hours |

Reference (informational; DST shifts these):

| Local | Peak |
|---|---|
| UTC+3 (Türkiye, no DST) | 04:00–07:00 and 09:00–13:00 |
| CET / CEST | ≈ 02:00–05:00 / 07:00–11:00 (winter) |
| UTC+8 (Beijing) | 09:00–12:00 and 14:00–18:00 |

## Behaviour

1. **Before starting a batch:** `is_off_peak()`. If peak → do not start; 0 HTTP;
   `JobMessage("Peak hours — LLM not run. Next off-peak: HH:MM UTC")`.
2. **Before every call:** re-check `is_off_peak()`.
   - off-peak → call, write `llm_cache`, mark the item `done`.
   - peak reached → **stop**: no further calls and no HTTP; an item already in
     `llm_cache` is still served, the others stay `pending`.
3. Job ends `done` (not an error) with `N/M done, rest pending, next off-peak …`.
4. The board renders whatever completed; no partial/inconsistent writes.
5. Next off-peak run resumes: finished items come from `llm_cache` and the durable
   profiles, only the `pending` ones call the API.
6. `RC_DEEPSEEK_OFF_PEAK_ONLY=1` default **on**. An override exists but is
   discouraged — the rule is "no LLM at peak".
7. The gate never sleeps; the single job worker is never blocked.

## Components

- **`core/offpeak.py`** (done) — pure, I/O-free:
  `PEAK_WINDOWS_UTC = ((1, 4), (6, 10))`, `is_off_peak`, `next_off_peak`,
  `local_peak_windows`, `describe`. Tests in `core/tests/test_offpeak.py`.
- **`core/providers/deepseek.py`** — key via `core/credentials.py`
  (`DEEPSEEK_API_KEY`). Order: **cache → gate → HTTP**. A cache miss at peak raises
  `PeakHoursError` (no HTTP). The base URL is `RC_DEEPSEEK_BASE`, read at call time.
  A reply that is not JSON raises `DeepSeekError` and is not cached; the caller's
  `validate=` hook can also keep an answer out of the cache. The key is read from
  the environment only; no key file in the tree is read, and none belongs in the
  repository.
- **`llm_cache`** (`raw.duckdb`) — `cache_key` (a hash of model, thinking mode,
  effort, system and user text), `ticker`, `cik`, `model`, `thinking`, `effort`,
  `prompt_version`, `input_hash`, `response` (`{content, usage}`), `fetched_at`,
  `status`. Turns re-runs into 0-cost hits (and is the bigger saving than off-peak
  itself).
- **Pending marker** — there is none to add: an item with no profile row in
  `relatedness_profiles` is pending, so a stopped batch resumes by itself.
- **Redo** — a stored profile is profiled again when it was made under another
  mode or from another input: the hash of today's prompt (filing text and the
  `equity_universe` company name) differs from the `input_hash` in its cache key
  (#388 D5). Both wait for the run's `$` cap like any pending name. "Another
  mode" is another `prompt_version` for every writer; a DeepSeek profile must
  also match the mode's model, thinking and effort. A Grok profile (its cache key
  carries `"provider": "grok"`) at the current `prompt_version` is current for
  both writers (`store.is_stale`, `ALT_WRITERS`), so the nightly DeepSeek run
  does not redo it; see the Grok writer below.

## Grok writer (#388, no off-peak gate)

The D5 v2 re-profile may run through a SuperGrok subscription instead of DeepSeek. It is a manual CLI path;
the dashboard Fetch and the scheduled `RC-Relatedness-OffPeak` (`--run --cap 8`)
stay on DeepSeek, unchanged.

- **Client** `core/providers/grok.py`: order **cache → login → allowance → HTTP**.
  The login is an xAI OAuth token, read at call time through
  `subscription_usage.xai_token` (`XAI_OAUTH_TOKEN`, else the file named by
  `RC_XAI_AUTH_FILE`); a missing or expired login stops before any request, and
  the token never appears in a log or an error. Endpoint `RC_GROK_BASE` +
  `/chat/completions` (default the Grok CLI chat proxy
  `https://cli-chat-proxy.grok.com/v1`; only `grok.com` hosts or a loopback mock
  are accepted), model `RC_GROK_MODEL` (default `grok-4.6`). DeepSeek's
  `thinking`, `reasoning_effort` and `temperature` are **not sent**; `json_mode`
  becomes `response_format: {"type": "json_object"}`.
- **No off-peak gate.** SuperGrok bills by allowance, not by time of day. Its gate
  is the allowance (`core/subscription_usage`): read before the first request of a
  run and again after every 25 requests; a window at its ceiling (session 80 %,
  weekly 90 %, the weekly one set by `RC_GROK_CAP` up to 95, a value outside that
  falls back to 90; `--status` reads the same cap) or an unreadable reading stops the run cleanly (`stopped_reason`
  `allowance`, `next_reset`), exit 0, and the rest stays pending.
- **Cost accounting:** a call costs 0 USD (subscription); the run summary counts
  the calls. `--cap` (USD) is DeepSeek's; with Grok, `--limit N` caps the calls and
  never shortens the universe the board covers.
- **Stored writer:** `relatedness_profiles.model` is the Grok model and the cache
  key names `"provider": "grok"`; the `llm_cache` row's `model` is the Grok model
  and its key hash starts with the provider, so a Grok reply never replays as
  DeepSeek's or back.
- **Stops:** `auth` (login), `request` (any other 4xx: endpoint, model or a
  parameter is wrong), `transient` (repeated), `unusable` (the run's first 5
  replies held no profile; none of them counts an attempt) exit 1; `allowance`
  exits 0.
- **CLI:** `--grok-ping` (one live call with the profile prompt and a run's
  parameters, writes nothing), `--run --provider grok --limit N`,
  `--name-themes --provider grok`, `--rebuild-themes --name --provider grok`.

## Tests

- Peak start → 0 HTTP, immediate stop.
- Peak reached mid-batch → stops there, remaining `pending`, resume continues.
- Peak + cache hit → the cached answer is served, 0 HTTP.
- Off-peak + miss → 1 HTTP, cached; off-peak + hit → 0 HTTP.
- `no_fetch` / Refresh → never touches the LLM, 0 HTTP.
- `RC_DEEPSEEK_OFF_PEAK_ONLY` unset/1 → gate active; 0 → runs (documented escape).

## Not in scope

Installing a Task Scheduler task automatically, an in-app cron/timer, retries beyond the single
429 / pre-send retry above, and streaming.
