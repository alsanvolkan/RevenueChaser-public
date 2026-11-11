"""The Earnings inputs sweeps (E0, #372): D26, D37, D27, D10 and D14 against fake providers.

No request leaves the process: UW goes through a ``CappedUWClient`` whose transport is a dict of canned answers,
FRED and yfinance through monkeypatched provider functions.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from RevenueChaser.core import nightly_inputs as N
from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.module_earnings_radar import inputs
from RevenueChaser.module_earnings_radar.inputs import earnings_calendar as d26

NY = ZoneInfo("America/New_York")
NIGHT = datetime(2026, 10, 13, 19, 30, tzinfo=NY).timestamp()        # session 2026-10-13, complete
SESSION = date(2026, 10, 13)
INTRADAY = datetime(2026, 10, 14, 11, 0, tzinfo=NY).timestamp()


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text, self.headers = status, body, "", {}

    def json(self):
        return self._body


class FakeUW:
    """The transport of a ``CappedUWClient``: ``routes`` maps an endpoint path to ``fn(params) -> (status, body)``."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None):
        path = url[len(unusual_whales.BASE):]
        self.calls.append((path, dict(params or {})))
        for prefix, fn in self.routes.items():
            if path == prefix or path.startswith(prefix.rstrip("*")) and prefix.endswith("*"):
                status, body = fn(params or {})
                return _Resp(status, body)
        return _Resp(404, {})

    def close(self):
        pass


def _factory(fake, key="k"):
    def make(budget):
        c = N.CappedUWClient(budget, key=key)
        c._client = fake
        return c
    return make


def _run(sweeps, tmp_path, *, fake=None, uw=200, fred=10, yf=1000, cboe=20, cftc=5, run_at=NIGHT, run_id="r1"):
    return N.run_step("earnings", sweeps, caps={"uw": uw, "fred": fred, "yfinance": yf, "cboe": cboe, "cftc": cftc},
                      run_id=run_id, run_at=run_at, log=lambda s: None, ledger_dir=tmp_path,
                      uw_factory=_factory(fake or FakeUW({})))


def _rows(table, order="1"):
    return rawstore.query(f"SELECT * FROM {table} ORDER BY {order}")


# --- D26 earnings calendar (#304) -----------------------------------------------------------------------------------

def _cal_item(symbol, **kw):
    return {"symbol": symbol, "sector": "Technology", "full_name": f"{symbol} Inc", "marketcap": "1234567890",
            "expected_move_perc": "0.051", "street_mean_est": "1.23", "pre_earnings_close": None,
            "post_earnings_close": None, "report_time": "premarket", **kw}


def _calendar_routes(by_day=None):
    """One item per day and session unless ``by_day`` says otherwise."""
    def answer(session):
        def fn(params):
            d = params["date"]
            items = (by_day or {}).get((d, session))
            if items is None:
                items = [_cal_item(f"T{d[5:7]}{d[8:]}{session[0].upper()}")]
            if isinstance(items, int):
                return items, {}
            return 200, {"data": items}
        return fn
    return {"/earnings/premarket": answer("premarket"), "/earnings/afterhours": answer("afterhours")}


def test_d26_asks_four_weeks_of_trading_days_for_both_sessions():
    days = d26.days_ahead(SESSION)
    assert days[0] == date(2026, 10, 14) and days[-1] == date(2026, 11, 10) and len(days) == 20
    assert all(d.weekday() < 5 for d in days)


def test_d26_stores_each_row_under_the_run_session(rawstore_tmp_db, tmp_path):
    fake = FakeUW(_calendar_routes())
    res = _run((d26.SWEEP,), tmp_path, fake=fake)
    assert res["sweeps"]["D26"]["status"] == "ok" and res["sweeps"]["D26"]["written"] == 40
    assert len(fake.calls) == 40 and res["calls"]["uw"]["used"] == 40
    rows = _rows("earnings_calendar", "earnings_date, report_session")
    first = rows[0]
    assert (first["session_date"], first["earnings_date"], first["report_session"]) == (
        SESSION, date(2026, 10, 14), "afterhours")
    assert first["marketcap"] == 1234567890.0 and first["street_mean_est"] == 1.23
    assert first["source"] == "uw" and first["run_id"] == "r1" and first["run_at"] == NIGHT
    assert json.loads(first["item"])["full_name"] == first["full_name"]


