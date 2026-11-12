"""``etf``: the ETF universe, one row per ticker, built by ``spine/security`` from the staging of
``alphavantage/listing_status`` and ``sec/ncen`` and from the ``etp`` rows of ``security`` (#346).

**The universe** (avo, 1 Nov 2026): Alpha Vantage's active ``assetType=ETF`` rows ∪ N-CEN's ``IS_ETF=Y`` series
(with a ticker) ∪ ``security``'s ``security_type='etp'`` rows. ``source`` says which: ``both`` (AV and N-CEN),
``av``, ``ncen``, or ``etp`` (the spine alone; mostly banks' notes). Every one is stored: there is **no threshold
and no ``active`` column** here; a board's thresholds (AUM, dollar volume) are its settings, applied when it reads.

**ETNs and notes** are flagged ``is_etn`` by a name rule (:data:`ETN_NAME`), kept, and not counted as ETFs.
**Closed-end funds** are flagged ``is_cef`` the same way (avo, 1 Nov 2026): Alpha Vantage lists some as ``ETF``
(DHY), and a row the spine types ``cef`` (``security.security_type``) stays, flagged, not counted as an ETF.

**What fills which column in this PR:** the ``is_*`` fund flags and ``aum`` (``DAILY_AVG_NET_ASSETS``, else
``MONTHLY_AVG_NET_ASSETS``, with the filing's report end as ``aum_as_of``) from the series' newest N-CEN, NULL for
a row N-CEN does not hold (unknown, not false); ``inception`` from AV's ``ipoDate``; ``is_option_income`` by a name
rule; ``asset_class`` only ``commodity``, by the trust rule below; ``sector_name`` for the 11 sector SPDRs. NULL
until later PRs: ``focus``, ``country``, ``adv_usd_20d``, ``expense_ratio`` and every other ``asset_class``.

**The commodity rule.** An ``etp`` row of the spine that is no note, has no N-CEN series, and whose CIK filed no
N-CEN at all is not a registered investment company: a grantor trust or commodity pool registered under the 1933
Act only (GLD, SLV, USO, the bitcoin trusts), so ``asset_class='commodity'``. A unit investment trust does file
N-CEN (SPY and DIA as S-6 registrants, with no series row), so it is not caught. Until ``sec/ncen`` holds four
quarters (a fund files once a fiscal year) the rule says nothing: one quarter holds a fraction of the filers.
"""
from __future__ import annotations

import re

import duckdb

from ....core.rotation_reference import ROTATION_ETFS
from .. import base
from .av_listing import STAGING as AV
from . import ncen, nport
from .ncen import FILERS, STAGING as NCEN
from .nport import FUNDS as NPORT

TABLE = "etf"
SCHEMA = [("ticker", "TEXT PRIMARY KEY"), ("name", "TEXT"), ("series_id", "TEXT"), ("cik", "TEXT"),
          ("source", "TEXT"), ("is_etn", "BOOLEAN"),
          # is_cef: the spine types the ticker a closed-end fund (AV calls some CEFs ETF); kept, not counted as ETFs.
          ("is_cef", "BOOLEAN"), ("asset_class", "TEXT"), ("focus", "TEXT"), ("country", "TEXT"),
          ("is_index", "BOOLEAN"), ("is_lev_inverse", "BOOLEAN"), ("is_money_market", "BOOLEAN"),
          ("is_option_income", "BOOLEAN"), ("is_fund_of_funds", "BOOLEAN"), ("inception", "DATE"),
          # aum: N-PORT's net assets at its newest month's end (aum_source 'nport'; #354) when that month is later
          # than N-CEN's fiscal year, else N-CEN's average net assets over that year (daily, else monthly average;
          # 'ncen'), not a point-in-time AUM; aum_as_of is the month's or the year's end (review N4 of #349).
          ("aum", "DOUBLE"), ("aum_as_of", "DATE"), ("aum_source", "TEXT"), ("adv_usd_20d", "DOUBLE"),
          ("expense_ratio", "DOUBLE"),
          ("sector_name", "TEXT"),
          ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]

