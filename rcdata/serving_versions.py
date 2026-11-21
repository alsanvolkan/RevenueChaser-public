"""The serving schema version of each kind, in a module the builder's rules fingerprint does not hash (#649).

``build/dirty.py``'s ``rules_fingerprint`` hashes ``serving.py`` for every kind, so a number kept there made one
kind's bump a whole build of all six files. Kept here, a bump makes only its own kind whole, through the version
compare in ``dirty.full_rebuild_reason`` and ``serving._copy_live``. ``serving.SERVING_SCHEMA_VERSION`` is this dict.
"""
from __future__ import annotations

# The serving schema of each kind. A change to a kind's tables bumps its number in its own PR (box rule).
SERVING_SCHEMA_VERSION = {"market": 5, "spine": 10, "ownership": 5}
# market 3: ``economic_event``, UW's economic calendar by snapshot day (``build/uw_economic_calendar.py``), and
#           ``news_headline``, IBKR's Dow Jones headlines (``build/ibkr_news.py``); #643, avo's D4 of 10 Nov 2026.
# market 4: ``news_headline`` leaves; headlines live only in the News board's ``boards/news.duckdb`` (avo's option A,
#           10 Nov 2026), and the builder's ``ibkr/news`` is gone. ``build/__init__.py`` retires the table.
# market 5: ``fda_event``, UW's FDA calendar by snapshot day (``build/uw_fda_calendar.py``); #807, slice N2 of the
#           News chain (#783), avo 12 Nov 2026 "FDA kesin eklensin". A day is that day's newest fetch, so the table
#           shrinks by the source's choice like ``economic_event`` and is in ``SHRINKS_BY_CHOICE`` with it.
# ownership 2: ``sec13f_ticker_period`` gains ``started_filers``, ``started_shares`` and ``stopped_shares``, its flows
#              and new/exited holders count only the filers that reported both quarters, and ``sec13f_filer_breaks``
#              lists the filers that started or stopped reporting; ``sec13f_data_sets`` (the SEC's 13F data sets
#              as listed, ``sec/13f_sets_listed``) and ``closed`` on ``sec13f_periods`` and ``sec13f_ticker_period``:
#              a quarter closes when the set holding its deadline is published (#732, owner's decisions of 12 Nov 2026).
# ownership 3: ``uw_institution``, Unusual Whales' map of every 13F institution per snapshot day (CIK, tags,
#              ``is_hedge_fund``, the report and filing dates, total and share value), and
#              ``uw_institution_succession``, the predecessor and successor pairs of that map
#              (``build/ownership/uw_institutions.py``, #775, owner's decisions A and B of 12 Nov 2026).
# ownership 4: ``sec13f_type_holdings``, per investor type (UW's institution tags) and quarter the type's largest
#              tickers and their share of its portfolio (``build/ownership/serve.py``, #760).
# ownership 5: the buy and sell flows of ``sec13f_ticker_period`` take the quarter before on the quarter's share basis
#              (the prices store's split factors, #253), and ``sec13f_type_sector`` and ``sec13f_type_flow``, each
#              investor type's value per sector and its dollars bought and sold per quarter (#788 A2); one bump for
#              all three, one whole ownership build (owner's decision E of 16 Nov 2026). Before it went live the
#              same bump took ``sec13f_filer_style`` (each filer's turnover, top-ten share and names held, #779) and
#              ``etf_holdings_nport``, the spine ETFs' holdings from Form N-PORT (#354 C2): one build for all (avo,
#              16 Nov 2026, Q2 and Q11 of the 13F plan), and ``sec13f_filer_holdings``, each filer's largest names
#              per quarter (#928, Q16).
# spine 2: ``fund``, the SEC fund file's share classes and the ETF rule (``build/spine/sec_tickers_mf.py``).
# spine 3: ``etf_holdings``, the rotation board's ETFs read whole (``build/spine/etf_holdings.py``).
# spine 4: ``etf``, the ETF universe from Alpha Vantage and N-CEN (``build/spine/etf.py``, #346).
# spine 5: ``my_universe`` and ``my_universe_changes``, the names we keep and who entered or left
#          (``build/spine/my_universe.py``, avo's rules of 4 Nov 2026).
# spine 6: ``security.sic``, Compustat's company SIC, for the relatedness profile (#400, avo 10 Nov 2026).
# spine 7: ``etf_class`` and ``etf_class_changes``, what each ETF of ``my_universe`` is, from its N-PORT holdings
#          (``build/spine/etf_class.py``, #1031, avo's decisions of 17 Nov 2026).
# spine 8: ``etf_theme``, the map's theme funds that sit in no one GICS branch, each with the issuer's name for it
#          (``etf_class.themes``, avo's G of 18 Nov 2026); ``etf.sector_name`` leaves, a hand label nothing read.
# spine 9: ``etf.aum_source``; ``etf.aum`` is the fund's own shares x close where Compustat has it, else N-CEN's
#          series figure (#357; avo, 18 Nov 2026: funds enter and leave ``my_universe`` on the new figure).