def test_d26_rows_key_on_session_earnings_date_ticker_and_report_session(rawstore_tmp_db, tmp_path):
    assert rawstore.MANIFEST["earnings_calendar"]["key"] == ["session_date", "earnings_date", "ticker",
                                                              "report_session"]
    both = {("2026-10-14", "premarket"): [_cal_item("AAPL")], ("2026-10-14", "afterhours"): [_cal_item("AAPL")]}
    _run((d26.SWEEP,), tmp_path, fake=FakeUW(_calendar_routes(both)))
    aapl = [r for r in _rows("earnings_calendar") if r["ticker"] == "AAPL"]
    assert sorted(r["report_session"] for r in aapl) == ["afterhours", "premarket"]


def test_d26_a_missing_field_stays_null_never_0(rawstore_tmp_db, tmp_path):
    item = _cal_item("ZZZ", sector="", expected_move_perc=None, marketcap="n/a")
    _run((d26.SWEEP,), tmp_path, fake=FakeUW(_calendar_routes({("2026-10-14", "premarket"): [item]})))
    row = next(r for r in _rows("earnings_calendar") if r["ticker"] == "ZZZ")
    assert row["sector"] is None and row["expected_move_perc"] is None and row["marketcap"] is None


def test_d26_a_second_run_of_the_night_asks_nothing_again(rawstore_tmp_db, tmp_path):
    _run((d26.SWEEP,), tmp_path, fake=FakeUW(_calendar_routes()))
    again = FakeUW(_calendar_routes())
    res = _run((d26.SWEEP,), tmp_path, fake=again)
    assert again.calls == [] and res["sweeps"]["D26"]["status"] == "ok" and res["sweeps"]["D26"]["written"] == 0


def test_d26_a_failed_day_is_partial_and_the_other_days_are_stored(rawstore_tmp_db, tmp_path):
    res = _run((d26.SWEEP,), tmp_path, fake=FakeUW(_calendar_routes({("2026-10-15", "premarket"): 404})))
    s = res["sweeps"]["D26"]
    assert s["status"] == "partial" and s["written"] == 39 and "1 of 40" in s["note"]


def test_d26_stops_at_the_uw_cap(rawstore_tmp_db, tmp_path):
    fake = FakeUW(_calendar_routes())
    res = _run((d26.SWEEP,), tmp_path, fake=fake, uw=5)
    assert len(fake.calls) == 5 and res["sweeps"]["D26"]["status"] == "partial"
    assert res["sweeps"]["D26"]["note"] == "call_cap" and res["sweeps"]["D26"]["written"] == 5


def test_d26_without_a_key_is_failed_and_sends_nothing(rawstore_tmp_db, tmp_path, monkeypatch):
    from RevenueChaser.core import credentials
    monkeypatch.setattr(credentials, "key_for", lambda provider: None)
    fake = FakeUW(_calendar_routes())
    res = N.run_step("earnings", (d26.SWEEP,), caps={"uw": 200, "fred": 0, "yfinance": 0}, run_id="r1",
                     run_at=NIGHT, log=lambda s: None, ledger_dir=tmp_path, uw_factory=_factory(fake, key=None))
    assert fake.calls == [] and res["sweeps"]["D26"]["status"] == "failed"
    assert "missing_key" in res["sweeps"]["D26"]["error"]


def test_the_step_is_registered_and_lists_its_sweeps():
    assert N.STEPS["earnings"] == "RevenueChaser.module_earnings_radar.inputs:sweeps"
    assert [s.name for s in N.load_step("earnings")] == [s.name for s in inputs.sweeps()]


def test_the_scheduled_steps_split_the_six_sweeps_once_each():
    """#620: one step per schedule; together they run every sweep of ``earnings`` exactly once."""
    split = [s.name for step in ("earnings-calendar", "short-interest", "earnings-daytime", "macro-weekly", "cftc")
             for s in N.load_step(step)]
    assert sorted(split) == sorted(s.name for s in inputs.sweeps())


# --- D27 FRED macro series (#305, FRED part) ------------------------------------------------------------------------

class _FredResp:
    def __init__(self, obs, status=200):
        self._obs, self.status_code = obs, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            req = httpx.Request("GET", "https://fred.example/obs?api_key=SECRETKEY")
            raise httpx.HTTPStatusError("bad", request=req, response=httpx.Response(self.status_code, request=req))

    def json(self):
        return {"observations": self._obs}


