"""Which names the options datasets fetch.

- **The list set** (decision D2): SPX always, and the option watch list, read at each run through
  ``rcdata.read.watched("option")`` (``ops.sqlite`` ``watchlist_option``; there is no second accessor). A name added
  to the list is in the next run's set; a name taken off it is no longer fetched, and its rows stay.
- **The earnings set** of session S: the screener-set names that report on the next session (the evening before
  their report, whether before the open or after the close), pass the Earnings Radar gates on S, and are not in the
  list set, which has their full chain already. Each is fetched once, on that evening.
"""
from __future__ import annotations

from datetime import date

from ....core import calendar

# Fetched whatever the list holds (newdata.OPTIONS_ALWAYS shows the same name on the Settings card).
ALWAYS = ("SPX",)


def list_names() -> list[str]:
    """SPX, then the option watch list in ticker order, each once."""
    from ... import read
    return [*ALWAYS, *sorted(read.watched("option") - set(ALWAYS))]


def earnings_set(session: date, screener_rows: list[dict], on_list: set[str]) -> list[dict]:
    """The rows of ``screener_rows`` (``option_screener_daily`` of ``session``) in the earnings set, by ticker."""
    report_day = calendar.next_trading_day(session)
    return sorted((r for r in screener_rows
                   if r["report_date"] == report_day and r["passes_gates"] and r["ticker"] not in on_list),
                  key=lambda r: r["ticker"])
