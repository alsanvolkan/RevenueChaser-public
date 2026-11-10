"""The serving schema version of each kind, in a module the builder's rules fingerprint does not hash (#649).

``build/dirty.py``'s ``rules_fingerprint`` hashes ``serving.py`` for every kind, so a number kept there made one
kind's bump a whole build of all six files. Kept here, a bump makes only its own kind whole, through the version
compare in ``dirty.full_rebuild_reason`` and ``serving._copy_live``. ``serving.SERVING_SCHEMA_VERSION`` is this dict.
"""
from __future__ import annotations

# The serving schema of each kind. A change to a kind's tables bumps its number in its own PR (box rule).
SERVING_SCHEMA_VERSION = {"market": 3, "spine": 6, "ownership": 1}
# market 3: ``economic_event``, UW's economic calendar by snapshot day (``build/uw_economic_calendar.py``), and
#           ``news_headline``, IBKR's Dow Jones headlines (``build/ibkr_news.py``); #643, avo's D4 of 10 Nov 2026.
# spine 2: ``fund``, the SEC fund file's share classes and the ETF rule (``build/spine/sec_tickers_mf.py``).
# spine 3: ``etf_holdings``, the rotation board's ETFs read whole (``build/spine/etf_holdings.py``).
# spine 4: ``etf``, the ETF universe from Alpha Vantage and N-CEN (``build/spine/etf.py``, #346).
# spine 5: ``my_universe`` and ``my_universe_changes``, the names we keep and who entered or left
#          (``build/spine/my_universe.py``, avo's rules of 4 Nov 2026).
# spine 6: ``security.sic``, Compustat's company SIC, for the relatedness profile (#400, avo 10 Nov 2026).

# PR 7 (G2): ``prices.duckdb``, the daily bars, corporate actions, shares and market cap (``build/prices/``).
SERVING_SCHEMA_VERSION["prices"] = 1
# PR 12 (G5): ``fundamentals.duckdb``, the facts, filings, earnings, estimates and checks (``build/fundamentals/``).
SERVING_SCHEMA_VERSION["fundamentals"] = 2
# fundamentals 2: ``earnings_reported``, the EPS a company reported as UW serves it, in a table of its own
#                 (``build/fundamentals/uw_earnings.py``, avo's decision of 5 Nov 2026). Bumped before the kind's
#                 first build, so no live file had to be checkpointed for it.
# PR 18 (G8): ``options.duckdb``, the option chains, exposures, prints and IV (``build/options/``).
SERVING_SCHEMA_VERSION["options"] = 1