@pytest.fixture
def fred(monkeypatch):
    from RevenueChaser.module_earnings_radar import macro_watch
    calls, fail = [], set()
    obs = [{"date": "2026-10-13", "value": "4.05"}, {"date": "2026-10-12", "value": "."},
           {"date": "2026-07-03", "value": "4.30"}]

    def get(url, params=None, timeout=None, **kwargs):
        calls.append(params["series_id"])
        return _FredResp([] if params["series_id"] in fail else obs, 500 if params["series_id"] in fail else 200)

    monkeypatch.setattr("httpx.get", get)
    monkeypatch.setattr(macro_watch, "FRED_API_KEY", "SECRETKEY")
    return calls, fail


def test_d27_stores_each_fred_series_by_observation_date(rawstore_tmp_db, tmp_path, fred):
    from RevenueChaser.module_earnings_radar.inputs import macro as d27
    calls, _ = fred
    res = _run((d27.SWEEP,), tmp_path)
    assert calls == list(d27.SERIES) and len(calls) == 8 and res["calls"]["fred"]["used"] == 8
    assert res["sweeps"]["D27"]["status"] == "ok" and res["sweeps"]["D27"]["written"] == 16
    rows = _rows("macro_series", "series_id, obs_date")
    dgs10 = [r for r in rows if r["series_id"] == "DGS10"]
    assert [(r["obs_date"], r["value"]) for r in dgs10] == [(date(2026, 7, 3), 4.30), (date(2026, 10, 13), 4.05)]
    holiday = dgs10[0]
    assert holiday["session_date"] == date(2026, 7, 2)        # 3 July 2026 is an NYSE holiday
    assert holiday["source"] == "fred" and holiday["frequency"] == "daily" and holiday["proxy_for"] is None
    assert dgs10[1]["session_date"] == date(2026, 10, 13) and holiday["run_id"] == "r1"


def test_d27_stops_at_the_fred_cap(rawstore_tmp_db, tmp_path, fred):
    from RevenueChaser.module_earnings_radar.inputs import macro as d27
    calls, _ = fred
    res = _run((d27.SWEEP,), tmp_path, fred=3)
    assert len(calls) == 3 and res["sweeps"]["D27"]["status"] == "partial" and res["sweeps"]["D27"]["note"] == "call_cap"


def test_d27_without_a_fred_key_is_failed_and_sends_nothing(rawstore_tmp_db, tmp_path, fred, monkeypatch):
    from RevenueChaser.module_earnings_radar import macro_watch
    from RevenueChaser.module_earnings_radar.inputs import macro as d27
    monkeypatch.setattr(macro_watch, "FRED_API_KEY", "")
    calls, _ = fred
    res = _run((d27.SWEEP,), tmp_path)
    assert calls == [] and res["sweeps"]["D27"]["status"] == "failed" and "missing_key" in res["sweeps"]["D27"]["error"]
    assert res["calls"]["fred"]["used"] == 0


def test_d27_a_failed_series_never_shows_the_key(rawstore_tmp_db, tmp_path, fred):
    from RevenueChaser.module_earnings_radar.inputs import macro as d27
    _, fail = fred
    fail.add("DGS2")
    res = _run((d27.SWEEP,), tmp_path)
    s = res["sweeps"]["D27"]
    assert s["status"] == "partial" and "HTTP 500" in s["note"] and "SECRETKEY" not in json.dumps(res)


def test_the_old_fred_daily_reader_still_swallows_a_failure(fred):
    from RevenueChaser.module_earnings_radar import macro_watch
    _, fail = fred
    fail.add("DGS10")
    assert macro_watch._fred_daily("DGS10") == []
    assert macro_watch._fred_daily("DGS5") == [("2026-07-03", 4.30), ("2026-10-13", 4.05)]


# --- D10 short interest (#296) --------------------------------------------------------------------------------------

def _seed_calendar(tickers_by_day):
    rows = [{"session_date": SESSION, "earnings_date": d, "ticker": t, "report_session": "premarket", "source": "uw",
             "fetched_at": 1.0, "run_id": "seed", "run_at": 1.0}
            for d, ts in tickers_by_day.items() for t in ts]
    N.record_first("earnings_calendar", rows)


