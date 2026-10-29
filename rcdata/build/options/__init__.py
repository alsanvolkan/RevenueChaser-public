"""The options data layer (build plan PR 18, group G8): SPX and the option watch list, the screener set of every
reporting name, and the earnings set of the evening, served from ``options.duckdb`` (serving kind ``options``).

Three datasets, all daily and started by hand the first time:

- ``uw/option_list`` (``uw_list.py``): the chain, the option stats and the last 50 prints of SPX and every name on
  the option watch list, once a session, in the observed window.
- ``uw/option_screener`` (``uw_screener.py``): the earnings calendar of the next 28 days, the screener row of every
  name on it (100 names a call), and the earnings set: once per name, on the evening before its report, every name
  that passes the Earnings Radar gates and is not on the option list.
- ``ibkr/iv_history`` (``ibkr_iv.py``): one year of IBKR's 30-day implied volatility, once per list name.

The tables are in ``tables.py``, the reads in ``rcdata/read.py``.

**The UW call arithmetic** (the plan's: list names x (chain + stats + prints) + screener pages + earnings-set names),
per session. ``uw_calls_a_session`` is the planning figure; the real count is the builder's, per request, in
``provider_calls_daily``, and each name's own count is in the ``names`` Parquet (``tables.NAMES``). A list name takes
``LIST_CALLS`` + ``EXPIRY_CALLS`` per expiry, plus one call per extra page of a paged answer (500 rows a page); an
earnings-set name ``EARNINGS_CALLS``; the screener one call per 100 reporting names; the calendar one call per day
and report session (premarket, afterhours) plus one per extra 100 names. The planning numbers below are assumptions
until the first week of real runs replaces them (dataset_check notes and the ``names`` rows).

**Paging is part of the count, and SPX is where it shows.** A chain page holds 500 contracts, so an expiry with more
takes a second page, and one with more than 1,000 a third. The first real run (2026-10-28, the sample in PR 250)
matched the model to the call once the extra pages were counted: AAPL 23 expiries, 57 calls; NVDA 25 expiries, 61
calls; both with no extra page. SPX took 64 calls for 20 expiries: 51 without extra pages, plus 13, because its
expiries hold 446 to 1,016 contracts each. That is 207 UW calls for the run: 182 for the three list names and 25 for
the screener. So a name's count is ``list_name_calls(expiries, extra_pages)``, never ``11 + 2 x expiries`` alone,
and ``SPX_CALLS`` stays a planning ceiling. UW's ``SPX`` breakdown listed 20 expiries, the first 2026-11-20 (the
third Friday of November), so this chain holds no weekly or daily SPX expiry; the 0-1 DTE grid the SPX Gamma board
reads comes from ``uw/spot_expiry_strike``, not from this chain.
"""
from __future__ import annotations

import math

KIND = "options"

LIST_CALLS = 11       # expiry-breakdown, the nine stats parts and flow-recent: one call each (a paged one: page 0)
EXPIRY_CALLS = 2      # option-contracts (page 0) and greeks, per expiry
EARNINGS_CALLS = 5    # spot exposures by strike, GEX levels, term structure, options volume, flow-recent
CALENDAR_DAYS = 20    # trading days in the 28 calendar days ahead (DAYS_AHEAD), each asked twice
PAGE_ROWS = 100       # the screener's ticker batch and the calendar's page

# Planning numbers (assumptions, see the module doc).
EXPIRIES_A_NAME = 25          # a listed name with weeklies, a year out
EXTRA_PAGES_A_NAME = 5        # chain pages past the first of the near expiries
SPX_CALLS = 400               # SPX: about 70 expiries and several chain pages each near the money
REPORTING_NAMES = 2_500       # names on the 28-day calendar at the height of a reporting season
EARNINGS_NAMES = 150          # names reporting the next session that pass the gates, at the height of a season


def list_name_calls(expiries: int, extra_pages: int = 0) -> int:
    return LIST_CALLS + EXPIRY_CALLS * expiries + extra_pages


def uw_calls_a_session(list_names: int, *, expiries: int = EXPIRIES_A_NAME,
                       extra_pages: int = EXTRA_PAGES_A_NAME, spx_calls: int = SPX_CALLS,
                       reporting_names: int = REPORTING_NAMES, earnings_names: int = EARNINGS_NAMES) -> int:
    """UW calls of one session: ``list_names`` names on the option list besides SPX."""
    batches = math.ceil(reporting_names / PAGE_ROWS)
    return (list_names * list_name_calls(expiries, extra_pages) + spx_calls
            + batches                                    # the screener
            + 2 * CALENDAR_DAYS + batches                # the calendar: each day and session, and its extra pages
            + EARNINGS_CALLS * earnings_names)
