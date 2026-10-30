"""The MAX_LAG boundary of every session-stamped stored input (E5, #372; review S1).

A stored session exactly MAX_LAG sessions behind the latest complete one is read; one session more is not, and the
run asks the provider. The sessions are written out by hand: latest complete = Wed 2026-10-14 (Mon 10-12 is a
trading day for the stock market).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar, rawstore
from RevenueChaser.module_earnings_radar import store_reads, stored_inputs

NOW = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc)      # 17:00 New York: Wed 10-14 is complete
BACK = {1: date(2026, 10, 13), 2: date(2026, 10, 12), 3: date(2026, 10, 9), 4: date(2026, 10, 8),
        5: date(2026, 10, 7), 6: date(2026, 10, 6)}           # sessions behind Wed 10-14
DAY = date(2026, 10, 19)


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


@pytest.fixture(autouse=True)
def _store(rawstore_tmp_db):
    return rawstore_tmp_db


def _calendar(session):
    rawstore.write_rows("earnings_calendar", [{
        "session_date": session, "earnings_date": DAY, "ticker": "AAA", "report_session": "premarket",
        "item": {"symbol": "AAA", "report_time": "premarket"}, "source": "uw"}])
    return stored_inputs.calendar_items([DAY])[1]


def _short(session):
    rawstore.write_rows("short_interest", [{"ticker": "AAA", "session_date": session, "source": "yfinance",
                                            "si_float": 0.1, "total_float": 1e6}])
    return stored_inputs.short_rows(["AAA"])[1]




# (reader, the spec's MAX_LAG) — written out, so a changed constant fails here rather than moving the boundary.
READERS = {"earnings_calendar": (_calendar, 1), "short_interest": (_short, 3)}


@pytest.mark.parametrize("table", sorted(READERS))
def test_a_session_max_lag_behind_is_read(table):
    read, lag = READERS[table]
    assert read(BACK[lag]) == BACK[lag]


@pytest.mark.parametrize("table", sorted(READERS))
def test_a_session_one_past_max_lag_is_not_read(table):
    read, lag = READERS[table]
    assert read(BACK[lag + 1]) is None


def test_the_lags_are_the_spec_values():
    """The boundaries above follow MAX_LAG; these are the values the spec and the model version name."""
    assert stored_inputs.MAX_LAG == {"earnings_calendar": 1, "short_interest": 3}
    # The 10-year yield left this module with the market store and the catalog owns its limit, so this value
    # follows ``macro.dgs10``. It was 4 from 30 Oct 2026: the live store measured an ordinary lag of 2 and 3 left
    # one session of room, so a single missed run read "not stored" with nothing wrong.
    assert store_reads.MAX_LAG["dgs10"] == 4