def _seed_13f(tickers):
    """The served store, which is where the sweep reads its 13F ticker set since #515."""
    import duckdb

    from RevenueChaser.rcdata import serving

    path = serving.live_path("ownership")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS sec13f_ticker_period (ticker TEXT, period DATE, shares DOUBLE, "
                    "filing_deadline DATE)")
        con.executemany("INSERT INTO sec13f_ticker_period (ticker, period, shares, filing_deadline) VALUES (?,?,?,?)",
                        [[t, date(2026, 6, 30), 1.0, date(2026, 8, 14)] for t in tickers])
    finally:
        con.close()


def _si_item(symbol, **kw):
    return {"symbol": symbol, "market_date": "2026-09-30", "short_interest": 75000, "si_float": "0.017647058",
            "total_float": 4250000, "days_to_cover": "2.1", "fee_rate": "0.2782", "rebate_rate": "3.3518",
            "short_shares_available": 10000000, "si_float_with_synth_long_pct_of_total_shares": "0.0173", **kw}


def _si_routes(screener=(), interest=(), screener_status=200):
    def screen(params):
        if screener_status != 200:
            return screener_status, {}
        asked = params["tickers"].split(",")
        return 200, {"data": [_si_item(t) for t in asked if t in screener]}

    def per_ticker(path_ticker):
        return (200, {"data": _si_item(path_ticker)}) if path_ticker in interest else (404, {})

    routes = {"/short_screener": screen}
    for t in ("A", "B", "C", "X", "Y", "Z"):
        routes[f"/shorts/{t}/interest-float/v2"] = (lambda tt: (lambda params: per_ticker(tt)))(t)
    return routes


@pytest.fixture
def yf_info(monkeypatch):
    from RevenueChaser.core.providers import yfinance as yfp
    asked = []
    infos = {"B": {"shortPercentOfFloat": 0.05, "floatShares": 1_000_000, "sharesShort": 50_000}}

    def get_info(ticker):
        asked.append(ticker)
        return {"info": infos.get(ticker, {})}

    monkeypatch.setattr(yfp, "get_info", get_info)
    return asked


def _d10():
    from RevenueChaser.module_earnings_radar.inputs import short_interest as d10
    return d10


def test_d10_screener_then_interest_float_then_yfinance(rawstore_tmp_db, tmp_path, yf_info):
    _seed_calendar({date(2026, 10, 14): ["A"], date(2026, 10, 15): ["B"], date(2026, 10, 20): ["C"]})
    _seed_13f(["B", "X", "Y"])
    fake = FakeUW(_si_routes(screener={"A", "X"}, interest={"C"}))
    res = _run((_d10().SWEEP,), tmp_path, fake=fake)
    assert fake.calls[0] == ("/short_screener", {"tickers": "A,B,C,X,Y", "limit": 500})
    assert [c[0] for c in fake.calls[1:]] == ["/shorts/B/interest-float/v2", "/shorts/C/interest-float/v2",
                                             "/shorts/Y/interest-float/v2"]
    assert yf_info == ["B"]                                  # only a calendar ticker falls back to Yahoo
    by = {r["ticker"]: r for r in _rows("short_interest")}
    assert {t: r["source"] for t, r in by.items()} == {"A": "uw_short_screener", "X": "uw_short_screener",
                                                     "C": "uw_interest_float", "B": "yfinance"}
    s = res["sweeps"]["D10"]
    assert s["status"] == "ok" and s["written"] == 4 and s["missing"] == 1
    assert res["calls"]["uw"]["used"] == 4 and res["calls"]["yfinance"]["used"] == 1


def test_d10_stores_the_uw_values_as_served_and_a_missing_field_null(rawstore_tmp_db, tmp_path, yf_info):
    _seed_calendar({date(2026, 10, 14): ["A", "B"]})
    _run((_d10().SWEEP,), tmp_path, fake=FakeUW(_si_routes(screener={"A"})))
    by = {r["ticker"]: r for r in _rows("short_interest")}
    a = by["A"]
    assert a["si_float"] == pytest.approx(0.017647058) and a["market_date"] == date(2026, 9, 30)
    assert a["short_interest"] == 75000.0 and a["days_to_cover"] == 2.1 and a["session_date"] == SESSION
    assert json.loads(a["payload"])["si_float_with_synth_long_pct_of_total_shares"] == "0.0173"
    b = by["B"]                                               # Yahoo: si_float as a fraction, no other UW field
    assert (b["si_float"], b["total_float"]) == (0.05, 1_000_000.0)
    assert b["short_interest"] is None and b["days_to_cover"] is None and b["market_date"] is None


