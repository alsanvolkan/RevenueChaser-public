"""Which names the options datasets fetch.

- **The list set** (decision D2): SPX always, and the option watch list, read at each run through
  ``rcdata.read.watched("option")`` (``ops.sqlite`` ``watchlist_option``; there is no second accessor). A name added
  to the list is in the next run's set; a name taken off it is no longer fetched, and its rows stay.
- **The earnings set** of session S: the screener-set names that report on one of the next ``REPORT_DAYS`` sessions
  (whether before the open or after the close), pass the Earnings Radar gates on S, and are not in the list set,
  which has their full chain already. Each is fetched once an evening, so a name is fetched on each of the
  ``REPORT_DAYS`` evenings before its report (avo's D1, 10 Nov 2026: the evening set widened to two days; no
  intraday mode, 11 Nov 2026).
"""
from __future__ import annotations

from datetime import date

from ....core import calendar

# Fetched whatever the list holds (newdata.OPTIONS_ALWAYS shows the same name on the Settings card).
ALWAYS = ("SPX",)
REPORT_DAYS = 2   # the next sessions whose reporters are in the earnings set (avo's D1)


def list_names() -> list[str]:
    """SPX, then the option watch list in ticker order, each once."""
    from ... import read
    return [*ALWAYS, *sorted(read.watched("option") - set(ALWAYS))]


def earnings_set(session: date, screener_rows: list[dict], on_list: set[str]) -> list[dict]:
    """The rows of ``screener_rows`` (``option_screener_daily`` of ``session``) in the earnings set, by ticker."""
    days, d = set(), session
    for _ in range(REPORT_DAYS):
        days.add(d := calendar.next_trading_day(d))
    return sorted((r for r in screener_rows
                   if r["report_date"] in days and r["passes_gates"] and r["ticker"] not in on_list),
                  key=lambda r: r["ticker"])
