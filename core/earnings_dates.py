"""The next earnings date a screener card shows: UW's calendar on the options layer first, then the stored D26
calendar, Yahoo's own value otherwise (#304, G6).

The cards (Asymmetric Growth, Swing, Fundamentals) and their week filter read ``quote["earningsTimestampStart"]``
(epoch seconds). The stored calendar (``earnings_calendar``, the nightly D26 sweep) covers about a third of the names
Yahoo has a date for, so a name the store has no date for keeps Yahoo's value: a hard switch would drop it from the
week filter. The overlay only changes the row a score reads, never the stored universe row.

Since G6 the first source is ``options.duckdb``'s ``option_screener_daily`` (``uw/option_screener``, G8): the same UW
calendar endpoints, every name reporting in the 28 days after its session.

Since the internal data API (step B) the two sources are **one ordered union**, not a choice. The layer is asked
through ``api.get("earnings.next")``; the stored D26 calendar then fills in every name the layer does not hold, and
Yahoo's own value stays for whatever neither holds. Before this, a fresh layer session returned early and a name
only the D26 sweep had a date for silently lost it, although the two sources cover different names.

Freshness is the catalog's and only the catalog's (avo, 29 Oct 2026): ``earnings.next``'s ``max_lag``, with the unit
it is counted in. avo set it to **one session** the same day, which settles the contradiction this module carried —
``MAX_AGE_DAYS`` said four calendar days while ``stored_inputs.MAX_LAG`` said one session for the same calendar —
in favour of the stricter one. A session older than that, or later than ``today``, is not read, so a stale sweep
never outranks Yahoo.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from RevenueChaser.core import calendar, rawstore

logger = logging.getLogger(__name__)

#: The need the layer's calendar is asked for; its ``max_lag`` is the one freshness constant.
NEED = "earnings.next"
# The D26 sweep's own window, in calendar days. Not the layer's: that one is the catalog's and nothing else's.
MAX_AGE_DAYS = 4


def next_dates_answer(today: date | None = None):
    """The ``earnings.next`` answer, with the stored D26 calendar counted in ``coverage["from_fallback"]``.

    ``rows`` are the layer's screener rows as the store holds them. The merged ``{ticker: date}`` map a card wants is
    ``stored_next_dates``; this is the same read with the envelope kept, for the cards that want to show how fresh
    the calendar is and how much of it came from the older sweep.
    """
    import dataclasses

    from RevenueChaser.rcdata import api

    today = today or calendar.today_ny()
    need = api.catalog.NEEDS[NEED]
    try:
        ans = api.get(NEED)
    except Exception as exc:  # noqa: BLE001 - the D26 store is the fallback, as before the API
        logger.warning("the options layer's earnings calendar could not be read (%s); the stored D26 calendar is "
                       "read", exc)
        ans = api.Answer(need=NEED, rows=[], state="missing", status="unreadable",
                         freshness={"as_of_session": None, "lag_sessions": None,
                                    "max_lag": need.max_lag, "unit": need.max_lag_unit},
                         coverage={"asked": 0, "served": 0, "missing": [], "from_fallback": 0}, provenance=[])
    layer = _layer_dates(ans, today, need)
    stored = _d26_dates(today)
    extra = sorted(set(stored) - set(layer))
    coverage = {**ans.coverage, "from_fallback": len(extra)}
    return dataclasses.replace(ans, coverage=coverage)


def stored_next_dates(today: date | None = None) -> dict[str, date]:
    """``{ticker: next earnings date on or after today}``: the layer's calendar, then the stored D26 one for the names
    the layer does not hold. ``{}`` when neither source answers; never an error."""
    today = today or calendar.today_ny()      # the New York date: after midnight in Amsterdam it is still yesterday there
    from RevenueChaser.rcdata import api

    need = api.catalog.NEEDS[NEED]
    try:
        ans = api.get(NEED)
        layer = _layer_dates(ans, today, need)
    except Exception as exc:  # noqa: BLE001 - the D26 store is the fallback, as before the API
        logger.warning("the options layer's earnings calendar could not be read (%s); the stored D26 calendar is "
                       "read", exc)
        layer = {}
    # The stored sweep fills the names the layer does not hold; a name both name keeps the layer's date.
    return {**_d26_dates(today), **layer}


def _layer_dates(ans, today: date, need) -> dict[str, date]:
    """``{ticker: coming report date}`` from the layer's answer, or ``{}`` when its session is too old or not yet
    reached. A session later than ``today`` is a calendar from a run that has not happened for this day yet."""
    session = ans.freshness["as_of_session"]
    if session is None or session > today:
        return {}
    from RevenueChaser.rcdata import api

    if api.lag_in(need.max_lag_unit, session, today) > need.max_lag:
        return {}
    # One row per session and ticker (the table's key), so a name has one report date in the session.
    return {str(r["ticker"]).strip().upper(): r["report_date"] for r in ans.rows
            if r.get("session_date") == session and r.get("ticker") and r.get("report_date")
            and r["report_date"] >= today}


def _d26_dates(today: date) -> dict[str, date]:
    """``{ticker: next earnings date}`` from the newest stored D26 session, ``{}`` when the store has none, a stale
    one, or cannot be read. Its writer is the dashboard, not the builder, so it is not a need of its own yet."""
    try:
        rows = rawstore.query(
            "SELECT ticker, min(earnings_date) AS d FROM earnings_calendar "
            "WHERE session_date = (SELECT max(session_date) FROM earnings_calendar "
            "WHERE session_date <= ? AND session_date >= ?) AND earnings_date >= ? GROUP BY ticker",
            [today, today - timedelta(days=MAX_AGE_DAYS), today], db="raw")
    except Exception as exc:  # noqa: BLE001 - a missing table or an unreadable store leaves the layer's dates
        logger.warning("stored earnings dates could not be read (%s); the cards keep what the layer and Yahoo have",
                       exc)
        return {}
    return {str(r["ticker"]).strip().upper(): r["d"] for r in rows if r.get("ticker") and r.get("d")}


def epoch(day: date) -> float:
    """Noon UTC of ``day``: the same calendar date in every timezone the week filter can run in."""
    return datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).timestamp()


def with_stored_date(row: dict, stored: dict[str, date]) -> dict:
    """``row`` with the stored date in its quote, or ``row`` itself when the store has none for the ticker."""
    day = stored.get(str(row.get("ticker") or "").strip().upper())
    if day is None:
        return row
    return {**row, "quote": {**(row.get("quote") or {}), "earningsTimestampStart": epoch(day)}}
