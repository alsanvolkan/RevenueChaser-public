"""Tests for the 8-week price-window candle selection."""
from __future__ import annotations

from datetime import date, timedelta

from RevenueChaser.module_spx_gamma.candles import price_window_sessions, week_window_start


def _sessions(n: int, end: date = date(2026, 8, 14)) -> list[dict]:
    rows = []
    d = end
    while len(rows) < n:
        if d.weekday() < 5:
            rows.append({"date": d.isoformat(), "close": 100.0 + (n - len(rows))})
        d -= timedelta(days=1)
    rows.reverse()
    return rows


def test_week_window_start_wk40():
    # ISO week 40 of 2026 = Mon 2026-09-28 .. Sun 2026-10-04
    assert week_window_start(date(2026, 10, 2)) == date(2026, 8, 3)   # wk32
    assert week_window_start(date(2026, 10, 1)) == date(2026, 8, 3)   # wk40.7 -> still wk40


def test_week_window_start_wk41():
    assert week_window_start(date(2026, 10, 5)) == date(2026, 8, 10)  # wk41 -> wk33


def test_week_window_start_is_monday():
    for d in (date(2026, 10, 2), date(2026, 10, 5), date(2026, 8, 14)):
        assert week_window_start(d).weekday() == 0


def test_price_window_sessions_filters_to_window():
    rows = _sessions(60, date(2026, 8, 14))  # ~12 weeks of weekdays ending 08-14
    out = price_window_sessions(rows, date(2026, 8, 14))
    start = week_window_start(date(2026, 8, 14))
    assert out and out[0]["date"] == start.isoformat()
    assert out[-1]["date"] == "2026-08-14"
    assert all(r["date"] <= "2026-08-14" for r in out)


def test_price_window_sessions_excludes_future_and_missing_close():
    rows = _sessions(40, date(2026, 8, 14))
    rows.append({"date": "2026-08-17", "close": 999.0})   # future -> excluded
    rows.append({"date": "2026-08-15", "close": None})     # no close -> excluded
    out = price_window_sessions(rows, date(2026, 8, 14))
    assert all(r["date"] <= "2026-08-14" for r in out)
    assert all(r["close"] is not None for r in out)