def test_d10_calendar_tickers_come_first_inside_the_cap(rawstore_tmp_db, tmp_path, yf_info):
    _seed_calendar({date(2026, 10, 14): ["B"], date(2026, 10, 15): ["C"]})
    _seed_13f(["Y"])
    fake = FakeUW(_si_routes(interest={"B", "C", "Y"}))
    res = _run((_d10().SWEEP,), tmp_path, fake=fake, uw=2)
    assert [c[0] for c in fake.calls] == ["/short_screener", "/shorts/B/interest-float/v2"]
    s = res["sweeps"]["D10"]
    assert s["status"] == "partial" and "call_cap" in s["note"] and s["not_asked"] == 2


def test_d10_13f_tickers_never_stored_go_before_the_ones_stored_before(rawstore_tmp_db, tmp_path, yf_info):
    _seed_13f(["Y", "Z"])
    N.record_first("short_interest", [{"ticker": "Y", "session_date": date(2026, 10, 9), "source": "uw_interest_float",
                                       "fetched_at": 1.0, "run_id": "old", "run_at": 1.0}])
    fake = FakeUW(_si_routes(interest={"Y", "Z"}))
    _run((_d10().SWEEP,), tmp_path, fake=fake)
    assert [c[0] for c in fake.calls[1:]] == ["/shorts/Z/interest-float/v2", "/shorts/Y/interest-float/v2"]


def test_d10_a_second_run_of_the_night_skips_the_stored_tickers(rawstore_tmp_db, tmp_path, yf_info):
    _seed_calendar({date(2026, 10, 14): ["A", "C"]})
    _run((_d10().SWEEP,), tmp_path, fake=FakeUW(_si_routes(screener={"A"})))
    again = FakeUW(_si_routes(screener={"A"}))
    _run((_d10().SWEEP,), tmp_path, fake=again)
    assert again.calls[0] == ("/short_screener", {"tickers": "C", "limit": 500})


def test_d10_a_failed_screener_is_partial_when_interest_float_still_answers(rawstore_tmp_db, tmp_path, yf_info):
    _seed_calendar({date(2026, 10, 14): ["A"]})
    res = _run((_d10().SWEEP,), tmp_path, fake=FakeUW(_si_routes(interest={"A"}, screener_status=500)))
    s = res["sweeps"]["D10"]
    assert s["status"] == "partial" and s["written"] == 1 and "HTTP 500" in s["note"]


# --- D14 estimate snapshots (A6, #323) ------------------------------------------------------------------------------

def _trend_blocks(nan_avg=False, revisions_key="downLast7days"):
    """The four blocks exactly as yfinance's ``get_*(as_dict=True)`` gives them (``DataFrame.to_dict()``)."""
    import pandas as pd
    periods = ["0q", "+1q", "0y", "+1y"]
    ee = pd.DataFrame({"numberOfAnalysts": [30, 28, 40, 38], "avg": [float("nan") if nan_avg else 1.5, 1.6, 6.5, 7.2],
                       "low": [1.4, 1.5, 6.1, 6.6], "high": [1.7, 1.8, 7.0, 8.0], "yearAgoEps": [1.3, 1.4, 6.0, 6.5],
                       "growth": [0.15, 0.14, 0.08, 0.1], "currency": ["USD"] * 4}, index=periods)
    re_ = pd.DataFrame({"numberOfAnalysts": [25, 24, 35, 33], "avg": [9.1e10, 9.5e10, 3.9e11, 4.2e11],
                        "low": [9.0e10] * 4, "high": [9.3e10] * 4, "yearAgoRevenue": [8.5e10] * 4,
                        "growth": [0.07] * 4}, index=periods)
    et = pd.DataFrame({"current": [1.5, 1.6, 6.5, 7.2], "7daysAgo": [1.5] * 4, "30daysAgo": [1.49] * 4,
                       "60daysAgo": [1.48] * 4, "90daysAgo": [1.45] * 4}, index=periods)
    er = pd.DataFrame({"upLast7days": [2, 1, 3, 2], "upLast30days": [5, 4, 6, 5], revisions_key: [0, 0, 1, 0],
                       "downLast30days": [1, 1, 2, 1]}, index=periods)
    return {"earnings_estimate": ee.to_dict(), "revenue_estimate": re_.to_dict(), "eps_trend": et.to_dict(),
            "eps_revisions": er.to_dict()}


