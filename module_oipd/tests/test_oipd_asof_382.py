"""M-OIPD-2 (#382): ``as_of`` is the spot candle's session, not ``date.today()``.

A Saturday Fetch whose ``ohlc_latest`` bar is Friday's gives ``as_of`` Friday and counts every DTE
from Friday; with no candle the latest complete session is used. Offline: the client is a test
double and the calendar clock is frozen.
"""
from __future__ import annotations

from datetime import datetime, timezone

from RevenueChaser.core import calendar
from RevenueChaser.module_oipd import engine, report
from RevenueChaser.module_oipd.tests.test_engine import _FakeUW

_FRIDAY = "2026-09-25"
_SATURDAY = "2026-09-26"
# Saturday 14:00 UTC = 10:00 ET; the latest complete session is Friday 2026-09-25.
_SATURDAY_NOON_UTC = datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc)


class _FridayCandleUW(_FakeUW):
    """The spot candle of Friday 2026-09-25, as a Saturday Fetch would see it."""

    def ohlc_latest(self, ticker):
        return {"close": 100.0, "date": _FRIDAY}


class _NoCandleUW(_FakeUW):
    def ohlc_latest(self, ticker):
        return {}


def test_as_of_is_the_candle_session_and_dte_is_counted_from_it(monkeypatch):
    monkeypatch.setattr(engine, "_today", lambda: _SATURDAY)
    payload = engine.run(_FridayCandleUW(), "TST")
    assert payload["asof"] == _FRIDAY
    first = payload["expiries"][0]
    # Expiries are 30 days after 2026-09-23, and 2026-10-23 - 2026-09-25 = 28 days.
    assert first["date"] == "2026-10-23" and first["dte"] == 28


def test_the_eod_label_shows_the_candle_session(monkeypatch):
    monkeypatch.setattr(engine, "_today", lambda: _SATURDAY)
    html = report.render_oipd_dashboard(engine.run(_FridayCandleUW(), "TST"))
    assert "Friday, September 25, 2026 EOD" in html


def test_no_candle_uses_the_latest_complete_session(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: _SATURDAY_NOON_UTC)
    assert engine._today() == _FRIDAY


def test_a_fetch_without_a_candle_dates_the_run_from_the_latest_complete_session(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: _SATURDAY_NOON_UTC)
    payload = engine.run(_NoCandleUW(), "TST")
    assert payload["asof"] == _FRIDAY