# spine 10: ``security.country`` and ``security.country_source``, where the company's business is (ISO alpha-2):
#           Compustat's ``comp.company.loc`` where the row links to a company, else the SEC submissions header's
#           business address (avo, 21 Nov 2026: "Wrds de ulke bilgisini de al"). Asymmetric Growth's non-US test
#           reads it and keeps the SEC address as the fallback; every other table is built as before.

# PR 7 (G2): ``prices.duckdb``, the daily bars, corporate actions, shares and market cap (``build/prices/``).
SERVING_SCHEMA_VERSION["prices"] = 1
# PR 12 (G5): ``fundamentals.duckdb``, the facts, filings, earnings, estimates and checks (``build/fundamentals/``).
SERVING_SCHEMA_VERSION["fundamentals"] = 7
# fundamentals 2: ``earnings_reported``, the EPS a company reported as UW serves it, in a table of its own
#                 (``build/fundamentals/uw_earnings.py``, avo's decision of 5 Nov 2026). Bumped before the kind's
#                 first build, so no live file had to be checkpointed for it.
# fundamentals 3: the freeze of #645 (avo 10 Nov 2026, one bump for the file): ``fundamental_company`` gains the SEC
#                 header (``sic``, ``sic_description``, state of incorporation, ``business_state``,
#                 ``business_country``) and ``fundamental_fact`` the first-reported value (#277 A2); the three SEC
#                 fields (tax, pre-tax income, interest) are rows, not columns.
# fundamentals 4: ``segment_revenue`` gains ``report_tables``, the tables (role ids) of the annual report's
#                 definition linkbase in which the row's member stands on its axis (``build/fundamentals/linkbase.py``,
#                 #1048); NULL for Compustat's rows and a filing with no linkbase. Every other column is built as before.
# fundamentals 5: ``earnings_report_history``, the day each past quarter was reported: Compustat's ``rdq``
#                 (``wrds/fundq``) where it has one, UW's report day where it has none (#1181, step A of the WRDS
#                 plan; avo, 20 Nov 2026). Every other table is built as before.
# fundamentals 6: ``audit_flag``, one row per restatement, late-filing notice and director or officer change Audit
#                 Analytics recorded for a company of ours (``wrds/audit``, #1181 step C). A history about two
#                 months behind; every other table is built as before.
# fundamentals 7: ``earnings_transcript``, the turns of each company's last reported earnings call
#                 (``alphavantage/earnings_transcripts``, #1214), and ``earnings_report_history`` gains
#                 ``fiscal_year`` and ``fiscal_quarter`` -- Compustat's ``fyearq`` and ``fqtr``, NULL on a UW row.
#                 The label is the one Alpha Vantage asks a transcript by (measured 21 Nov 2026: its ``quarter``
#                 is the company's fiscal quarter), and no date arithmetic produces it for an off-calendar fiscal
#                 year. One bump for both, one whole fundamentals build. Every other table is built as before.
# PR 18 (G8): ``options.duckdb``, the option chains, exposures, prints and IV (``build/options/``).
SERVING_SCHEMA_VERSION["options"] = 2
# options 2: the calendar item's fields on ``option_screener_daily`` (and the screener's ``realized_volatility``,
#            ``stock_volume``), ``option_far_print`` (the LEAPS market scan) and ``earnings_tape`` (``/earnings/{t}``
#            of the earnings set); group 3's one structure PR (#644, avo's D2 of 10 Nov 2026).
# options 3: ``option_far_contract``, the market's far-dated contracts of a session and the next session's open
#            interest of each (``build/options/uw_far_contracts.py``, #746, avo's "A: Tum piyasa" of 12 Nov 2026).
SERVING_SCHEMA_VERSION["options"] = 3
# PR (#757): ``sec_forms.duckdb``, the sections and note blocks of the stored filings (``build/sec_forms/``). Its
# first number, not a bump: there is no live file to checkpoint.
SERVING_SCHEMA_VERSION["sec_forms"] = 1
# The SEC reader tool (plan 2026-11-15_1119, step 1): ``sec_reads.duckdb``, the answers ``build/sec_reads/`` stores.
# Its first number, not a bump: there is no live file to checkpoint.
SERVING_SCHEMA_VERSION["sec_reads"] = 1
# sec_reads 2 (plan 2026-11-15_1722, step 1): ``read_answer`` gains ``form``, ``input_kind``, ``calls``,
#             ``tokens_in``, ``tokens_out`` and ``error``. No live rows to checkpoint: no question was registered.
SERVING_SCHEMA_VERSION["sec_reads"] = 2
# sec_reads 3 (avo, 16 Nov 2026): ``read_answer`` gains ``report_date``, the period the filing reports on, so a
#             board can say which fiscal year an answer is about. No live rows to checkpoint: no question is registered.
SERVING_SCHEMA_VERSION["sec_reads"] = 3