def _d14():
    from RevenueChaser.module_earnings_radar.inputs import estimates as d14
    return d14


def test_d14_maps_every_value_column_of_the_table():
    cols = [c.split()[0] for c in rawstore.MANIFEST["estimate_snapshots"]["columns"] if not c.startswith("PRIMARY")]
    stamps = {"ticker", "session_date", "period", "payload", "source", "fetched_at", "run_id", "run_at"}
    assert sorted(_d14().VALUE_COLUMNS) == sorted(c for c in cols if c not in stamps)
    assert len(_d14().VALUE_COLUMNS) == 21


def test_d14_the_yfinance_trend_frame_maps_onto_the_columns():
    rows = _d14().trend_rows("AAPL", _trend_blocks(revisions_key="downLast7Days"), SESSION,
                             {"run_id": "r1", "run_at": 1.0, "fetched_at": 2.0})
    assert [r["period"] for r in rows] == ["0q", "+1q", "0y", "+1y"]
    q = rows[0]
    assert (q["eps_est_analysts"], q["eps_est_avg"], q["eps_est_year_ago"], q["eps_est_growth"]) == (30.0, 1.5, 1.3, 0.15)
    assert (q["rev_est_avg"], q["rev_est_year_ago"]) == (9.1e10, 8.5e10)
    assert (q["eps_trend_current"], q["eps_trend_7d"], q["eps_trend_90d"]) == (1.5, 1.5, 1.45)
    assert (q["eps_rev_up_7d"], q["eps_rev_up_30d"], q["eps_rev_down_7d"], q["eps_rev_down_30d"]) == (2.0, 5.0, 0.0, 1.0)
    assert all(q[c] is not None for c in _d14().VALUE_COLUMNS)
    assert q["source"] == "yfinance" and q["session_date"] == SESSION and q["payload"]["earnings_estimate"]["currency"] == "USD"


def test_d14_a_missing_field_stays_null_never_0():
    blocks = _trend_blocks(nan_avg=True)
    del blocks["eps_revisions"]["upLast30days"]
    q = _d14().trend_rows("AAPL", blocks, SESSION, {"run_id": "r1", "run_at": 1.0, "fetched_at": 2.0})[0]
    assert q["eps_est_avg"] is None and q["eps_rev_up_30d"] is None and q["eps_est_low"] == 1.4
    assert q["payload"]["earnings_estimate"]["avg"] is None                  # NaN is not valid JSON


def test_d14_an_empty_answer_writes_no_row():
    assert _d14().trend_rows("ZZZ", {}, SESSION, {}) == []


@pytest.fixture
def yf_trend(monkeypatch):
    from RevenueChaser.core.providers import yfinance as yfp
    from RevenueChaser.core.providers.result import ProviderResult
    asked, answers = [], {}

    def get(ticker):
        asked.append(ticker)
        a = answers.get(ticker, "ok")
        if a == "fail":
            return ProviderResult("failed", {}, {"error": "HTTPError", "attempts": 1}, ticker)
        return ProviderResult("ok", {} if a == "empty" else _trend_blocks(), None, ticker)

    monkeypatch.setattr(yfp, "get_earnings_trend", get)
    return asked, answers


def test_d14_asks_the_calendar_tickers_nearest_first_and_stores_four_periods(rawstore_tmp_db, tmp_path, yf_trend):
    asked, answers = yf_trend
    answers["C"] = "empty"
    _seed_calendar({date(2026, 10, 20): ["C"], date(2026, 10, 14): ["B", "A"]})
    res = _run((_d14().SWEEP,), tmp_path)
    assert asked == ["A", "B", "C"] and res["calls"]["yfinance"]["used"] == 3
    s = res["sweeps"]["D14"]
    assert s["status"] == "ok" and s["written"] == 8 and s["empty"] == 1
    rows = _rows("estimate_snapshots", "ticker, period")
    assert {(r["ticker"], r["period"]) for r in rows} == {(t, p) for t in "AB" for p in ("0q", "+1q", "0y", "+1y")}
    assert json.loads(rows[0]["payload"])["eps_trend"]["current"] is not None


