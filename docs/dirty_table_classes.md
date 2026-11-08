# What each served table is, for the dirty set

Read with `docs/data_architecture.md` ("A build's base", "What is dirty") and `rcdata/build/dirty.py`. The
incremental steps of #446 need one answer per served table before they can narrow anything:

> **Can a new source day change a row the table already holds?**

- **No** → the table is **cumulative**: a day only adds rows. What is dirty is a *range of days*
  (`Dirty(ranges=...)`); the build deletes that range and reloads it from Parquet.
- **Yes** → the table is **restated**: new data rewrites rows that are already served. What is dirty is a *key*
  (`Dirty(keys=...)`), and the whole history of that key is rebuilt — no window, because a split that goes ex
  today changes every `adj_factor` period of that security back to the first one.
- **Neither** → the table is a **derivation**: a projection of the whole store (a latest-wins snapshot, a ranking,
  a chosen list). It is rebuilt whole; it is small enough that this costs seconds.

A table can be both cumulative and restated. That is why `dirty.Dirty` has two axes: `market_cap_daily` gains a row
per session **and** has its whole range for one security rewritten when that security's share count is restated.

Every row below was read from the build code, not inferred from the table's name. The "evidence" column names the
file that decides it.

## Two dependencies that restate tables with no source day of their own

1. **The spine names the ticker.** Every ownership table gets its `ticker` from the live spine, not from its own
   source: `finra_short_interest`, `sec_ftd`, `regsho_threshold` and `ibkr_shortable` select through
   `ownership/common.symbol_map`; `insider_transactions` names the issuer through `common.issuer_tickers`
   (`ownership/form4.py`) and `beneficial_ownership` through `common.cusip_issuer_map` (`ownership/sched13dg.py`);
   and the 13F period tables resolve CUSIP → ticker from the spine's identity history (`ownership/serve.py`).
   A spine build that moves a ticker changes that column in **every** row, however old. So: *spine changed →
   these tables are wholly dirty*, whatever their own partitions say -- and a narrowed rebuild keyed on `ticker`
   would miss exactly the rows whose ticker moved, because the key itself is spine-derived.
2. **The prices universe.** `prices/serving` reads the live spine's `security` and `my_universe` to decide which
   securities the prices store serves at all (`prices/universe.py`), and its `etf` table (AUM at or over
   `ETF_MIN_AUM`, notes and closed-end funds left out) for the funds. A universe change adds a security's whole
   history and removes another's. So: *universe changed → prices is wholly dirty* (the plan's "mandatory whole
   rebuild" case).

Both are why `dirty.full_rebuild_reason` exists and why a dataset may declare one.

## market

| Table | Class | Dirty unit | Evidence |
|---|---|---|---|
| `market_series` | cumulative + restated | `obs_date` range; key `(series_id, source)` | `market_series.py`: Parquet is merged by `(series_id, obs_date, source)`, "the newest fetch winning a key" — a provider that revises a past observation rewrites that row |
| `spx_gamma_expiry_strike` | cumulative | `session_date` range | `uw_spot_expiry_strike.py`: one raw file per session, the file is the resume marker, a stored session is never re-asked |
| `spx_greek_expiry_strike` | cumulative | `session_date` range | `uw_greek_expiry_strike.py`, same shape |

## spine

