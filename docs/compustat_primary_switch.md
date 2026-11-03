# Compustat as the one price source: before and after

What changes on the boards when the Compustat-primary build (this branch) first serves `prices.duckdb`, so avo
can look at real data before the switch. Nothing below was measured here (a cloud builder has no WRDS and no live
store): every number marked *homesrv* is the coordinator's measurement, and every "check" is a read-only query to run
on homesrv first (`duckdb.connect(path, read_only=True)` with `RC_STORE_AUTO_ENSURE=0`).

## What moves

| Board number | Before | After | Check before the switch |
|---|---|---|---|
| Served bars, source mix (`price_bar_daily.source`) | Alpaca on its sessions, CRSP 1993-2025 where linked, else Compustat | Compustat on every day it prices (an empty `trfd` too); CRSP only on a delisted name's final days (`stkdelists`); no Alpaca row | `wrds_price_diff` row `crsp_vs_compustat`: its `close_differs` is the number of sessions whose close changes |
| History span | 1993-01-29 onwards | 1983-12-30 onwards (*homesrv*: Compustat's earliest priced day), plus the Arca listings (exchg 17, 21) | `backfill wrds/prices --dry-run`: queries and expected rows |
| Newest served session | Alpaca's (yesterday) | Compustat's newest `datadate` (*homesrv*: 2026-10-30 on 3 Nov) | `max(datadate)` of the newest `secd` partition |
| `tier_a_no_bar` | against Alpaca's newest session | against the newest served session | the count of Tier A names without a Compustat row on that day |
| Securities only CRSP holds (`permno:`, FIGI or CIK names Compustat does not price) | served from CRSP | not served: *homesrv*, 11,173 of the 18,947 security ids served from `wrds/crsp` today have no Compustat series (decision 7) | `SELECT count(DISTINCT security_id) FROM price_bar_daily WHERE source = 'wrds/crsp'` today |
| Universe with a Compustat bar (*homesrv*, priced `comp.secd` row in the last 20 sessions) | — | Tier A 5,454 of 5,655 (96.45 %; 9 history only, 192 with no gvkey/iid/CUSIP link); Tier B 1,532 of 2,015 (76.03 %; 1 history only, 482 unlinked: 205 ETPs, 157 SPACs, 70 warrants, 37 preferreds, 13 units); 9 linked names priced only on exchg 17/19/21 | `tier_a_no_bar` after the first build |
| `market_cap_daily.shares_source` | CRSP `shrout` 1993-2025, covers on Alpaca days | Compustat `cshoc` on most days | `shares_source` counts by year |
| `adj_factor` rows, adjusted closes | splits from Alpaca and `comp.sec_split`, dividends from `comp.sec_divid` | plus `ajexdi` changes; dividends from the `secd` row first | count of securities with a new split or dividend row |
| `price_gap_no_action` | 1,167 open on real splits (*homesrv*, 22 Tier A) | most resolved by the `ajexdi` splits on the next build (`ops.sync_issues`); one only CRSP explains becomes `split_only_crsp`; none deleted in code | the open count of both kinds after the first build |
| Compustat return rule (build note, `wrds_price_diff` `compustat_returns`) | `prccd/ajexdi*trfd`, none without `trfd` | the same, else price plus the day's distribution, each flagged (*homesrv*: `trfd` empty on 26.88 % of 2010-2025 rows) | the note's `Compustat day returns by rule` counts |
| Delisting days (`wrds_price_diff` `crsp_delisting_returns`) | CRSP's priceless last row dropped | `stkdelists`' delisting day served where nothing else prices it; `delret` checked | the new row's `return_differs` |
| `alpaca_close_differs` (new), `adjustment_differs` | the latter on Alpaca-served bars | both on the tester's names only | the first build's counts |
| `alpaca/bars` coverage (the 99 % Tier A acceptance) | over 7,670 names | over the at-risk names the tester picked | the run note `Alpaca tester: N of M names at risk (...)` |
| Alpaca's own corporate actions | resolved for every name with an Alpaca bar | resolved only for the tester's names, where they still win a Compustat action of the same kind and day (on purpose: Alpaca's reach 90 days ahead of Compustat's refresh); Compustat's cover the rest | — |

## Server steps, in order

1. `python -m RevenueChaser.rcdata.build backfill wrds/prices --dry-run` and read the plan.
2. The attended backfill (one Duo push; `wrds` has no day cap since #380, so one session).
3. One `prices/serving` build, after a store checkpoint. Every Alpaca row leaves `price_bar_daily` while the
   1983-1992 years and the Arca listings join it; if the net is a shrink past `serving.MAX_SHRINK`, the swap refuses
   it and the request needs `allow_shrink` once.
4. Read the numbers above against the live file kept from before, then decide the switch stays.

## Decisions only avo can make

avo, 3 Nov 2026 ~11:20 Amsterdam: "1 evet, 2 evet, 3-Alpaca test eşikleri: bunu acikla nerede kullaniyoruz? 4 CRSP
ver" (the "4" is decision 7 below).

1. **Daily Compustat: decided, yes.** `wrds/prices`'s own cadence is now daily (six queries a day); with Alpaca no
   longer served the newest served session is Compustat's. A `dataset_schedule` row on homesrv still overrides it:
   remove one that says monthly.
2. **Freshness label.** `read.DATASET_OF_TABLE` still names `alpaca/bars` as the source of `price_bar_daily`'s status;
   move it to `wrds/prices` with the cadence change, or keep it.
3. **The placeholders:** the tester's `RISK_DAYS` (10) and `LOW_PRICE` ($5).
4. **`dlycumfacpr`'s direction: checked** (*homesrv*, 3 WRDS queries): it runs as `ajexdi` does (AAPL, PERMNO 14593:
   2020-08-28 at 499.23 has 4.0 / 4.0, 2020-08-31 at 129.04 has 1.0 / 1.0), so `split_only_crsp` reads it right.
5. **`secd.exchg` is today's exchange, not the day's: decided and built (step 10).** *homesrv*: 0 of 18,024 local
   securities carry more than one `exchg`; `sec_idhist` has 013657:02 on NYSE (11) until 2013-02-10 and on Nasdaq (14)
   from 2013-02-11, while secd shows 14 on 2013-02-05..08. avo, 3 Nov 2026 ~09:50 Amsterdam: "Evet kurali uygula".
   The rule (`wrds_history.SECD_LISTED`): a secd row is served when the security was on 11, 12, 14, 17 or 21 **that
   day** by `comp.sec_idhist`'s EXCHG period holding it, or, on a day in a gap between two periods, by the previous
   period's exchange (the change is not reported yet; the delta review of #382 asked for the rule); before its first
   period (1983-1997, sec_idhist starts in 1998) by its first known exchange; with no history, or after its last
   period ended, by today's code (then the newest word on its exchange). The secd pull
   widens to every security `sec_idhist` ever lists on those exchanges, whatever its code today (a name now on OTC
   keeps its listed years); the periods come from the spine's `wrds/sec_idhist` snapshot, no new query. On the
   fixtures (not real data) the rule adds 2 securities and 4 rows against today's-code filter and drops 1 security
   and 2 rows; the first build's note `exchange history: ...` gives the real counts. **Residual risk:** a name that
   moved exchange before 1998 and ended before 1998 has no sec_idhist row, so its last exchange decides its whole
   history; a pre-1998 move of a name that lived on is read as its 1998 exchange. Priced secd rows per exchg 11 / 12
   / 14 / 17 / 21 (*homesrv*): 1984 402,251 / 111,631 / 284,113 / 506 / -; 2025 713,778 / 74,558 / 1,188,069 /
   587,399 / 257,131.
6. **The link** stays the 8-character `hdrcusip`/`cusip`/`ticker` rule (`PERMNO_LINK`); the dated
   `stksecurityinfohist.cusip9` link is the next PR.
7. **The CRSP-only securities:** 11,173 security ids lose their bars at the switch (the row above); keep them off,
   or serve CRSP for securities Compustat never priced.
