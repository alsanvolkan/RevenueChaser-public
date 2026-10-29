"""The cards' next earnings date and the earnings-week filter read UW's calendar from ``options.duckdb``
(``option_screener_daily``, G8) first and the stored D26 calendar for the names the layer does not hold, Yahoo last.

Since the internal data API (step B) the two are a **union**, not a choice, and the age window is counted in trading
sessions (the ``earnings.next`` need's ``max_lag_sessions``) rather than calendar days, so the layer's freshness is
measured the way the rest of the store's is. Across a weekend the two units differ: 17 Oct 2026 is five calendar
days before 22 Oct but only four sessions.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.core import earnings_dates as ed

TODAY = date(2026, 10, 22)


def _screener(rows):
    return lambda session_date=None, passing_only=False: [dict(r) for r in rows]


def _raw_none(*a, **k):
    """The stored D26 calendar holding nothing, so a test about the layer reads only the layer."""
    return []


def test_a_fresh_layer_session_gives_each_names_nearest_coming_report(monkeypatch):
    from RevenueChaser.rcdata import read
    s = date(2026, 10, 21)
    monkeypatch.setattr(read, "option_screener", _screener([
        {"session_date": s, "ticker": " aaa ", "report_date": date(2026, 10, 28)},
        {"session_date": s, "ticker": "BBB", "report_date": date(2026, 11, 2)},
        {"session_date": s, "ticker": "CCC", "report_date": date(2026, 10, 20)},     # already past: no date
        {"session_date": s, "ticker": "DDD", "report_date": TODAY},
        {"session_date": s, "ticker": "EEE", "report_date": None},
        {"session_date": s, "ticker": None, "report_date": date(2026, 10, 30)}]))
    monkeypatch.setattr(ed.rawstore, "query", _raw_none)
    assert ed.stored_next_dates(TODAY) == {"AAA": date(2026, 10, 28), "BBB": date(2026, 11, 2), "DDD": TODAY}


# 22 Oct 2026 is a Thursday: 16 Oct (Friday) is four sessions back, 15 Oct five. ``MAX_AGE_SESSIONS`` is the
# ``earnings.next`` need's, the one place the window is written.
MAX_AGE_SESSIONS = 4
AT_THE_EDGE = date(2026, 10, 16)
PAST_THE_EDGE = date(2026, 10, 15)


def test_the_age_window_is_the_needs_own(monkeypatch):
    from RevenueChaser.rcdata import api
    assert api.catalog.NEEDS["earnings.next"].max_lag_sessions == MAX_AGE_SESSIONS
    assert api.lag_sessions(AT_THE_EDGE, TODAY) == MAX_AGE_SESSIONS
    assert api.lag_sessions(PAST_THE_EDGE, TODAY) == MAX_AGE_SESSIONS + 1


def test_a_session_exactly_at_the_edge_of_the_window_is_still_read(monkeypatch):
    from RevenueChaser.rcdata import read
    monkeypatch.setattr(read, "option_screener", _screener([
        {"session_date": AT_THE_EDGE, "ticker": "AAA", "report_date": date(2026, 10, 28)}]))
    monkeypatch.setattr(ed.rawstore, "query", lambda *a, **k: [{"ticker": "ZZZ", "d": date(2026, 10, 30)}])
    assert ed.stored_next_dates(TODAY) == {"AAA": date(2026, 10, 28), "ZZZ": date(2026, 10, 30)}


@pytest.mark.parametrize("session", [PAST_THE_EDGE, TODAY + timedelta(days=1)])
def test_a_layer_session_outside_the_age_window_leaves_only_the_stored_calendar(monkeypatch, session):
    """Too old, or from a run that has not happened for this day yet: the layer's rows are not read at all."""
    from RevenueChaser.rcdata import read
    monkeypatch.setattr(read, "option_screener", _screener([
        {"session_date": session, "ticker": "AAA", "report_date": date(2026, 10, 28)}]))
    monkeypatch.setattr(ed.rawstore, "query", lambda *a, **k: [{"ticker": "ZZZ", "d": date(2026, 10, 30)}])
    assert ed.stored_next_dates(TODAY) == {"ZZZ": date(2026, 10, 30)}


def test_a_name_only_the_stored_calendar_holds_is_kept_beside_the_layers(monkeypatch):
    """The short-circuit step B closes: a fresh layer session used to replace the stored calendar outright."""
    from RevenueChaser.rcdata import read
    monkeypatch.setattr(read, "option_screener", _screener([
        {"session_date": date(2026, 10, 21), "ticker": "AAA", "report_date": date(2026, 10, 28)}]))
    monkeypatch.setattr(ed.rawstore, "query", lambda *a, **k: [{"ticker": "ZZZ", "d": date(2026, 10, 30)}])
    assert ed.stored_next_dates(TODAY) == {"AAA": date(2026, 10, 28), "ZZZ": date(2026, 10, 30)}


def test_no_options_file_leaves_the_stored_calendar(monkeypatch):
    from RevenueChaser.rcdata import read
    monkeypatch.setattr(read, "option_screener", _screener([]))
    monkeypatch.setattr(ed.rawstore, "query", lambda *a, **k: [{"ticker": "ZZZ", "d": date(2026, 10, 30)}])
    assert ed.stored_next_dates(TODAY) == {"ZZZ": date(2026, 10, 30)}


def test_an_unreadable_layer_is_logged_and_falls_back_to_the_stored_calendar(monkeypatch, caplog):
    from RevenueChaser.rcdata import read

    def boom(*a, **k):
        raise RuntimeError("options.duckdb is locked")
    monkeypatch.setattr(read, "option_screener", boom)
    monkeypatch.setattr(ed.rawstore, "query", lambda *a, **k: [{"ticker": "ZZZ", "d": date(2026, 10, 30)}])
    with caplog.at_level("WARNING"):
        assert ed.stored_next_dates(TODAY) == {"ZZZ": date(2026, 10, 30)}
    assert "options.duckdb is locked" in caplog.text


# The real path (the file built from the UW mock at the HTTP boundary) is in
# ``rcdata/tests/test_options_read.py``, where the ``http_mock`` fixture lives.