#: A note, by its name: ETN / ETRACS, a dated note, or a bank issuer's name (Alpha Vantage lists BMO's, UBS's,
#: Barclays' and Deutsche Bank's products under the bank's name, and the spine's etp rows carry the SEC's).
ETN_NAME = re.compile(
    r"\bETNs?\b|\bETRACS\b|\bnotes?\s+due\b|\bsenior\s+notes\b"
    r"|\b(?:bank\s+of\s+montreal|ubs\s+ag|barclays\s+bank|deutsche\s+bank\s+ag|credit\s+suisse\s+ag"
    r"|jpmorgan\s+chase\s+(?:bank|financial|&\s+co)|citigroup\s+global\s+markets|morgan\s+stanley\s+finance"
    r"|goldman\s+sachs\s+international|royal\s+bank\s+of\s+canada|canadian\s+imperial\s+bank)\b", re.I)
#: Option income, by the fund's name: a **name rule** (the plan's words: option income, covered call, buffer,
#: autocallable, defined outcome, target outcome; avo's of 1 Nov 2026: premium income, buy-write, put-write, option
#: strategy, managed floor, defined or structured protection); nothing in N-CEN says it.
OPTION_INCOME_NAME = re.compile(
    r"\boptions?\s+income\b|\bcovered\s+call|\bbuffer|\bautocallable\b|\bdefined\s+outcome\b|\btarget\s+outcome\b"
    r"|\bpremium\s+income\b|\bbuy-?write\b|\bput-?write\b|\boptions?\s+strategy\b|\bmanaged\s+floor\b"
    r"|\b(?:defined|structured)\s+protection\b", re.I)
#: Leveraged or inverse, by the fund's name: a **name rule** beside N-CEN's ``IS_MULTI_INVERSE_INDEX``, which 185
#: leveraged ``IS_ETF=Y`` series leave blank (review S1 of PR 349: Tradr 2X Long IBM, Defiance 2X Long RIOT). A
#: multiple (2x-5x, 1.25x, -1x, 1x short), ProShares' Ultra / UltraPro / UltraShort / Short (only ProShares':
#: elsewhere "Ultra" is an ultra-short bond fund, a buffer or a dividend fund, measured over the N-CEN names of
#: 1 Nov 2026), inverse, leveraged (not a leveraged-loan fund), and a daily fund that is long, short, bull or bear.
#: A bare "short" is not enough: "iShares Short Treasury Bond" is no such fund.
LEV_INVERSE_NAME = re.compile(
    r"\b(?:[2-5]|1\.\d+)\s*x\b|-\s*1\s*x\b|\b1\s*x\s+short\b"
    r"|\bproshares\s+(?:ultra|short\b)|\binverse\b|\bleveraged\b(?!\s+loans?)"
    r"|\bdaily\b.*\b(?:long|short|bull|bear)\b|\b(?:long|short|bull|bear)\b.*\bdaily\b", re.I)
#: The 11 sector SPDRs' Morningstar sector names, the ones ``engine.sector_etf_trees`` matches.
SECTOR_NAMES = {t: e["name"] for t, e in ROTATION_ETFS.items() if e.get("group") == "sector"}


def is_etn(*names) -> bool:
    return any(ETN_NAME.search(n) for n in names if n)


def is_option_income(*names) -> bool:
    return any(OPTION_INCOME_NAME.search(n) for n in names if n)


def is_lev_inverse(*names) -> bool:
    return any(LEV_INVERSE_NAME.search(n) for n in names if n)


def _flag(nc: bool, value) -> bool | None:
    return bool(value) if nc else None


