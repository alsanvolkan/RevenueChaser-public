# Glossary — dataset & field renames

Single old→new map for the Classification data-model refactor
(`docs/classification_design.md`, data model). Keep this file in sync with
`core/rawstore.py` `_RENAMES` and `MANIFEST`.

## Tables

| Old | New | Note |
|---|---|---|
| `asymmetric_universe` | `equity_universe` | spine: membership (the SEC exchange file's Nasdaq / NYSE / CBOE rows) + GICS |
| `asymmetric_universe_meta` | `equity_universe_provenance` | |
| `ticker_store` | `ticker_scores` | AG per-ticker scores |
| `ticker_cache` | `ticker_payload_cache` | raw per-ticker payload |
| `sec_facts` | `edgar_financials` | per-ticker JSON (`annual`/`quarterly`/`meta`) in `edgar.duckdb`; the `(cik, concept, period, form)` fact spine is `edgar_facts` |
| `index_members` | `index_constituents` | reference; **not pruned** |
| `ticker_registry` | `index_tickers` | becomes a **view** |
| `cusip_ticker` + `sec13f_cusip_names` | `cusip_map` | **deferred** — optional read-only VIEW later; no physical merge |
| `sec_company_tickers` + `sec_company_sic` | `sec_company` (+ child) | **deferred** — keep ticker-keyed; use read-time `_cik_map()` |
| `universe_meta` + `sec13f_universe_meta` | `sec13f_security_meta` | PK = ticker (applied) |
| `etf_holdings_cache` | `etf_holdings` | |
| `swing_info` | **(kept)** | plan's "already inside `ticker_payload_cache`" premise is **false** |

## New tables (this work)

| Table | Role |
|---|---|
| `run_registry` | runtime pins (profile mode, current theme run id, migration markers) |
| `edgar_documents` (in `.store/edgar.duckdb`) | filing **metadata** (accession/form/dates); raw HTML is `edgar_docs/{cik}/{accession}.html.gz` |
| `ticker_themes(ticker, theme_id, theme, level, theme_run_id)` | runtime theme SSoT |
| `relatedness_profiles`, `relatedness_attributes`, `relatedness_scores` | Relatedness |
| `relatedness_themes`, `relatedness_theme_tags`, `relatedness_theme_members`, `theme_runs` | NMF themes |
| `fundamentals_scores`, `swing_scores`, `fundamentals_gates`, `swing_gates` | per-ticker scores + gates |

## Status

Phase-by-phase status history (renames, merges, Phases 0-6) moved to the private notes
(`notes/glossary/glossary-status-history.md`). The tables above are the current map.