def test_d14_stops_at_the_yfinance_cap(rawstore_tmp_db, tmp_path, yf_trend):
    asked, _ = yf_trend
    _seed_calendar({date(2026, 10, 14): ["A", "B", "C"]})
    res = _run((_d14().SWEEP,), tmp_path, yf=2)
    assert asked == ["A", "B"] and res["sweeps"]["D14"]["status"] == "partial"
    assert res["sweeps"]["D14"]["not_asked"] == 1 and "call_cap" in res["sweeps"]["D14"]["note"]


def test_d14_stops_after_five_failures_in_a_row(rawstore_tmp_db, tmp_path, yf_trend):
    asked, answers = yf_trend
    tickers = [f"T{i}" for i in range(8)]
    answers.update({t: "fail" for t in tickers})
    _seed_calendar({date(2026, 10, 14): tickers})
    res = _run((_d14().SWEEP,), tmp_path)
    assert len(asked) == 5 and res["sweeps"]["D14"]["status"] == "failed"
    assert res["sweeps"]["D14"]["not_asked"] == 3


def test_d14_a_second_run_of_the_night_skips_the_stored_tickers(rawstore_tmp_db, tmp_path, yf_trend):
    asked, _ = yf_trend
    _seed_calendar({date(2026, 10, 14): ["A", "B"]})
    _run((_d14().SWEEP,), tmp_path)
    asked.clear()
    _run((_d14().SWEEP,), tmp_path)
    assert asked == []


def test_the_provider_reads_the_four_blocks_as_one_call(monkeypatch):
    import yfinance
    from RevenueChaser.core.providers import yfinance as yfp
    blocks = _trend_blocks()

    class T:
        def __init__(self, ticker):
            self.ticker = ticker

        def __getattr__(self, name):
            block = name[len("get_"):]
            return lambda as_dict=False: blocks[block]

    monkeypatch.setattr(yfinance, "Ticker", T)
    yfp.reset_health()
    res = yfp.get_earnings_trend("AAPL")
    assert res.status == "ok" and res.data == blocks and yfp.health()["calls"] == 1

    class Boom:
        def __init__(self, ticker):
            pass

        def get_earnings_estimate(self, as_dict=False):
            raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(yfinance, "Ticker", Boom)
    res = yfp.get_earnings_trend("AAPL")
    assert res.status == "failed" and yfp.health()["failed"] == 1


# --- the scheduled entries (#620) ---------------------------------------------------------------------------------

ALL_TABLES = ("earnings_calendar", "short_interest", "macro_series", "estimate_snapshots", "vol_index_daily",
              "cftc_cot")
CAPS = ("--max-uw-calls", "unlimited", "--max-fred-calls", "12", "--max-yf-calls", "1000", "--max-cboe-calls", "20",
        "--max-cftc-calls", "5")


