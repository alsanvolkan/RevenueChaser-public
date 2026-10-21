---
name: revenuechaser-conventions
description: RevenueChaser project conventions — load before changing dashboard filters (market cap, earnings, index, GICS, theme, ticker combobox), GICS/SIC/sector/theme mapping, equity_universe / classification_universe / the $300M cut, index membership, board registration (Fetch vs Refresh), or where data lives (.store / .cache / module data). Gives the single source of truth (SSoT) file for each rule and the known traps, so a change lands in the right place instead of forking a second copy.
---

# RevenueChaser conventions

These rules say **where each thing lives**. The detailed docs are the reference; this
skill points to them and lists the traps that have bitten before. When code and this
file disagree, trust the code, then fix this file in the same change.

| Topic | SSoT (code) | Reference doc |
|---|---|---|
| Storage (.store vs .cache, typed columns) | `core/rawstore.py` (`MANIFEST`), `core/config.py` | `docs/data_architecture.md` |
| Fetch vs Refresh | `app/dashboard.py`, `app/runners.py` | `docs/data_retrieval.md` |
| Filters + ticker combobox | `core/reporting_common.py`, `core/static/rc_filters.js` | `docs/dashboard_ui.md` |
| Sector + industry | `core/identity/` (`security_master`, the single writer via `apply.py`), `core/gics_map.py` (the 11 Morningstar names, the two-level path) | issues #85, #86, #265, #266 |
| Themes | `core/taxonomy.py`, `core/ticker_themes.py` | `docs/classification_design.md` |
| Universe | `core/universe.py` (reader), writer below | `docs/data_health.md` |
| Index membership | `core/index_registry.py` | — |
| Area map (which area owns what) | — | `docs/system_map.md` (issue #45; may not exist yet) |

## 1. Where data lives

- **`.store/` is durable** — `raw.duckdb` (structured raw + derived data, identity/GICS
  spine, `sec_company_*`, universe, index registry, board runs) and `edgar.duckdb`
  (`edgar_documents`, `edgar_facts`, `edgar_financials`, `chain_*`). Never delete it.
  The one durable file tree is `edgar_docs/{cik}/{accession}.html.gz`.
- **`.cache/` is regenerable** — rendered boards `{module}_latest.html` and
  `{module}/{module}_latest.json`, written through `app/runners.py::_write_output`.
  Research output goes to `.cache/research/<issue>/`.
- **Module-owned caches** — `module_<x>/data/`, with `DATA_DIR` defined in the module config.
- **Paths derive from `OUTPUT_DIR` / `DATA_DIR` in `core/config.py`**, never from
  `BASE_DIR`, or tests escape the `RC_STORE_DIR` / `RC_CACHE_DIR` redirect.
  `core/tests/test_store_layout.py` enforces this.
- **Hot-path rule:** anything filtered, scanned or joined is a typed column, never inside
  a JSON blob. Promote it in the same change.
- **Partial-row updates:** `equity_universe` is `write: "upsert"` and overwrites *every*
  non-key column. Use `rawstore.update_columns(...)` to change some columns.
- A new MANIFEST table or column bumps `_SCHEMA_VERSION` (currently `"6"`). **Warn the
  user to take a manual `.store/` checkpoint.**
- App code applies the MANIFEST to the store on its first call; for read-only measurement on the live store set `RC_STORE_AUTO_ENSURE=0`.

## 2. Boards: Fetch vs Refresh

- **Fetch** = `POST /run/<module>` → async job (`app/jobs.py`, short or long lane) →
  network → store → re-render. Long jobs (universe, 10-K sync, 13F, cap backfill) go on
  `lane="long"`. **Every write (POST and any non-`GET`/`HEAD`/`OPTIONS` method) must send an
  `X-RC-Panel` header** with a non-empty value, or the dashboard answers `403` (#231): a
  board's `fetch` adds it, a shell call uses `-H "X-RC-Panel: cli"`, and a test uses
  `core.tests.panel_client.panel_test_client(app)`.
- **Refresh** = `GET /view/<module>?refresh=1` → re-render from `.store` with **zero
  network**, and no credentials required. e2e asserts this.
- A new board must be registered in **four** places: `_MODEL_ONLY_RENDERERS` (derives
  `_STORE_REFRESHABLE`) in `app/dashboard.py`, `STORE_BACKED` in
  `templates/dashboard.html` (without those two Refresh silently degrades), `TABS` /
  `_module_file`, and a hostile-payload `Board` in `core/tests/board_fixtures.py`
  (`test_registry_covers_every_board` fails without it; a sub-page with its own HTML is its
  own `Board`).
- **Board HTML is untrusted-string territory (ADR-X1, `AGENTS.md` §6).** Python text and
  attributes go through `core/html_safe.py::esc`, inline-script data through
  `script_json`, client `innerHTML` through `rcEsc` (`html_safe.ESC_JS`; the one JS escaper,
  and a page that calls it must embed it). Inline handlers read `this.dataset.*`, never an
  interpolated value. Jinja HTML runs with autoescape on, and a trusted fragment is `Markup`
  at the call site. A ticker from a URL is `.strip().upper()` then `_valid_ticker`.
- **A run that finds nothing says so:** write the artifact with an explicit empty state
  and raise `jobs.JobMessage`.
- **Swing and Fundamentals share one scan body**, `core/screen_common.run_screen` (#147): universe load,
  identity-type exclusion, `prefilter`, the shortlist, the thread pool (a ticker that raises is a counted
  `compute_error`), the empty state and persist-on-Fetch. Change those there, once. Per board: `_compute_row`,
  `build_payload`, `scores.persist`, the report.
- **Global metadata** (Regime badge, last sync) lives only in the parent header. A board
  top bar is `h1` + subtitle + at most `Last sync: <friendly date> EOD`.

## 3. Filters and ticker pickers

- **Target component (ADR-2):** the shared filter bar `filters_bar_html()` +
  `core/static/rc_filters.js` (`RCFilters`, `RCCombobox`). The facet order is fixed:
  **Index → Market cap → Sector (2 levels: sector, industry) → Theme → Earnings**. A board may omit a
  facet, never reorder one. State is in localStorage `rc.filters.v1` plus the URL hash.
  The row contract is `core/universe.py::filter_fields`.
- **Migration status:** Co-movement uses the new bar (pilot). Asymmetric, Swing,
  Fundamentals, OIPD, Relatedness and Value Chain still inject the older
  `MC_FILTER_JS` / `EWEEK_FILTER_JS` / `INDEX_FILTER_JS` / `SECTOR_FILTER_JS`
  snippets. Moving them is issue #32. **Do not add a third variant**: new work uses the
  new bar, and old boards are migrated rather than patched.
- **Ticker pickers are searchable comboboxes**, not native `<select>`. Match on ticker and
  name, ranked: exact ticker → ticker prefix → ticker contains → name prefix → name
  contains. The closed label is `TICKER — full name`. Use `combobox_html()` /
  `RCCombobox`; do not fork it.
- **Filters first, then the combobox.** Filters narrow the combobox; on Implied
  Probability they only narrow the combobox.
- Market-cap buckets: Micro < $300M · Small $300M–2B · Mid $2B–10B · Large $10B–200B ·
  Mega > $200B, plus the preset $50M–$5B and custom min/max in $M.

## 4. Universe

- **Writer (spine):** `core/universe_fetch.py` — `fetch_universe_stats()`
  (Phase A: the SEC exchange file's Nasdaq / NYSE / CBOE rows are the members, the Yahoo
  screener's quotes join on by ticker; a failed SEC map or an SEC list under `MIN_FULL_UNIVERSE` = 3000 listed rows keeps the
  stored universe; full-vs-partial guard D6: every chunk complete and quotes for >= 90% of the stored quoted names,
  else upsert and flag `__fetch__`; one `universe_write_lock()` holder at a time; cap carry-forward) and `backfill_universe_caps()` (Phase B: `fast_info` shares × price for
  NULL caps, null-only, stops early when rate-limited). Moved out of
  `module_asymmetric_growth/engine.py` in #146 (from #217); the engine re-exports the names.
- **Reader:** `core/universe.py::classification_universe(mc_min=DEFAULT_MC_MIN)`, with
  `DEFAULT_MC_MIN = $300M`, minus the identity-master types in
  `identity.types.EXCLUDED_TYPES` (funds / ETFs, SPACs, preferred / warrants / units;
  Identity R4, #87; a name with no type stays). `universe_counts()` states the same steps
  and `universe.TYPE_GROUPS` is the funnel-row grouping of the excluded types. Types are
  read with a separate query, never a JOIN (a missing table reads as empty). Co-movement,
  Relatedness, Value Chain and Implied Probability must read this same set. Do not add a
  second `$300M` constant in a board config (#40 removed the old copies; a test enforces
  one definition).
- **Provenance:** `equity_universe_provenance` with key `__meta__` (membership), key
  `__stats__` (cap backfill) and key `__fetch__` (the last pull: `refreshed_by` is `full` or
  `partial`). Write each with `write_rows`, never `replace_all` (it would delete the others).
- **One universe writer at a time:** `universe_fetch.universe_write_lock()` (re-entrant per thread).
- **"Scanned universe" = the raw `equity_universe` count**, the same on every board.
  Per-board drops go in the funnel, never in that number.

## 5. Sector, industry and themes

- **Sector and industry come from the identity master** (`security_master`, built by `core/identity/refresh.py`).
  Two levels only (#265): the sector is one of the 11 Morningstar names (`gics_map.MORNINGSTAR_SECTORS`), the
  industry is UW's text (Yahoo's where UW has none). Precedence for the sector per ticker (#272): manual (R7) → the
  S&P 1500 Wikipedia list, for its own names only, through `gics_map.canonical_sector` (`sp1500`) → UW → Yahoo where
  UW has no usable sector → the explicit 263-code SIC table (`core/identity/sic_sector.py`, `sic_explicit`) → type
  rule (cef / bdc / bank_nonsec; ETPs stay unclassified) → `Unclassified`; `sector_source` names the step. The list's
  GICS industry names and the SIC ranges are not written. The raw SIC (`sic`, `sic_description`, `sic_group2`,
  `sic_source`) sits on `security_master` for every ticker that has one. `industry_group` / `sub_industry` stay in
  the schema and are written NULL.
- **`core/identity/apply.py` is the single writer** of `equity_universe.sector / industry / industry_group /
  sub_industry / next_earnings_date` (the date is UW's, from the `uw_sector` cache). It runs after every identity
  refresh and after every universe rebuild. Do not write sector or industry into the universe anywhere else.
- When a name looks wrongly classified, check `security_master.sector_source` and
  `identity_conflicts`. Fix it with a manual override (R7), not with a new mapping table.
- `core/gics_map.py` owns the names: the 11 **Morningstar** sector names (`MORNINGSTAR_SECTORS` / `SECTORS_11`)
  and the two-level path (`sector_path`, `GicsPath(sector, industry)`). The GICS tree, `resolve_gics`, the
  industry aliases and `core/identity/gics_tree.py` are gone (#266). `canonical_sector(raw)` maps a vendor or an
  old GICS spelling (`Information Technology`, `Health Care`, `Financials`, ...) onto the Morningstar name through
  `SECTOR_ALIASES`; that table is the only place an old spelling may appear.
- **The unknown bucket is `Unclassified`** (`gics_map.UNCLASSIFIED`; `is_unclassified()` also
  accepts the legacy `Other`). It always sorts last. `module_sec_13f` only re-exports `canonical_sector`.
- Themes: `core/taxonomy.py`. The pinned NMF coarse/medium/fine run comes first
  (`relatedness_themes` via `ticker_themes`), then the keyword fallback, then the
  sector fallback. Every ticker gets at least one theme. `Other` is always last.

## 6. Index membership

- `core/index_registry.py` → the `index_constituents` table, read via the
  `index_tickers` view. Sources are all Unusual Whales: S&P 500 (screener
  `is_s_p_500`), Dow (DIA holdings), Nasdaq Composite (exchange map).
- A completeness guard stops a partial fetch from shrinking an index.
  **Russell 2000 is deferred** (#9).
- Build: `python -m RevenueChaser.core.index_registry [--refresh]`.

## 7. Tracking

Every change maps to a GitHub issue in this repository, attached as a sub-issue of its
area epic.
