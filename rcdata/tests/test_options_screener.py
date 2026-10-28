"""``uw/option_screener``, the screener set: the earnings calendar of the next 28 days and one screener row per
reporting name, 100 names a call. UW is mocked at the HTTP boundary."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from RevenueChaser.core import calendar, config
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.options import uw_screener

from .options_fixture import SESSION, UWOptions

AFTER_CLOSE = datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc)       # 17:00 New York, Monday 26 Oct 2026
REPORT_DAYS = list(base.iter_sessions(SESSION + timedelta(days=1), SESSION + timedelta(days=28)))
NEXT = calendar.next_trading_day(SESSION)
LIQUID = {"call_volume": 400, "put_volume": 200, "total_open_interest": 5000, "close": 50, "er_time": "premarket"}


@pytest.fixture(autouse=True)
def uw_env(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setattr(unusual_whales, "_sleep", lambda s: None)
    monkeypatch.setattr(calendar, "_now", lambda: AFTER_CLOSE)


@pytest.fixture
def uw_opts(http_mock):
    mock = UWOptions()
    http_mock.add("uw.test/api", mock)
    return mock


def _daily():
    return base.run_request({"dataset": uw_screener.NAME, "params": {}})


def _query(sql: str, params=()):
    with serving.open_read("options") as con:
        return con.execute(sql, list(params)).fetchall()


def _report(uw_opts, day, session, tickers, fields=None):
    uw_opts.calendar[(day, session)] = list(tickers)
    for t in tickers:
        uw_opts.screener[t] = dict(fields or LIQUID)


def test_the_calendar_covers_every_trading_day_of_the_next_28_days_in_both_sessions(uw_opts):
    _report(uw_opts, REPORT_DAYS[3], "afterhours", ["AAA"])
    _daily()
    asked = {(p.split("/")[2], q["date"][0]) for p, q in uw_opts.calls("/earnings/")}
    assert asked == {(s, d.isoformat()) for d in REPORT_DAYS for s in ("premarket", "afterhours")}
    assert all(q["limit"] == ["100"] for _, q in uw_opts.calls("/earnings/")), "UW's documented maximum"
    assert REPORT_DAYS[0] == NEXT and REPORT_DAYS[-1] <= SESSION + timedelta(days=28)


def test_the_screener_asks_100_reporting_names_a_call_and_covers_every_one(uw_opts):
    names = [f"T{i:03d}" for i in range(250)]
    _report(uw_opts, REPORT_DAYS[5], "premarket", names[:120])
    _report(uw_opts, REPORT_DAYS[9], "afterhours", names[120:])
    del uw_opts.screener["T007"]                       # UW has no screener row for it
    row = _daily()
    batches = [q["ticker"][0].split(",") for _, q in uw_opts.calls("/screener/stocks")]
    assert [len(b) for b in batches] == [100, 100, 50]
    assert sorted(t for b in batches for t in b) == names, "every reporting name is asked once"
    assert all(q["date"] == [SESSION.isoformat()] and q["limit"] == ["500"] for _, q in uw_opts.calls("/screener"))
    assert row["coverage"] == 1.0
    rows = dict(_query("SELECT ticker, screened FROM option_screener_daily"))
    assert len(rows) == 250 and rows["T007"] is False and rows["T008"] is True
    assert _query("SELECT report_date, report_session FROM option_screener_daily WHERE ticker = 'T130'") == [
        (REPORT_DAYS[9], "afterhours")]


def test_a_calendar_session_with_more_than_100_names_is_paged(uw_opts):
    _report(uw_opts, REPORT_DAYS[2], "afterhours", [f"N{i:03d}" for i in range(150)])
    _daily()
    day = [REPORT_DAYS[2].isoformat()]
    pages = [q.get("page") for p, q in uw_opts.calls("/earnings/afterhours") if q["date"] == day]
    assert pages == [None, ["1"]]
    assert _query("SELECT count(*) FROM option_screener_daily")[0][0] == 150


def test_the_gates_are_the_earnings_radar_s(uw_opts):
    """Option volume >= 500, total open interest >= 200, dollar option volume (volume x 100) >= 50k
    (core/config.py; module_earnings_radar/pipeline.py preselect)."""
    assert (config.MIN_OPTION_VOLUME, config.MIN_TOTAL_OI, config.MIN_DOLLAR_OPTION_VOLUME) == (500, 200, 50_000)
    cases = {"PASS": (300, 200, 200), "LOWVOL": (299, 200, 5000), "LOWOI": (400, 200, 199), "EDGE": (500, 0, 200)}
    uw_opts.calendar[(REPORT_DAYS[1], "premarket")] = list(cases)
    for t, (calls, puts, oi) in cases.items():
        uw_opts.screener[t] = {"call_volume": calls, "put_volume": puts, "total_open_interest": oi}
    _daily()
    got = {t: (vol, dollar, ok) for t, vol, dollar, ok in _query(
        "SELECT ticker, option_volume, dollar_option_volume, passes_gates FROM option_screener_daily")}
    assert got == {"PASS": (500, 50_000.0, True), "LOWVOL": (499, 49_900.0, False), "LOWOI": (600, 60_000.0, False),
                   "EDGE": (500, 50_000.0, True)}


def test_a_second_run_of_the_evening_asks_nothing_again(uw_opts):
    _report(uw_opts, REPORT_DAYS[4], "premarket", ["AAA", "BBB"])
    _daily()
    n = len(uw_opts.requests)
    row = _daily()
    assert len(uw_opts.requests) == n and row["calls"] == 0 and row["coverage"] == 1.0


def test_a_failed_calendar_day_holds_the_screener_until_the_next_run(uw_opts):
    base.parquet_root().mkdir(parents=True)
    _report(uw_opts, REPORT_DAYS[4], "premarket", ["AAA"])
    uw_opts.fail[f"/earnings/afterhours?date={REPORT_DAYS[6].isoformat()}"] = 503
    row = _daily()
    assert uw_opts.calls("/screener/stocks") == [] and row["coverage"] == 0.0
    assert row["next_check_due"] == SESSION.isoformat()
    uw_opts.fail.clear()
    row = _daily()
    assert len(uw_opts.calls("/screener/stocks")) == 1 and row["coverage"] == 1.0


def test_outside_the_observed_window_no_call_is_sent(uw_opts, monkeypatch):
    base.parquet_root().mkdir(parents=True)
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc))
    row = base.run_request({"dataset": uw_screener.NAME, "params": {"as_of": "2026-10-26"}})
    assert row["calls"] == 0 and uw_opts.requests == [] and "outside the observed window" in row["note"]
