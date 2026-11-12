"""The serving schema version of each kind, in a module the builder's rules fingerprint does not hash (#649).

``build/dirty.py``'s ``rules_fingerprint`` hashes ``serving.py`` for every kind, so a number kept there made one
kind's bump a whole build of all six files. Kept here, a bump makes only its own kind whole, through the version
compare in ``dirty.full_rebuild_reason`` and ``serving._copy_live``. ``serving.SERVING_SCHEMA_VERSION`` is this dict.
"""
from __future__ import annotations

# The serving schema of each kind. A change to a kind's tables bumps its number in its own PR (box rule).
SERVING_SCHEMA_VERSION = {"market": 4, "spine": 6, "ownership": 2}
# market 3: ``economic_event``, UW's economic calendar by snapshot day (``build/uw_economic_calendar.py``), and
#           ``news_headline``, IBKR's Dow Jones headlines (``build/ibkr_news.py``); #643, avo's D4 of 10 Nov 2026.
# market 4: ``news_headline`` leaves; headlines live only in the News board's ``boards/news.duckdb`` (avo's option A,
#           10 Nov 2026), and the builder's ``ibkr/news`` is gone. ``build/__init__.py`` retires the table.
# ownership 2: ``sec13f_ticker_period`` gains ``started_filers``, ``started_shares`` and ``stopped_shares``, its flows
#              and new/exited holders count only the filers that reported both quarters, and ``sec13f_filer_breaks``
#              lists the filers that started or stopped reporting; ``sec13f_data_sets`` (the SEC's 13F data sets
#              as listed, ``sec/13f_sets_listed``) and ``closed`` on ``sec13f_periods`` and ``sec13f_ticker_period``:
#              a quarter closes when the set holding its deadline is published (#732, owner's decisions of 12 Nov 2026).
# spine 2: ``fund``, the SEC fund file's share classes and the ETF rule (``build/spine/sec_tickers_mf.py``).
# spine 3: ``etf_holdings``, the rotation board's ETFs read whole (``build/spine/etf_holdings.py``).
# spine 4: ``etf``, the ETF universe from Alpha Vantage and N-CEN (``build/spine/etf.py``, #346).
# spine 5: ``my_universe`` and ``my_universe_changes``, the names we keep and who entered or left
#          (``build/spine/my_universe.py``, avo's rules of 4 Nov 2026).
# spine 6: ``security.sic``, Compustat's company SIC, for the relatedness profile (#400, avo 10 Nov 2026).

# PR 7 (G2): ``prices.duckdb``, the daily bars, corporate actions, shares and market cap (``build/prices/``).
SERVING_SCHEMA_VERSION["prices"] = 1
# PR 12 (G5): ``fundamentals.duckdb``, the facts, filings, earnings, estimates and checks (``build/fundamentals/``).
SERVING_SCHEMA_VERSION["fundamentals"] = 3
# fundamentals 2: ``earnings_reported``, the EPS a company reported as UW serves it, in a table of its own
#                 (``build/fundamentals/uw_earnings.py``, avo's decision of 5 Nov 2026). Bumped before the kind's
#                 first build, so no live file had to be checkpointed for it.
# fundamentals 3: the freeze of #645 (avo 10 Nov 2026, one bump for the file): ``fundamental_company`` gains the SEC
#                 header (``sic``, ``sic_description``, state of incorporation, ``business_state``,
#                 ``business_country``) and ``fundamental_fact`` the first-reported value (#277 A2); the three SEC
#                 fields (tax, pre-tax income, interest) are rows, not columns.
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
