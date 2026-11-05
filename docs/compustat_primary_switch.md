# Compustat as the one price source: before and after

What changes on the boards when the Compustat-primary build (this branch) first serves `prices.duckdb`, so avo
can look at real data before the switch. Nothing below was measured here (a cloud builder has no WRDS and no live
store): every number marked *homesrv* is the coordinator's measurement, and every "check" is a read-only query to run
on homesrv first (`duckdb.connect(path, read_only=True)` with `RC_STORE_AUTO_ENSURE=0`).

## What moves

| Board number | Before | After | Check before the switch |
|---|---|---|---|
| Served bars, source mix (`price_bar_daily.source`) | Alpaca on its sessions, CRSP 1993-2025 where linked, else Compustat | Compustat on every day it prices (an empty `trfd` too), and nothing else: no CRSP row and no Alpaca row (avo, 4 Nov 2026) | `wrds_price_diff` row `crsp_vs_compustat`: its `close_differs` is the number of sessions whose close changes |
| History span | 1993-01-29 onwards | 1983-12-30 onwards (*homesrv*: Compustat's earliest priced day), plus the Arca listings (exchg 17, 21) | `backfill wrds/prices --dry-run`: queries and expected rows |
| Newest served session | Alpaca's (yesterday) | Compustat's newest `datadate` (*homesrv*: 2026-10-30 on 3 Nov) | `max(datadate)` of the newest `secd` partition |
| `tier_a_no_bar`, `universe_etf_no_bar` (new) | Tier A against Alpaca's newest session | the stored universe's Tier A names, and its ETFs by ticker, against the newest served session (review S1 and M3 of #382: build 4's 253 counted the Tier A names the store leaves out on purpose) | the count of universe names without a Compustat row on that day |
| Securities only CRSP holds (`permno:`, FIGI or CIK names Compustat does not price) | served from CRSP | **no bar at all** (avo, 4 Nov 2026, replacing decision 7): CRSP is the cross-check only; *homesrv*, 11,173 of the 18,947 security ids served from `wrds/crsp` today have no Compustat series, and those outside today's universe leave the store besides | `SELECT count(DISTINCT security_id) FROM price_bar_daily WHERE source = 'wrds/crsp'` today |
| What is stored at all | every security a source holds, 18,947 ids from CRSP alone | today's universe only (`rcdata/build/prices/universe.py`: the spine's `my_universe` list, which #417 made live: 7,456 names on homesrv, 5 Nov 2026, the delta review of #420; else the Tier A operating types and the ETFs over 100M AUM, 5,292 + 2,071 on 4 Nov 2026), the benchmarks, rotation ETFs, watch list and the cover classes beside it; **nothing delisted** | the build note `universe: ...`, and `count(DISTINCT security_id)` of `price_bar_daily` before and after |
| Universe with a Compustat bar (*homesrv*, priced `comp.secd` row in the last 20 sessions) | — | Tier A 5,454 of 5,655 (96.45 %; 9 history only, 192 with no gvkey/iid/CUSIP link); Tier B 1,532 of 2,015 (76.03 %; 1 history only, 482 unlinked: 205 ETPs, 157 SPACs, 70 warrants, 37 preferreds, 13 units); 9 linked names priced only on exchg 17/19/21 | `tier_a_no_bar` after the first build |
| `market_cap_daily.shares_source` | CRSP `shrout` 1993-2025, covers on Alpaca days | Compustat `cshoc` on most days | `shares_source` counts by year |
| `adj_factor` rows, adjusted closes | splits from Alpaca and `comp.sec_split`, dividends from `comp.sec_divid` | plus `ajexdi` changes; dividends from the `secd` row first | count of securities with a new split or dividend row |
| `price_gap_no_action` | 1,167 open on real splits (*homesrv*, 22 Tier A) | most resolved by the `ajexdi` splits on the next build (`ops.sync_issues`); one only CRSP explains becomes `split_only_crsp`, whose detail names the served close's move ("jumps ... Compustat lacks this split" at 1.9 times or more); none deleted in code | the open count of both kinds after the first build, and the note `split_only_crsp: N within the served bars (M with a served close jump ...)`: each of the M is checked before the switch (review M6 of #382) |
| Compustat return rule (build note, `wrds_price_diff` `compustat_returns`) | `prccd/ajexdi*trfd`, none without `trfd` | the same, else price plus the day's distribution, each flagged (*homesrv*: `trfd` empty on 26.88 % of 2010-2025 rows) | the note's `Compustat day returns by rule` counts |
| Delisting days (`wrds_price_diff` `crsp_delisting_returns`) | CRSP's priceless last row dropped | the comparison is gone with the delisted names: there is no delisting day in the store to check `delret` against (`crsp_a_stock.stkdelists` is still fetched, nothing reads it) | `wrds_price_diff` holds five comparisons, not six |
| `alpaca_close_differs` (new), `adjustment_differs` | the latter on Alpaca-served bars | both on the tester's names only | the first build's counts |
| `alpaca/bars` coverage (the 99 % Tier A acceptance) | over 7,670 names | over the at-risk names the tester picked | the run note `Alpaca tester: N of M names at risk (...)` |
| Alpaca's own corporate actions | resolved for every name with an Alpaca bar | resolved only for the tester's names, where they still win a Compustat action of the same kind and day (on purpose: Alpaca's reach 90 days ahead of Compustat's refresh); Compustat's cover the rest | — |

## Server steps, in order

0. **#416 and #417 first, and one spine build on the store as it is today** (review M4 of #382). `my_universe`'s
   volume rule for a fund N-CEN gives no AUM (GLD, SLV, IBIT and the other commodity and crypto trusts) reads the
   **live** `prices.duckdb` (`spine/etf.adv_usd_20d`). Once this PR's store is live it holds only the universe, so
   such a fund that is not in `my_universe` yet has no bar to measure and stays undecided for good. Built first, the
   list takes them in from today's bars, and this PR's store then keeps them. Check before going on: the live spine
   holds `my_universe`, how many of its rows entered on `etf_adv`, and the funds the `my_universe_etf_undecided`
   issue names.
1. **A rollback copy of the live `prices.duckdb`, outside `market/`, with its sha256** (review M5). The swap keeps
   the file it replaces only as `prices.prev.duckdb`, which the next swap replaces, and `wrds/prices` now runs daily
   (decision 1), so the file from before is gone within a day. A copy left in `market/` under another
   `prices.*.duckdb` name is deleted by `serving.sweep_builds` at the next builder start. The target is
   `$RAW_STORE_BACKUP_DIR/deploy-<date>-pr382/prices.duckdb`: the folder `rcdata backup` writes to, but **not** its
   `market/` subfolder, where only the newest five copies per kind are kept (`backup.KEEP`) and the daily 04:30 run
   would rotate this one out. Before copying, `df` the target and the store: the target needs the live file's size
   free (2,895,130,624 bytes on 5 Nov 2026) with room to spare, and it must be another disk than the store's. No
   space or the same disk: stop, the deploy does not run without its rollback (review D3 of #420). Compare the copy's
   sha256 with the live file's, and write the path, both hashes and the size in the deploy log.
   **The builder stays stopped** (`life-rc-builder`, the hourly RC-Build timer, disabled today) from this step until
   step 5 is decided, and through a rollback: a builder start runs `sweep_builds`, and any build swaps over the file
   being judged. It is enabled again only after step 5 (review D2 of #420).
   **Rollback**, in order: idle `/api/jobs` and the builder stopped; check out the pre-382 `main`; copy the file back
   to `market/prices.duckdb` after its sha256 matches the log; restart the dashboard; then run one `prices/serving`
   build on that code. The file alone is not the whole state: this PR's build synced the `prices/serving` issues
   (`ops.sqlite`, Redis) after its swap, and the old build syncs them back.
2. `python -m RevenueChaser.rcdata.build backfill wrds/prices --dry-run` and read the plan.
3. The attended backfill (one Duo push; `wrds` has no day cap since #380, so one session).
4. One `prices/serving` build, after a store checkpoint. Every Alpaca row leaves `price_bar_daily` while the
   1983-1992 years and the Arca listings join it; if the net is a shrink past `serving.MAX_SHRINK`, the swap refuses
   it and the request needs `allow_shrink` once. The `crsp_unadjusted` table leaves the file by name
   (`serving.RETIRED_TABLES`, review M2); any other live table missing from the build still refuses the swap.
5. Read the numbers above against the copy from step 1, then decide the switch stays. Before it stays: every
   `split_only_crsp` issue whose detail says the served close *jumps* is checked (the note counts them; review M6),
   and the `universe_etf_no_bar` and `universe_etf_ticker_shared` funds are read (review M3 of #382, D1 of #420).
   The note `dividend_not_applied: N named, M more ...` gives the dividends that went ex before their security's first
   served bar (counted, not named); the N named ones are read (review D6 of #420).

## Decisions only avo can make

avo, 3 Nov 2026 ~11:20 Amsterdam: "1 evet, 2 evet, 3-Alpaca test eşikleri: bunu acikla nerede kullaniyoruz? 4 CRSP
ver" (the "4" is decision 7 below).

1. **Daily Compustat: decided, yes.** `wrds/prices`'s own cadence is now daily (five WRDS queries a day: the columns, then secd and sec_divid of the last two years; seven on the day the two snapshots are due, every 30 days; CRSP's final years are not re-asked); with Alpaca no
   longer served the newest served session is Compustat's. A `dataset_schedule` row on homesrv still overrides it:
   remove one that says monthly.
2. **Freshness label: decided, yes.** `read.DATASET_OF_TABLE` names `wrds/prices` as the source of
   `price_bar_daily`'s status (`security_symbol` and `market_cap_daily` still follow `alpaca/bars`).
3. **The tester's thresholds: decided, kept** (avo, 3 Nov 2026 ~11:35 Amsterdam: "3-evet"): `RISK_DAYS` = 10 and
   `LOW_PRICE` = $5. `LOW_PRICE` is a placeholder: after a month of tester issues it is replaced by the measured
   price where Compustat's and Alpaca's closes part most (the `shortcut` note in `tester.py`). Both are in
   `rcdata/build/prices/tester.py`, `at_risk()`, and decide only **which tickers the daily `alpaca/bars` run asks
   Alpaca for**; nothing served depends on them. `RISK_DAYS` (10) is the look-back in calendar days: a ticker is
   asked when, in the last 10 days, its `ajexdi` changed or a split went ex, a day had no `trfd`, a dividend or
   spin-off went ex, or a new `sec_idhist` exchange period began; an open `price_gap_no_action` issue also selects
   it. `LOW_PRICE` ($5) selects a ticker whose newest Compustat close is under $5, where a cent of rounding is a
   large return. The selected tickers' Alpaca bars are compared with Compustat's (and CRSP's) in every serving build
   and write `alpaca_close_differs` / `adjustment_differs` issues. Raising either asks more tickers (more Alpaca
   calls, within `caps.WINDOWS`' 200 a minute); lowering asks fewer.
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
7. **The CRSP-only securities: CRSP serves nothing** (avo, 4 Nov 2026, replacing his earlier "4 CRSP ver"). No
   CRSP row is a served bar, whatever Compustat has: CRSP stays the cross-check of Compustat's closes
   (`crsp_vs_compustat`), of its own day returns (`crsp_returns`) and of its splits (`CRSP_SPLITS`, the
   `split_only_crsp` issue), and no CRSP split is ever applied. A security Compustat never priced therefore has no
   bar; where it is also outside today's universe it is in no table at all. The `crsp_unadjusted` helper table and
   the adjusted view's rule that hung on it are gone with the served CRSP bars.
8. **The funds N-CEN gives no AUM (GLD, SLV, IBIT): decided, their volume from raw Compustat** (avo, 5 Nov 2026
   ~08:34 Amsterdam, option B of three; review M4 of #382, D4 of #420). `my_universe` keeps such a fund on its
   20-session dollar volume, which `spine/etf.adv_usd_20d` reads from the live prices store; once this PR's store is
   live it holds no bar of a fund outside the universe, so a new one could never be measured. The volume is to be
   read from the raw Compustat `secd` Parquet, which the universe does not cut: a spine change in its own pull
   request, not this one. Until it lands, step 0's order is what lets today's funds in. The options not taken: keep
   every unknown-AUM fund in the store as a candidate (the store outgrows the universe), or add each fund by hand in
   `my_universe_manual.json` (every new fund waits on avo).