# A table whose row count is a **chosen** list and not an accumulation, so a smaller count is the intended outcome
# of the choice changing and not a sign of a partial build; it must not need a request-wide ``allow_shrink`` (which
# would relax every other table of the kind with it). The "empties" guard still applies to all of them.
#   ``my_universe``: avo's rules plus his manual removes, so a shorter list is what he asked for.
#   ``economic_event``: UW's economic calendar is a forward-looking window ("this week and next"), fetched hourly,
#     and a snapshot day's partition is replaced whole by that day's newest fetch
#     (``build/uw_economic_calendar.py``). So an event that has rolled out of the window, or that UW dropped, is
#     gone from the day it was fetched on: the newest fetch of a day holds fewer rows than the first one, by the
#     source's choice, not ours. Read from ``RC-Build.log`` on homesrv, 14 Nov 2026: a build served 114 rows
#     against 126 live (-9.5 %), and that one refused swap failed the whole ``market`` build every hour from
#     00:05 UTC on (one serving file per kind, one swap: ``build/base.py`` ``build_kind_many``).
#   ``fda_event``: UW's FDA calendar, fetched hourly and kept the same way (``build/uw_fda_calendar.py``): a
#     snapshot day's partition is replaced whole by that day's newest fetch, so a catalyst UW dropped or that has
#     passed out of its answer is gone from the day it was fetched on. Same reason as ``economic_event``, listed
#     with it when the table was added (#807).
#   ``etf_class``: one row per ETF of ``my_universe`` (``build/spine/etf_class.py``, #1031), so it shrinks with it.
# Kept here and not in ``serving.py`` since #1031: that file is in every kind's rules fingerprint, so a table added
# to this list there was a whole build of all six kinds (the second reviewer's finding, 17 Nov 2026).
SHRINKS_BY_CHOICE = ("my_universe", "economic_event", "fda_event", "etf_class")