@pytest.mark.parametrize("name,step,time,days", [
    ("RC-Earnings-Calendar", "earnings-calendar", "23:40", ("Monday", "Thursday")),
    ("RC-Short-Interest", "short-interest", "23:40", ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")),
    ("RC-Earnings-Daytime", "earnings-daytime", "13:00", ("Tuesday", "Thursday")),
    ("RC-Macro-Weekly", "macro-weekly", "23:40", ("Thursday",)),
    ("RC-CFTC-Weekly", "cftc", "23:40", ("Friday",)),
])
def test_each_inputs_entry_runs_its_own_step_on_its_schedule(name, step, time, days):
    from RevenueChaser.core import automations as A
    e = A.entry(name)
    assert (e.time, e.days, e.after) == (time, days, None)
    assert e.args == ("-m", "RevenueChaser.scripts.nightly_inputs", step, *CAPS)
    assert set(e.tables_written) == {t for s in N.load_step(step) for t in s.tables}
    assert e.settings.wake_to_run and not e.default_install and not e.offpeak_gated
    A.check_chain(A.REGISTRY)
    A.check_inputs_steps(A.REGISTRY)


def test_the_entries_write_every_table_once_and_the_old_entry_is_gone():
    from RevenueChaser.core import automations as A
    written = [t for e in A.REGISTRY if e.args[2:3] and e.args[:2] == ("-m", A.INPUTS_CLI) for t in e.tables_written]
    assert sorted(written) == sorted(ALL_TABLES) == sorted({t for s in inputs.sweeps() for t in s.tables})
    assert "RC-Earnings-Inputs" not in {e.name for e in A.REGISTRY}


def _vol_csv() -> str:
    return "DATE,OPEN,HIGH,LOW,CLOSE\n10/13/2026,20.0,21.0,19.0,20.5\n"


def _cftc_row() -> dict:
    return {"report_date_as_yyyy_mm_dd": "2026-10-06T00:00:00.000", "open_interest_all": "2000000",
            "asset_mgr_positions_long": "500000", "asset_mgr_positions_short": "120000",
            "lev_money_positions_long": "300000", "lev_money_positions_short": "260000",
            "dealer_positions_long_all": "90000", "dealer_positions_short_all": "420000"}


def _all_fakes(monkeypatch):
    from datetime import date as _date

    from RevenueChaser.core.providers import yfinance as yfp
    from RevenueChaser.core.providers.result import ProviderResult
    from RevenueChaser.module_earnings_radar import macro_watch
    from RevenueChaser.module_earnings_radar.inputs import cftc_cot, vol_index

    monkeypatch.setattr("httpx.get", lambda url, params=None, timeout=None, headers=None, **kwargs: _FredResp(
        [{"date": "2026-10-13", "value": "4.05"}]))
    monkeypatch.setattr(macro_watch, "FRED_API_KEY", "k")
    monkeypatch.setattr(yfp, "get_info", lambda t: {"info": {}})
    monkeypatch.setattr(yfp, "get_earnings_trend", lambda t: ProviderResult("ok", _trend_blocks(), None, t))
    # D28 and D29: no file, no Socrata read, no download leaves this fake dict.
    monkeypatch.setattr(vol_index, "fetch_csv", lambda symbol: vol_index.parse_csv(_vol_csv()))
    monkeypatch.setattr(vol_index, "download_move", lambda download=None: (
        [{"session_date": _date(2026, 10, 13), "open": 18.0, "high": 19.0, "low": 17.0, "close": 18.5}], None))
    monkeypatch.setattr(cftc_cot, "fetch_rows", lambda: ([_cftc_row()], None))
    return FakeUW({**_calendar_routes(), **_si_routes(screener={"T1014P"})})


def test_one_raising_sweep_leaves_the_others_written(rawstore_tmp_db, tmp_path, monkeypatch):
    from RevenueChaser.module_earnings_radar.inputs import cftc_cot
    fake = _all_fakes(monkeypatch)
    monkeypatch.setattr(cftc_cot, "fetch_rows", lambda: 1 / 0)
    res = _run(inputs.sweeps(), tmp_path, fake=fake)
    assert res["sweeps"]["D29"]["status"] == "failed" and "ZeroDivisionError" in res["sweeps"]["D29"]["error"]
    assert [n for n, s in res["sweeps"].items() if s["status"] != "failed"] == ["D26", "D27", "D10", "D14", "D28"]
    counts = {t: len(_rows(t)) for t in ALL_TABLES}
    assert counts["cftc_cot"] == 0 and all(counts[t] > 0 for t in ALL_TABLES if t != "cftc_cot")
    assert N.exit_code(res) == 1


def test_the_economic_calendar_left_the_inputs_sweeps():
    """avo, 11 Nov 2026 ("Raw tamamen kalkmalı"): the builder's hourly uw/economic_calendar serves it."""
    assert "economic_calendar" not in {t for s in inputs.sweeps() for t in s.tables}
    assert "D37" not in {s.name for s in inputs.sweeps()}


def test_an_intraday_clock_writes_zero_rows_and_makes_no_call(rawstore_tmp_db, tmp_path, monkeypatch):
    fake = _all_fakes(monkeypatch)
    res = _run(inputs.sweeps(), tmp_path, fake=fake, run_at=INTRADAY)
    assert res["skipped"] == "intraday" and fake.calls == [] and N.exit_code(res) == 0
    assert all(_rows(t) == [] for t in ALL_TABLES)


def test_the_uw_cap_holds_across_all_the_uw_sweeps(rawstore_tmp_db, tmp_path, monkeypatch):
    fake = _all_fakes(monkeypatch)
    res = _run(inputs.sweeps(), tmp_path, fake=fake, uw=30)
    assert len(fake.calls) == 30 and res["calls"]["uw"]["used"] == 30
    assert res["sweeps"]["D26"]["status"] == "partial" and res["sweeps"]["D37"]["status"] == "partial"
