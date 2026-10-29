"""The UW call arithmetic of the options data (``rcdata/build/options/__init__.py``): its per-name numbers are what
the datasets send (counted at the HTTP boundary), and the planning figure stays under UW's day cap."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.rcdata import caps
from RevenueChaser.rcdata.build import base, options
from RevenueChaser.rcdata.build.options import uw_screener

from .options_fixture import SESSION, UWOptions

AFTER_CLOSE = datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def uw_env(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setattr(unusual_whales, "_sleep", lambda s: None)
    monkeypatch.setattr(calendar, "_now", lambda: AFTER_CLOSE)


def test_a_list_name_costs_the_arithmetic_s_calls(http_mock):
    mock = UWOptions()
    http_mock.add("uw.test/api", mock)
    row = base.run_request({"dataset": "uw/option_list", "params": {}})
    assert row["calls"] == options.list_name_calls(len(mock.expiries)) == mock.per_name()


def test_the_screener_side_costs_the_arithmetic_s_calls(http_mock):
    mock = UWOptions()
    nxt = calendar.next_trading_day(SESSION)
    mock.calendar[(nxt, "premarket")] = [f"N{i:03d}" for i in range(150)]
    for t in mock.calendar[(nxt, "premarket")][:3]:
        mock.screener[t] = {"call_volume": 400, "put_volume": 200, "total_open_interest": 5000,
                            "call_premium": 80_000, "put_premium": 40_000}
    http_mock.add("uw.test/api", mock)
    row = base.run_request({"dataset": uw_screener.NAME, "params": {}})
    days = len(uw_screener.report_days(SESSION))
    calendar_calls = 2 * days + 1                  # one extra page: 150 names on one day and session
    screener_calls = 2                             # 150 names, 100 a call
    assert row["calls"] == calendar_calls + screener_calls + 3 * options.EARNINGS_CALLS


def test_the_planning_figure_leaves_most_of_uw_s_day_cap_to_the_other_datasets():
    cap = caps.DAY_CAPS["uw"]
    assert options.uw_calls_a_session(0) < 2_000
    assert options.uw_calls_a_session(100) < cap / 10
    assert options.uw_calls_a_session(500) < cap / 4
    assert options.uw_calls_a_session(1) - options.uw_calls_a_session(0) == options.list_name_calls(
        options.EXPIRIES_A_NAME, options.EXTRA_PAGES_A_NAME)