def rows(ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Every ETF ticker's row, from the staging tables and the ``security`` table this build just created."""
    cur = con.execute(f"""
        WITH av AS (SELECT ticker, name, ipo_date, source_as_of, fetched_at FROM {AV}
                    WHERE asset_type = 'ETF' AND status = 'Active'
                    QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY ipo_date DESC NULLS LAST, symbol) = 1),
             nc AS (SELECT * FROM {NCEN} WHERE is_etf AND ticker IS NOT NULL
                    QUALIFY row_number() OVER (PARTITION BY ticker
                                               ORDER BY filing_date DESC NULLS LAST, accession DESC, series_id) = 1),
             etp AS (SELECT ticker, cik, name, source_as_of, fetched_at FROM security WHERE security_type = 'etp'),
             cef AS (SELECT ticker FROM security WHERE security_type = 'cef'),
             npl AS (SELECT av_ticker, series_id FROM {NPORT} WHERE av_ticker IS NOT NULL
                     QUALIFY row_number() OVER (PARTITION BY av_ticker ORDER BY report_date DESC, series_id) = 1)
        SELECT t.ticker, av.ticker IS NOT NULL AS in_av, nc.ticker IS NOT NULL AS in_nc,
               etp.ticker IS NOT NULL AS in_etp, cef.ticker IS NOT NULL AS is_cef,
               coalesce(nc.series_id, npl.series_id) AS any_series_id, np.report_date AS np_date,
               np.net_assets AS np_assets, np.equity_pct, np.bond_pct,
               av.name AS av_name, av.ipo_date, nc.fund_name, nc.series_id, nc.cik AS nc_cik, nc.is_index,
               nc.is_multi_inverse_index, nc.is_money_market, nc.is_fund_of_fund, nc.daily_avg_net_assets,
               nc.monthly_avg_net_assets, nc.report_end, etp.cik AS etp_cik, etp.name AS etp_name,
               greatest(av.source_as_of, nc.source_as_of, etp.source_as_of) AS source_as_of,
               greatest(av.fetched_at, nc.fetched_at, etp.fetched_at) AS fetched_at
        FROM (SELECT ticker FROM av UNION SELECT ticker FROM nc UNION SELECT ticker FROM etp) t
        LEFT JOIN av ON av.ticker = t.ticker LEFT JOIN nc ON nc.ticker = t.ticker
        LEFT JOIN etp ON etp.ticker = t.ticker LEFT JOIN cef ON cef.ticker = t.ticker
        LEFT JOIN npl ON npl.av_ticker = t.ticker
        LEFT JOIN {NPORT} np ON np.series_id = coalesce(nc.series_id, npl.series_id)
        ORDER BY t.ticker""")
    cols = [d[0] for d in cur.description]
    found = [dict(zip(cols, x)) for x in cur.fetchall()]      # before the next execute replaces the result
    filers = {r[0] for r in con.execute(f"SELECT DISTINCT cik FROM {FILERS}").fetchall()}
    # Fewer quarters hold a fraction of the filers: "files no N-CEN" is only known over a full year (review M1).
    year_held = ncen.quarters_held() >= ncen.BACKFILL_QUARTERS
    out = []
    for r in found:
        names = (r["fund_name"], r["av_name"], r["etp_name"])
        nc = r["in_nc"]
        etn = not nc and is_etn(*names)          # N-CEN's IS_ETF=Y is a registered fund's word: no note (N3)
        aum = r["daily_avg_net_assets"] if r["daily_avg_net_assets"] is not None else r["monthly_avg_net_assets"]
        aum_as_of, aum_source = (r["report_end"], "ncen") if aum is not None else (None, None)
        if r["np_assets"] is not None and (aum_as_of is None or r["np_date"] > aum_as_of):
            aum, aum_as_of, aum_source = r["np_assets"], r["np_date"], "nport"
        commodity = r["in_etp"] and not nc and not etn and year_held and r["etp_cik"] not in filers
        out.append({
            "ticker": r["ticker"], "name": next((n for n in names if n), None), "series_id": r["any_series_id"],
            "cik": r["nc_cik"] or r["etp_cik"],
            "source": "both" if r["in_av"] and nc else "av" if r["in_av"] else "ncen" if nc else "etp",
            "is_etn": etn, "is_cef": r["is_cef"], "asset_class": "commodity" if commodity else nport.asset_class(r["equity_pct"], r["bond_pct"])
            if r["np_date"] is not None else None, "focus": None,
            "country": None,
            "is_index": _flag(nc, r["is_index"]),
            "is_lev_inverse": True if is_lev_inverse(*names) else _flag(nc, r["is_multi_inverse_index"]),
            "is_money_market": _flag(nc, r["is_money_market"]), "is_option_income": is_option_income(*names),
            "is_fund_of_funds": _flag(nc, r["is_fund_of_fund"]), "inception": r["ipo_date"], "aum": aum,
            "aum_as_of": aum_as_of, "aum_source": aum_source, "adv_usd_20d": None, "expense_ratio": None,
            "sector_name": SECTOR_NAMES.get(r["ticker"]), "source_as_of": r["source_as_of"],
            # The newest of its sources' fetch times; run_id is the build that wrote the row (it is derived).
            "fetched_at": r["fetched_at"], "run_id": ctx.run_id})
    etns = sum(1 for r in out if r["is_etn"])
    cefs = sum(1 for r in out if r["is_cef"] and not r["is_etn"])
    ctx.notes.append(f"{len(out)} in etf: {len(out) - etns - cefs} ETFs, {etns} ETNs flagged, {cefs} CEFs flagged")
    return out
