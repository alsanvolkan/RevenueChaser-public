# Compustat as the one price source: before and after

What changes on the boards when the Compustat-primary build (this branch) first serves `prices.duckdb`, so avo
can look at real data before the switch. Nothing below was measured here (a cloud builder has no WRDS and no live
store): every number marked *homesrv* is the coordinator's measurement, and every "check" is a read-only query to run
on homesrv first (`duckdb.connect(path, read_only=True)` with `RC_STORE_AUTO_ENSURE=0`).

## What moves

| Board number | Before | After | Check before the switch |
|---|---|---|---|
| Served bars, source mix (`price_bar_daily.source`) | Alpaca on its sessions, CRSP 1993-2025 where linked, else Compustat | Compustat on every day it prices (an empty `trfd` too); CRSP only on a delisted name's final days (`stkdelists`); no Alpaca row | `wrds_price_diff` row `crsp_vs_compustat`: its `close_differs` is the number of sessions whose close changes |
| History span | 1993-01-29 onwards | 1983-12-30 onwards (*homesrv*: Compustat's earliest priced day), plus the Arca listings (exchg 17, 21) | `backfill wrds/prices --dry-run`: queries, days of the 50-call cap, expected rows |
| Newest served session | Alpaca's (yesterday) | Compustat's newest `datadate` (*homesrv*: 2026-10-30 on 3 Nov) | `max(datadate)` of the newest `secd` partition |
| `tier_a_no_bar` | against Alpaca's newest session | against the newest served session | the count of Tier A names without a Compustat row on that day |
| Securities only CRSP holds (`permno:`, FIGI or CIK names Compustat does not price) | served from CRSP | not served | `SELECT count(DISTINCT security_id) FROM price_bar_daily WHERE source = 'wrds/crsp'` today |
| `market_cap_daily.shares_source` | CRSP `shrout` 1993-2025, covers on Alpaca days | Compustat `cshoc` on most days | `shares_source` counts by year |
| `adj_factor` rows, adjusted closes | splits from Alpaca and `comp.sec_split`, dividends from `comp.sec_divid` | plus `ajexdi` changes; dividends from the `secd` row first | count of securities with a new split or dividend row |
| `price_gap_no_action` | 1,167 open on real splits (*homesrv*, 22 Tier A) | most resolved by the `ajexdi` splits on the next build (`ops.sync_issues`); one only CRSP explains becomes `split_only_crsp`; none deleted in code | the open count of both kinds after the first build |
| Compustat return rule (build note, `wrds_price_diff` `compustat_returns`) | `prccd/ajexdi*trfd`, none without `trfd` | the same, else price plus the day's distribution, each flagged (*homesrv*: `trfd` empty on 26.88 % of 2010-2025 rows) | the note's `Compustat day returns by rule` counts |
| Delisting days (`wrds_price_diff` `crsp_delisting_returns`) | CRSP's priceless last row dropped | `stkdelists`' delisting day served where nothing else prices it; `delret` checked | the new row's `return_differs` |
| `alpaca_close_differs` (new), `adjustment_differs` | the latter on Alpaca-served bars | both on the tester's names only | the first build's counts |
| `alpaca/bars` coverage (the 99 % Tier A acceptance) | over 7,670 names | over the at-risk names the tester picked | the run note `Alpaca tester: N of M names at risk (...)` |
| Alpaca's own corporate actions | resolved for every name with an Alpaca bar | resolved only for the tester's names; Compustat's and CRSP's cover the rest | — |

## Server steps, in order

1. `python -m RevenueChaser.rcdata.build backfill wrds/prices --dry-run` and read the plan.
2. The attended backfill (one Duo push a session; about two to three sessions at the 50-call cap).
3. One `prices/serving` build, after a store checkpoint. Every Alpaca row leaves `price_bar_daily` while the
   1983-1992 years and the Arca listings join it; if the net is a shrink past `serving.MAX_SHRINK`, the swap refuses
   it and the request needs `allow_shrink` once.
4. Read the numbers above against the live file kept from before, then decide the switch stays.

## Decisions only avo can make

1. **Daily Compustat.** `wrds/prices` is monthly today; with Alpaca no longer served, the newest served session lags
   up to a month unless its `dataset_schedule` cadence is set to daily (six queries a day of the 50-call cap).
2. **Freshness label.** `read.DATASET_OF_TABLE` still names `alpaca/bars` as the source of `price_bar_daily`'s status;
   move it to `wrds/prices` with the cadence change, or keep it.
3. **The placeholders:** the tester's `RISK_DAYS` (10) and `LOW_PRICE` ($5).
4. **One check before reading `split_only_crsp`:** the direction of `dlycumfacpr` is the fixture's (the older days'
   factor the larger, as `ajexdi`); confirm it on one known split (AAPL's 4-for-1 ex 2020-08-31).
5. **`secd.exchg`: per day or today's code?** Neither the code nor its fixtures tell. If it is today's code, a
   security that moved exchange is kept or dropped for its whole history by its exchange now; `sec_idhist` (EXCHG
   from 1998) is the dated alternative. One query on homesrv answers it.
6. **The link** stays the 8-character `hdrcusip`/`cusip`/`ticker` rule (`PERMNO_LINK`); the dated
   `stksecurityinfohist.cusip9` link is the next PR.