The identity file stays a whole rebuild (the plan's §4): a SEC snapshot can move ticker → cik → gvkey globally, and
a ticker that changes hands touches every name that shared it (`spine/security.py`, the `_link` and `cusip_shared`
checks). Its build is 4.4-5.8 s measured, so narrowing buys nothing and risks identity.

| Table | Class | Note |
|---|---|---|
| `security`, `security_history`, `gics_history` | derivation (whole) | identity; a snapshot can rewrite any row |
| `index_membership` | derivation (whole) | built from the index lists and the history |
| `etf`, `etf_holdings`, `fund` | derivation (whole) | the fund files are read whole each build |
| `my_universe`, `my_universe_changes` | chosen list | avo's rules plus his manual removes; `serving.SHRINKS_BY_CHOICE` already treats a smaller list as intended |

## prices

| Table | Class | Dirty unit | Evidence |
|---|---|---|---|
| `price_bar_daily` | cumulative (+ restated on a revision) | `session_date` range; key `security_id` | `prices/serving.py` `KEYS`: one row per `(security_id, session_date)`; a revision is logged in `price_revision` and rewrites the bar |
| `market_cap_daily` | cumulative **and** restated | `session_date` range; key `security_id` | `MARKET_CAP` = close × shares; a new `shares_class` as-of rewrites every session after it for that security |
| `corporate_action` | cumulative + restated | `ex_date` range; key `security_id` | `CORPORATE_ACTION` carries `applied` / `not_applied_why`; a late or amended action lands in the past |
| `adj_factor` | restated, rebuilt whole | — | `ADJ_FACTOR` is `(security_id, valid_from, valid_to)` recomputed from the actions. Measured 5 Nov 2026: one security 121 ms, a day's dirty set 8.8 s, **the whole universe 9.7 s** — so no dirty-key code is written for it |
| `shares_class` | cumulative + restated | `as_of` range; key `security_id` | one row per `(security_id, measure, as_of)`; an amended filing rewrites an as-of |
| `security_symbol` | restated | key `security_id` | validity intervals `(valid_from, valid_to)`: a ticker change rewrites the interval that was open |
| `price_revision` | cumulative | `session_date` range | an append-only log of what a revision changed |
| `wrds_price_diff` | derivation (whole) | — | one comparison row set per `as_of`; a report, cheap to rebuild |

## ownership

The four source tables are rebuilt from all their `date=` partitions on every build
(`ownership/common.create_table`), and each joins the spine symbol map — see dependency 1.

| Table | Class | Dirty unit | Evidence |
|---|---|---|---|
| `finra_short_interest` | cumulative (spine-restated) | `settlement_date` range | `ownership/finra.py`: one partition per settlement date, refused unless the page count matches the header total |
| `regsho_threshold` | cumulative (spine-restated) | `list_date` range | `ownership/regsho.py` |
| `sec_ftd` | cumulative (spine-restated) | `settlement_date` range | `ownership/ftd.py`: SEC publishes half-month files |
| `ibkr_shortable` | cumulative (spine-restated) | `session_date` range | `ownership/ibkr.py` |
| `insider_transactions` | cumulative + restated (spine-restated) | `filed` range; key `ticker` (spine-derived, see dependency 1) | `ownership/form4.py`: a Form 4/A amends a filed transaction |
| `beneficial_ownership` | cumulative + restated (spine-restated) | `filed` range; key `ticker` (spine-derived, see dependency 1) | `ownership/sched13dg.py`: 13D/G amendments |
| `sec13f_filings` | cumulative | `index_day` / `filed` range | `ownership/sec13f.py`: one row per accession, with `is_amendment`, `amendment_type` and `filed`; an amendment adds a row and rewrites none. Which filing is active for a period (the amendment walk) is decided where the period tables are built, not here |
| `sec13f_filer_period`, `sec13f_ticker_period`, `sec13f_periods`, `sec13f_cusip_map` | derivation per period (spine-restated) | key `period` | `ownership/serve.py`: the amendment rule — a `NEW HOLDINGS` amendment adds to a period's set, any other amendment **replaces** it, and an amendment belongs to the period it reports on, not the quarter it was filed in. Per period it computes the filer's `value_factor` from a consensus price per CUSIP across filers, resolves CUSIP → ticker from the spine's dated identity history, then aggregates. One filer's new filing changes that period's consensus, so **the period is the unit** |

## fundamentals

| Table | Class | Dirty unit | Evidence |
|---|---|---|---|
| `fundamental_fact` | cumulative + restated | `filed` range; key `(cik, period_end)` | `fundamentals/tables.py` `FACT`: a 10-K/A rewrites a period's facts; `preliminary` rows are replaced by the filed ones |
| `filing_item` | cumulative | `filing_date` range | one row per `(cik, accession, item)`, documents and items |
| `earnings_event`, `earnings_reported` | cumulative + restated | `report_date` range; key `cik` | a calendar revision moves a future report date |
| `fundamental_revision` | cumulative | `new_as_of` range | a log of what changed between two monthly Compustat snapshots |
| `segment_revenue`, `customer_share` | restated | key `cik` | built from the newest annual report's inline XBRL and Compustat's segments; a new report rewrites the company's rows |
| `estimate_latest` | derivation (whole) | — | "the newest estimates per company, ticker and period"; the snapshots stay in Parquet |
| `fundamental_company` | derivation (whole) | — | one row per company, recomputed from the facts |
| `peer_group` | derivation (whole) | — | each company's nearest peers on the GICS ladder, ordered by the newest annual revenue: depends on every company at once |

## options

Every options table is keyed `session_date` first (`options/tables.py`), one row set per session, and no table is
rewritten by a later session. `option_eod` is additionally **windowed**: it serves only its newest sessions and
declares the first one it keeps (`serving.declare_window`), so the shrink check compares the rows both files are
meant to hold.

| Table | Class | Dirty unit |
|---|---|---|
| `option_eod` | cumulative, windowed | `session_date` range |
| `option_strike`, `option_expiry`, `option_daily`, `option_print`, `iv_daily`, `option_screener_daily`, `er_options` | cumulative | `session_date` range |

## What this means for the incremental steps

- **Cumulative tables** (most of options, the ownership sources, the price bars) are the cheap win: delete a day
  range, reload it from the partitions of those days.
- **Restated tables** need the key's whole history, and two of them are cheaper rebuilt whole than narrowed
  (`adj_factor`, measured; `estimate_latest`, `fundamental_company`, `peer_group`, `wrds_price_diff` by shape).
- **The two dependencies** above are the real risk: a spine or universe change makes tables dirty that have no new
  source day of their own. A dataset that reads the spine declares `full_rebuild_reason` rather than a narrow set.
- **13F's unit is the period**, not the day, because the consensus price and the scale fix are computed per period.
  The Parquet partition is a day, so the dirty-day → dirty-period mapping needs a column that survives into the
  served table (the `source_day` column the plan names).

Not verified here, and therefore not classified: whether any source ever **deletes** a partition it published (the
mtime signal cannot see a deletion; `dirty.py` holds that gap as a strict xfail test).
