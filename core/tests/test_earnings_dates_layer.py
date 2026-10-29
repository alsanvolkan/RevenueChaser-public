"""The cards' next earnings date and the earnings-week filter read UW's calendar from ``options.duckdb``
(``option_screener_daily``, G8) first, the stored D26 calendar when the layer has no fresh session, Yahoo last."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.core import earnings_dates as ed

TODAY = date(2026, 10, 22)


def _screener(rows):
    return lambda session_date=None, passing_only=False: [dict(r) for r in rows]


def _raw_never(*a, **k):
    raise AssertionError("the stored D26 calendar is read only when the layer has no fresh session")


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
    monkeypatch.setattr(ed.rawstore, "query", _raw_never)
    assert ed.stored_next_dates(TODAY) == {"AAA": date(2026, 10, 28), "BBB": date(2026, 11, 2), "DDD": TODAY}


@pytest.mark.parametrize("session", [TODAY - timedelta(days=ed.MAX_AGE_DAYS + 1), TODAY + timedelta(days=1)])
def test_a_layer_session_outside_the_age_window_falls_back_to_the_stored_calendar(monkeypatch, session):
    from RevenueChaser.rcdata import read
    monkeypatch.setattr(read, "option_screener", _screener([
        {"session_date": session, "ticker": "AAA", "report_date": date(2026, 10, 28)}]))
    monkeypatch.setattr(ed.rawstore, "query", lambda *a, **k: [{"ticker": "ZZZ", "d": date(2026, 10, 30)}])
    assert ed.stored_next_dates(TODAY) == {"ZZZ": date(2026, 10, 30)}


def test_no_options_file_falls_back_to_the_stored_calendar(monkeypatch):
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
