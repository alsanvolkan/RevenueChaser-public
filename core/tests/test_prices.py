"""Daily price bars (#156): the nightly writer, the fill, the read path and helpers.

Every test is offline: a fake UW client and a fake ``yf.download`` stand in at the seams,
and the clock is passed explicitly. See spec156.md §9-§10.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from RevenueChaser.core import calendar, job_lock, prices, rawstore
from RevenueChaser.core.providers.result import ProviderResult

UTC = timezone.utc
NOW = datetime(2026, 10, 8, 20, 35, tzinfo=UTC)      # 16:35 EDT, after the 16:30 complete edge
LATEST = date(2026, 10, 8)

_YF_FIELDS = ["Open", "High", "Low", "Close", "Adj Close", "Volume",
              "Dividends", "Stock Splits", "Capital Gains"]


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeUW:
    def __init__(self, bars=None, fail=(), verify_ok=True):
        self._bars = dict(bars or {})
        self._fail = set(fail)
        self._calls = 0
        self._failed = 0
        self.breaker_open = False
        self.auth_error = None
        self.verify_ok = verify_ok
        self.requests: list[tuple[str, int]] = []
        self.on_call = None

    def ohlc_daily_result(self, ticker, days=500):
        self._calls += 1
        self.requests.append((ticker, days))
        if callable(self.on_call):
            self.on_call(self, ticker)
        if ticker in self._fail:
            self._failed += 1
            return ProviderResult("failed", [], {"status": 500}, f"/stock/{ticker}/ohlc/1d")
        return ProviderResult("ok", [dict(r) for r in self._bars.get(ticker, [])])

    def health(self):
        return {"calls": self._calls, "failed": self._failed, "breaker_open": self.breaker_open,
                "auth_error": self.auth_error, "failures": [], "skipped_calls": 0}

    def verify(self):
        return (self.verify_ok, None if self.verify_ok else "missing key")


def yf_row(close, adj=None, volume=1000.0, o=None, h=None, low=None,
           div=None, split=None, gain=None):
    return {"Open": close if o is None else o, "High": close if h is None else h,
            "Low": close if low is None else low, "Close": close,
            "Adj Close": close if adj is None else adj, "Volume": volume,
            "Dividends": div, "Stock Splits": split, "Capital Gains": gain}


def make_frame(rows_by_ticker):
    dates = sorted({d for by in rows_by_ticker.values() for d in by})
    cols = {}
    for t, by in rows_by_ticker.items():
        for f in _YF_FIELDS:
            cols[(t, f)] = [by.get(d, {}).get(f) for d in dates]
    index = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    return pd.DataFrame(cols, index=index)


class FakeDownload:
    def __init__(self, rows_by_ticker=None):
        self.rows = dict(rows_by_ticker or {})
        self.calls: list[tuple] = []
        self.raise_all = False

    def __call__(self, tickers, **kw):
        self.calls.append((list(tickers), dict(kw)))
        if self.raise_all:
            raise RuntimeError("download failed")
        subset = {t: self.rows[t] for t in tickers if t in self.rows}
        if not subset:
            return pd.DataFrame()
        return make_frame(subset)


# ── store helpers ────────────────────────────────────────────────────────────

def bar_row(ticker, d, close=1.0, source="yfinance", volume=1000.0, adj_close=None,
            adj_source=None, fetched_at=0.0, run_id="seed", run_at=0.0, ohlc=None):
    dd = d if isinstance(d, date) else date.fromisoformat(d)
    o, h, low = ohlc if ohlc else (close, close, close)
    return {"ticker": ticker, "session_date": dd, "open": o, "high": h, "low": low,
            "close": close, "adj_close": adj_close, "volume": volume, "split_ratio": None,
            "adj_source": adj_source, "source": source, "fetched_at": fetched_at,
            "run_id": run_id, "run_at": run_at}


def seed_spy(last="2026-10-07", close=600.0):
    rawstore.write_rows("price_bars_daily", [bar_row("SPY", last, close=close)])
    rawstore.write_rows("price_checks", [{"dataset": "price_bars_daily",
                                           "source": "yfinance_max", "key": "SPY",
                                           "checked_at": 0.0, "run_id": "seed"}])


def seed_actions(ticker, iso_day, hh=20, mm=35):
    """An actions watermark at ``iso_day`` 20:35Z (after the 16:30 complete edge)."""
    ts = datetime.fromisoformat(iso_day).replace(hour=hh, minute=mm, tzinfo=UTC).timestamp()
    rawstore.write_rows("price_checks", [{"dataset": "price_actions", "source": "yfinance",
                                           "key": ticker, "checked_at": ts, "run_id": "seed"}])


def reset_prices():
    rawstore.clear("price_bars_daily")
    rawstore.clear("price_actions")
    rawstore.clear("price_checks")


def rows_for(ticker, d8=None):
    got = [r for r in rawstore.get_all("price_bars_daily") if r["ticker"] == ticker]
    if d8 is not None:
        got = [r for r in got if r["session_date"] == date.fromisoformat(d8)]
    return got


def session_back(latest: date, n: int) -> date:
    d = latest
    count = 0
    while count < n:
        d -= timedelta(days=1)
        if calendar.is_trading_day(d):
            count += 1
    return d


# ── T1-T12, T17-T20: the nightly writer ──────────────────────────────────────

def test_t1_nightly_writes_the_latest_complete_session(rawstore_tmp_db):
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-07", close=10.0)])
    uw = FakeUW({"AAA": [{"date": "2026-10-08", "open": 11, "high": 12, "low": 10.5,
                          "close": 11.5, "volume": 2000}]})
    dl = FakeDownload({"AAA": {"2026-10-07": yf_row(10.0),
                               "2026-10-08": yf_row(11.5, adj=11.4, volume=2000)}})

    res = prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl, now=NOW, run_id="r1")

    rows = rows_for("AAA", "2026-10-08")
    assert len(rows) == 1
    r = rows[0]
    assert r["source"] == "uw" and r["close"] == 11.5 and r["high"] == 12
    assert r["adj_close"] == 11.4 and r["adj_source"] == "yfinance"
    assert isinstance(r["session_date"], date)
    assert r["split_ratio"] is None
    assert r["run_id"] == "r1" and r["run_at"] == NOW.timestamp()
    assert res["rows_inserted"] == 1 and res["uw_ok"] == 1


def test_t2_intraday_is_not_stored_and_the_missing_session_is(rawstore_tmp_db):
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-06", close=9.0)])
    uw = FakeUW({"AAA": [{"date": "2026-10-07", "close": 9.5, "volume": 100},
                         {"date": "2026-10-08", "close": 10.0, "volume": 100}]})
    dl = FakeDownload({"AAA": {"2026-10-07": yf_row(9.5)}})
    now = datetime(2026, 10, 8, 19, 0, tzinfo=UTC)      # 15:00 EDT, session in progress

    prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl, now=now, run_id="r1")

    assert rows_for("AAA", "2026-10-08") == []
    assert [r["close"] for r in rows_for("AAA", "2026-10-07")] == [9.5]


def test_t3_a_second_run_inserts_nothing_and_never_rewrites(rawstore_tmp_db):
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-07", close=10.0)])
    uw = FakeUW({"AAA": [{"date": "2026-10-08", "close": 11.0, "volume": 100}]})
    dl = FakeDownload({"AAA": {"2026-10-07": yf_row(10.0), "2026-10-08": yf_row(11.0)}})
    prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl, now=NOW, run_id="r1")

    dl2 = FakeDownload({"AAA": {"2026-10-07": yf_row(10.0), "2026-10-08": yf_row(99.0)}})
    rawstore.clear("price_checks")         # an old actions watermark: run 2 must download again
    seed_spy()
    res = prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl2, now=NOW, run_id="r2")

    assert dl2.calls, "the second run fetched nothing, so 'never rewrites' was not exercised"
    assert res["rows_inserted"] == 0 and res["actions_inserted"] == 0
    assert rows_for("AAA", "2026-10-08")[0]["close"] == 11.0


def test_t4_uw_failure_falls_back_to_yfinance(rawstore_tmp_db):
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-07", close=10.0),
                                             bar_row("BBB", "2026-10-07", close=20.0)])
    uw = FakeUW({"AAA": [{"date": "2026-10-08", "close": 11.0, "volume": 1}]}, fail={"BBB"})
    dl = FakeDownload({"AAA": {"2026-10-08": yf_row(11.0)},
                       "BBB": {"2026-10-08": yf_row(21.0)}})

    res = prices.ensure_daily_bars(["AAA", "BBB"], uw=uw, yf_download=dl, now=NOW, run_id="r1")

    assert res["uw_failed"] == 1
    assert rows_for("BBB", "2026-10-08")[0]["source"] == "yfinance"


def test_t5_both_sources_fail_counts_and_flags(rawstore_tmp_db):
    seed_spy()
    tickers = [f"T{i:02d}" for i in range(50)]
    rawstore.write_rows("price_bars_daily", [bar_row(t, "2026-10-07", close=10.0) for t in tickers])
    uw = FakeUW({t: [{"date": "2026-10-08", "close": 10.5, "volume": 1}] for t in tickers[1:]},
                fail={tickers[0]})
    dl = FakeDownload({t: {"2026-10-08": yf_row(10.5)} for t in tickers[1:]})

    res = prices.ensure_daily_bars(tickers, uw=uw, yf_download=dl, now=NOW, run_id="r1")

    assert res["failed_both"] == 1
    assert "failed_both" in res["errors"]


def test_t6_a_failed_download_leaves_the_actions_watermark(rawstore_tmp_db):
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-07", close=10.0)])
    rawstore.write_rows("price_checks", [{"dataset": "price_actions", "source": "yfinance",
                                           "key": "AAA", "checked_at": 1000.0, "run_id": "seed"}])
    uw = FakeUW({"AAA": [{"date": "2026-10-08", "close": 11.0, "volume": 1}]})
    dl = FakeDownload({"AAA": {"2026-10-08": yf_row(11.0, adj=10.9)}})
    dl.raise_all = True

    res = prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl, now=NOW, run_id="r1")
    r = rows_for("AAA", "2026-10-08")[0]
    assert r["source"] == "uw" and r["adj_close"] is None
    assert rawstore.get_all("price_actions") == []
    marks = {m["key"]: m["checked_at"] for m in rawstore.get_all("price_checks")
             if m["dataset"] == "price_actions"}
    assert marks["AAA"] == 1000.0
    assert res["yf_failed"] == 1

    dl2 = FakeDownload({"AAA": {"2026-10-07": yf_row(10.0), "2026-10-08": yf_row(11.0)}})
    prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl2, now=NOW, run_id="r2")
    start = dl2.calls[-1][1]["start"]
    assert start <= date(2026, 9, 27)


def test_t7_actions_are_written_once(rawstore_tmp_db):
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-07", close=10.0)])
    uw = FakeUW({"AAA": [{"date": "2026-10-08", "close": 11.0, "volume": 1}]})
    frame = {"AAA": {"2026-10-07": yf_row(10.0),
                     "2026-10-08": yf_row(11.0, div=0.24, split=4.0)}}
    dl = FakeDownload(frame)

    res = prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl, now=NOW, run_id="r1")
    acts = {(a["action"], a["value"]) for a in rawstore.get_all("price_actions")}
    assert acts == {("dividend", 0.24), ("split", 4.0)}
    assert res["actions_inserted"] == 2

    res2 = prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=FakeDownload(frame),
                                    now=NOW, run_id="r2")
    assert res2["actions_inserted"] == 0
    assert len(rawstore.get_all("price_actions")) == 2


def test_t8_split_and_capital_gain_values(rawstore_tmp_db):
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-07", close=100.0)])
    uw = FakeUW({"AAA": [{"date": "2026-10-08", "close": 10.0, "volume": 1}]})
    dl = FakeDownload({"AAA": {
        "2026-10-07": yf_row(100.0, split=0.1),
        "2026-10-08": yf_row(10.0, split=0.0, gain=1.5)}})

    prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl, now=NOW, run_id="r1")

    acts = {(a["ex_date"], a["action"], a["value"]) for a in rawstore.get_all("price_actions")}
    assert acts == {(date(2026, 10, 7), "split", 0.1),
                    (date(2026, 10, 8), "capital_gain", 1.5)}


def test_t9_watermark_picks_uw_days_or_yfinance(rawstore_tmp_db):
    seed_spy()
    near = session_back(LATEST, 12)
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", near, close=10.0)])
    uw = FakeUW({"AAA": [{"date": "2026-10-08", "close": 11.0, "volume": 1}]})
    dl = FakeDownload({"AAA": {LATEST.isoformat(): yf_row(11.0)}})

    prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl, now=NOW, run_id="r1")
    assert uw.requests == [("AAA", 16)]

    rawstore.clear("price_bars_daily")
    seed_spy()
    far = session_back(LATEST, 300)
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", far, close=10.0)])
    uw2 = FakeUW({"AAA": []})
    dl2 = FakeDownload({"AAA": {far.isoformat(): yf_row(10.0),
                                LATEST.isoformat(): yf_row(11.0)}})
    prices.ensure_daily_bars(["AAA"], uw=uw2, yf_download=dl2, now=NOW, run_id="r2")
    assert uw2.requests == []
    assert dl2.calls[0][1]["start"] <= far + timedelta(days=1)


def test_t10_weekend_and_holiday_make_no_calls(rawstore_tmp_db):
    seed_spy(last="2026-10-09")
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-09", close=10.0)])
    seed_actions("AAA", "2026-10-09")
    uw = FakeUW({"AAA": []})
    dl = FakeDownload({"AAA": {}})
    sat = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    res = prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl, now=sat, run_id="r1")
    assert uw.requests == [] and dl.calls == [] and res["rows_inserted"] == 0

    reset_prices()
    seed_spy(last="2026-11-25")
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-11-25", close=10.0)])
    seed_actions("AAA", "2026-11-25", hh=21, mm=35)
    uw2 = FakeUW({"AAA": []})
    dl2 = FakeDownload({"AAA": {}})
    turkey = datetime(2026, 11, 26, 18, 0, tzinfo=UTC)
    res2 = prices.ensure_daily_bars(["AAA"], uw=uw2, yf_download=dl2, now=turkey, run_id="r2")
    assert uw2.requests == [] and dl2.calls == [] and res2["rows_inserted"] == 0


def test_t11_uw_call_cap(rawstore_tmp_db):
    seed_spy()
    tickers = ["A", "B", "C", "D", "E"]
    rawstore.write_rows("price_bars_daily", [bar_row(t, "2026-10-07", close=10.0) for t in tickers])
    uw = FakeUW({t: [{"date": "2026-10-08", "close": 11.0, "volume": 1}] for t in tickers})
    dl = FakeDownload({t: {"2026-10-08": yf_row(11.0)} for t in tickers})

    res = prices.ensure_daily_bars(tickers, uw=uw, yf_download=dl, now=NOW, run_id="r1",
                                   max_uw_calls=3)
    assert len(uw.requests) == 3 and res["uw_capped"] == 2
    assert rows_for("E", "2026-10-08")[0]["source"] == "yfinance"

    reset_prices()
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row(t, "2026-10-07", close=10.0) for t in tickers])
    uw2 = FakeUW({t: [{"date": "2026-10-08", "close": 11.0, "volume": 1}] for t in tickers})
    res2 = prices.ensure_daily_bars(tickers, uw=uw2, yf_download=FakeDownload({}),
                                    now=NOW, run_id="r2", max_uw_calls=5)
    assert len(uw2.requests) == 5 and res2["uw_capped"] == 0


def test_t12_actions_download_window_and_yf_kw(rawstore_tmp_db):
    seed_spy()
    far = date(2025, 12, 1)
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", far, close=10.0)])
    window = datetime(2026, 1, 2, 20, 0, tzinfo=UTC).timestamp()
    rawstore.write_rows("price_checks", [{"dataset": "price_actions", "source": "yfinance",
                                           "key": "AAA", "checked_at": window, "run_id": "seed"}])
    uw = FakeUW({"AAA": []})
    dl = FakeDownload({"AAA": {far.isoformat(): yf_row(10.0),
                               LATEST.isoformat(): yf_row(11.0)}})

    prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl, now=NOW, run_id="r1")

    _, kw = dl.calls[-1]
    assert set(kw) == set(prices.YF_KW) | {"start", "end"}
    assert kw["auto_adjust"] is False and kw["actions"] is True
    assert kw["start"] <= date(2026, 1, 2) - timedelta(days=prices.ACTIONS_LOOKBACK_DAYS)
    assert kw["end"] == LATEST + timedelta(days=1)


def test_t17_nightly_refuses_without_the_spy_fill(rawstore_tmp_db):
    rawstore.write_rows("price_bars_daily", [bar_row("SPY", "2026-10-07", close=600.0)])
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-07", close=10.0)])
    uw = FakeUW({"AAA": [{"date": "2026-10-08", "close": 11.0, "volume": 1}]})
    dl = FakeDownload({"AAA": {"2026-10-08": yf_row(11.0)}})

    res = prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl, now=NOW, run_id="r1")

    assert res["errors"] == ["fill_not_done"]
    assert uw.requests == [] and dl.calls == [] and res["rows_inserted"] == 0


def test_t18_new_tickers_fill_capped_and_retried(rawstore_tmp_db):
    seed_spy()
    uw = FakeUW({})
    dl = FakeDownload({t: {"2020-01-02": yf_row(5.0), "2026-10-07": yf_row(9.0),
                           "2026-10-08": yf_row(10.0)} for t in ("A", "B", "C")})

    res = prices.ensure_daily_bars(["A", "B", "C"], uw=uw, yf_download=dl, now=NOW,
                                   run_id="r1", max_new_fill=2)
    assert res["filled_new"] == 2 and res["awaiting_fill"] == 1
    marks = {m["key"] for m in rawstore.get_all("price_checks")
             if m["dataset"] == "price_bars_daily"}
    assert {"A", "B"} <= marks and "C" not in marks


def test_t18b_no_history_recent_is_skipped_and_retried(rawstore_tmp_db):
    seed_spy()
    rawstore.write_rows("price_checks", [
        {"dataset": "price_bars_daily", "source": "yfinance_max", "key": "OLD",
         "checked_at": (NOW - timedelta(days=2)).timestamp(), "run_id": "seed"},
        {"dataset": "price_bars_daily", "source": "yfinance_max", "key": "STALE",
         "checked_at": (NOW - timedelta(days=8)).timestamp(), "run_id": "seed"}])
    dl = FakeDownload({t: {"2026-10-08": yf_row(10.0)} for t in ("OLD", "STALE")})
    uw = FakeUW({})

    res = prices.ensure_daily_bars(["OLD", "STALE"], uw=uw, yf_download=dl, now=NOW,
                                   run_id="r1", max_new_fill=5)
    assert res["no_history_recent"] == 1 and res["filled_new"] == 1
    assert rows_for("OLD") == [] and rows_for("STALE")


def test_t19_no_fetch_makes_no_calls_or_writes(rawstore_tmp_db):
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-07", close=10.0)])
    uw = FakeUW({"AAA": [{"date": "2026-10-08", "close": 11.0, "volume": 1}]})
    dl = FakeDownload({"AAA": {"2026-10-08": yf_row(11.0)}})

    res = prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl, now=NOW, run_id="r1",
                                   no_fetch=True)

    assert res["no_fetch"] is True and res["due"] == 1
    assert uw.requests == [] and dl.calls == [] and res["rows_inserted"] == 0


def test_t20_a_failed_chunk_rolls_back_only_itself(rawstore_tmp_db, monkeypatch):
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-07", close=10.0),
                                             bar_row("BBB", "2026-10-07", close=20.0)])
    uw = FakeUW({t: [{"date": "2026-10-08", "close": 11.0, "volume": 1}] for t in ("AAA", "BBB")})
    dl = FakeDownload({t: {"2026-10-08": yf_row(11.0)} for t in ("AAA", "BBB")})
    monkeypatch.setattr(prices, "CHUNK", 1)

    real_atomic = rawstore.atomic
    calls = {"n": 0}

    def flaky(fn, db="raw"):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real_atomic(fn, db=db)

    monkeypatch.setattr(rawstore, "atomic", flaky)

    res = prices.ensure_daily_bars(["AAA", "BBB"], uw=uw, yf_download=dl, now=NOW, run_id="r1")

    assert res["chunk_failed"] == 1
    assert rows_for("AAA", "2026-10-08") and rows_for("BBB", "2026-10-08") == []
    marks = {m["key"] for m in rawstore.get_all("price_checks")
             if m["dataset"] == "price_actions"}
    assert "AAA" in marks and "BBB" not in marks, "a rolled-back chunk must not leave its watermark"


# ── T13-T14: the read formulas ───────────────────────────────────────────────

def _t13_bars():
    ev = datetime(2026, 3, 2, 21, 35, tzinfo=UTC).timestamp()
    def b(d, c, v):
        return {"ticker": "T", "session_date": date.fromisoformat(d), "open": c, "high": c,
                "low": c, "close": c, "adj_close": None, "volume": v, "split_ratio": None,
                "adj_source": None, "source": "uw", "fetched_at": ev, "run_id": "r", "run_at": ev}
    return [b("2026-03-02", 100.0, 1000.0), b("2026-03-03", 102.0, 1000.0),
            b("2026-03-04", 51.0, 2000.0), b("2026-03-05", 50.5, 2000.0)]


def _t13_actions(**kw):
    ev = datetime(2026, 3, 4, 21, 35, tzinfo=UTC).timestamp()
    return [
        {"ticker": "T", "ex_date": date(2026, 3, 4), "action": "split", "value": 2.0,
         "session_date": date(2026, 3, 4), "source": "yfinance", "fetched_at": ev,
         "run_id": "r", "run_at": ev},
        {"ticker": "T", "ex_date": date(2026, 3, 5), "action": "dividend", "value": 0.5,
         "session_date": date(2026, 3, 5), "source": "yfinance",
         "fetched_at": datetime(2026, 3, 5, 21, 35, tzinfo=UTC).timestamp(),
         "run_id": "r", "run_at": ev},
    ]


def test_t13_read_formulas_nominal_and_adjusted():
    bars, actions = _t13_bars(), _t13_actions()
    nominal = prices.apply_events(bars, actions, "nominal")
    assert [round(r["close"], 10) for r in nominal] == [50.0, 51.0, 51.0, 50.5]
    assert [round(r["volume"], 10) for r in nominal] == [2000.0, 2000.0, 2000.0, 2000.0]
    adj = prices.apply_events(bars, actions, "adjusted")
    assert [round(r["close"], 10) for r in adj] == [
        49.5098039216, 50.5, 50.5, 50.5]


def test_t14_split_and_amount_gating():
    ev = datetime(2026, 3, 4, 21, 35, tzinfo=UTC).timestamp()
    def b(d, c):
        return {"ticker": "T", "session_date": date.fromisoformat(d), "open": c, "high": c,
                "low": c, "close": c, "adj_close": None, "volume": 1000.0, "split_ratio": None,
                "adj_source": None, "source": "uw", "fetched_at": ev, "run_id": "r", "run_at": ev}
    bars = [b("2026-02-26", 48.0), b("2026-02-27", 49.0), b("2026-03-04", 51.0)]
    actions = [
        {"ticker": "T", "ex_date": date(2026, 3, 4), "action": "split", "value": 2.0,
         "session_date": date(2026, 3, 4), "source": "yfinance", "fetched_at": ev,
         "run_id": "r", "run_at": ev},
        {"ticker": "T", "ex_date": date(2026, 2, 27), "action": "dividend", "value": 1.0,
         "session_date": date(2026, 2, 27), "source": "yfinance",
         "fetched_at": datetime(2026, 2, 27, 21, 35, tzinfo=UTC).timestamp(),
         "run_id": "r", "run_at": ev}]

    nominal = {r["session_date"]: r["close"] for r in prices.apply_events(bars, actions, "nominal")}
    assert round(nominal[date(2026, 2, 27)], 10) == 49.0      # fetched after the split open
    adj = {r["session_date"]: r["close"] for r in prices.apply_events(bars, actions, "adjusted")}
    assert round(adj[date(2026, 2, 26)], 10) == 47.5        # 48 × (1 − 0.5/48), gated
    assert round(adj[date(2026, 2, 26)], 6) != 47.0         # not 1 − 1/48


def test_t13_daily_bars_filters_after_factors(rawstore_tmp_db):
    ev = datetime(2026, 3, 2, 21, 35, tzinfo=UTC).timestamp()
    rawstore.write_rows("price_bars_daily", [
        {"ticker": "T", "session_date": date(2026, 3, 4), "open": 51.0, "high": 51.0,
         "low": 51.0, "close": 51.0, "adj_close": None, "volume": 2000.0, "split_ratio": None,
         "adj_source": None, "source": "uw", "fetched_at": ev, "run_id": "r", "run_at": ev}])
    rawstore.write_rows("price_actions", [
        {"ticker": "T", "ex_date": date(2026, 3, 4), "action": "split", "value": 2.0,
         "session_date": date(2026, 3, 4), "source": "yfinance", "fetched_at": ev,
         "run_id": "r", "run_at": ev}])
    df = prices.daily_bars(["T"], basis="nominal", start=date(2026, 3, 4), end=date(2026, 3, 4))
    assert list(df["close"]) == [51.0]
    with pytest.raises(ValueError):
        prices.daily_bars(["T"], basis="bogus")


# ── T15-T16: the fill ────────────────────────────────────────────────────────

def test_t15_fill_reference_and_no_pre_2025_calendar(rawstore_tmp_db, monkeypatch):
    spy_first = date(2020, 1, 2)
    ref = {
        "SPY": {"2019-12-31": yf_row(300.0), spy_first.isoformat(): yf_row(310.0),
                "2026-10-08": yf_row(600.0)},
        "^GSPC": {"1962-01-02": yf_row(50.0), "1990-01-02": yf_row(350.0)},
    }
    old = {
        "1962-01-02": yf_row(1.0),          # a ^GSPC date before SPY's first: kept
        "2019-06-03": yf_row(2.0),          # absent from SPY U ^GSPC: dropped
        "2019-12-31": yf_row(3.0),          # a SPY date: kept
    }
    dl = FakeDownload({**ref, "OLD": old})

    real = calendar.is_trading_day

    def guard(d):
        if d < prices.CALENDAR_START:
            raise AssertionError(f"the calendar was called for pre-2025 date {d}")
        return real(d)

    monkeypatch.setattr(calendar, "is_trading_day", guard)

    res = prices.fill_history(["OLD"], yf_download=dl, now=NOW, run_id="r1")

    assert res["spy_first"] == "2019-12-31"
    assert res["reference_first"] == "1962-01-02"
    kept = {r["session_date"] for r in rows_for("OLD")}
    assert date(1962, 1, 2) in kept and date(2019, 12, 31) in kept
    assert date(2019, 6, 3) not in kept
    assert res["dropped_off_reference"] == 1
    assert dl.calls[0][0] == ["SPY", "^GSPC"] and dl.calls[1][0] == ["SPY"]


def test_t16_fill_markers_and_rerun_skips(rawstore_tmp_db):
    dl = FakeDownload({
        "SPY": {"2026-10-07": yf_row(600.0), "2026-10-08": yf_row(601.0)},
        "^GSPC": {"1990-01-02": yf_row(350.0), "2026-10-08": yf_row(5000.0)},
        "AAA": {"2026-10-07": yf_row(10.0), "2026-10-08": yf_row(11.0)},
        "BBB": {"2026-10-07": yf_row(20.0), "2026-10-08": yf_row(21.0)}})

    res = prices.fill_history(["AAA", "BBB"], yf_download=dl, now=NOW, run_id="r1")
    marks = {m["key"] for m in rawstore.get_all("price_checks")
             if m["dataset"] == "price_bars_daily"}
    assert {"SPY", "AAA", "BBB"} <= marks
    assert res["filled_new"] == 2

    dl2 = FakeDownload(dl.rows)
    prices.fill_history(["AAA", "BBB"], yf_download=dl2, now=NOW, run_id="r2")
    seen = {t for call, _ in dl2.calls for t in call}
    assert "AAA" not in seen and "BBB" not in seen


# ── T21-T24: diagnostics, outage, lock, calendar ─────────────────────────────

def test_t21_overlap_diagnostics():
    stats = prices.overlap_stats([
        ({"ticker": "A", "volume": 107.0, "close": 1.0005},
         {"ticker": "A", "volume": 100.0, "close": 1.0, "source": "uw"})])
    assert stats["rows"] == 1
    assert stats["prev_volume"]["n"] == 1 and round(stats["prev_volume"]["median"], 6) == 0.07
    assert stats["prev_volume"]["over_1pct"] == 1 and stats["close_mismatch"] == 0
    assert stats["prev_volume_by_source"]["uw"]["n"] == 1

    assert prices.overlap_stats([({"close": 1.002}, {"close": 1.0})])["close_mismatch"] == 1
    assert prices.overlap_stats([({"close": 2.0}, {"close": 1.0})])["close_step"] == 1


def test_t22_uw_auth_and_breaker_stop_the_calls(rawstore_tmp_db):
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-07", close=10.0),
                                             bar_row("BBB", "2026-10-07", close=20.0)])
    uw = FakeUW({t: [{"date": "2026-10-08", "close": 11.0, "volume": 1}] for t in ("AAA", "BBB")})
    uw.on_call = lambda self, t: setattr(self, "auth_error", {"status": 401})
    dl = FakeDownload({t: {"2026-10-08": yf_row(11.0)} for t in ("AAA", "BBB")})

    res = prices.ensure_daily_bars(["AAA", "BBB"], uw=uw, yf_download=dl, now=NOW, run_id="r1")

    assert len(uw.requests) == 1 and "uw_auth" in res["errors"]

    reset_prices()
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-07", close=10.0),
                                             bar_row("BBB", "2026-10-07", close=20.0)])
    uw2 = FakeUW({t: [{"date": "2026-10-08", "close": 11.0}] for t in ("AAA", "BBB")})
    uw2.breaker_open = True
    res2 = prices.ensure_daily_bars(["AAA", "BBB"], uw=uw2,
                                    yf_download=FakeDownload({t: {"2026-10-08": yf_row(11.0)}
                                                              for t in ("AAA", "BBB")}),
                                    now=NOW, run_id="r2")
    assert uw2.requests == [] and "uw_breaker" in res2["errors"]


def test_t23_lock_is_reported_and_runner_raises_jobmessage(rawstore_tmp_db):
    from RevenueChaser.app import runners
    from RevenueChaser.app.jobs import JobMessage

    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-07", close=10.0)])
    uw = FakeUW({"AAA": []})
    dl = FakeDownload({"AAA": {}})

    with job_lock.acquire("daily_bars", wait=0) as got:
        assert got
        res = prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl, now=NOW, run_id="r1")
        assert res["locked"] is True and uw.requests == [] and dl.calls == []
        with pytest.raises(JobMessage):
            runners.run_daily_bars(uw=uw, tickers=["AAA"])


def test_t24_calendar_error_aborts(rawstore_tmp_db):
    uw = FakeUW({"AAA": []})
    dl = FakeDownload({"AAA": {}})
    res = prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl,
                                   now=datetime(2029, 1, 2, 20, 35, tzinfo=UTC), run_id="r1")
    assert res["errors"] == ["calendar"] and uw.requests == [] and dl.calls == []
    assert not prices.run_ok(res)


# ── scope helper ─────────────────────────────────────────────────────────────

def test_bar_scope_puts_the_benchmarks_first(rawstore_tmp_db):
    rawstore.write_rows("equity_universe", [{"ticker": "aapl", "market_cap": 1e9},
                                            {"ticker": "MSFT", "market_cap": 1e9}])
    scope = prices.bar_scope()
    assert scope[:3] == ["SPY", "DIA", "QQQ"]
    assert "AAPL" in scope and "MSFT" in scope
    assert "US10Y" not in scope and "CL1!" not in scope      # yield/commodity macro rows left out
    assert "XLK" in scope                                     # a rotation ETF


# ── review follow-ups (coordinator review of the DeepSeek build) ─────────────

def test_an_amount_ex_on_or_after_the_split_is_never_divided_by_it():
    """§9.4: ``a' = a ÷ Π r`` only over splits with ``ex_div < e`` (and ``fetched_at_div < T(e)``)."""
    early = datetime(2026, 3, 3, 21, 35, tzinfo=UTC).timestamp()     # before the split's ex-date open
    def b(d, c):
        return {"ticker": "T", "session_date": date.fromisoformat(d), "open": c, "high": c,
                "low": c, "close": c, "adj_close": None, "volume": 1000.0, "split_ratio": None,
                "adj_source": None, "source": "uw", "fetched_at": early, "run_id": "r", "run_at": early}
    bars = [b("2026-03-05", 50.0), b("2026-03-06", 50.0)]
    actions = [
        {"ticker": "T", "ex_date": date(2026, 3, 4), "action": "split", "value": 2.0,
         "session_date": date(2026, 3, 4), "source": "yfinance", "fetched_at": early,
         "run_id": "r", "run_at": early},
        {"ticker": "T", "ex_date": date(2026, 3, 6), "action": "dividend", "value": 0.5,
         "session_date": date(2026, 3, 6), "source": "yfinance", "fetched_at": early,
         "run_id": "r", "run_at": early}]
    adj = {r["session_date"]: r["close"] for r in prices.apply_events(bars, actions, "adjusted")}
    assert round(adj[date(2026, 3, 5)], 10) == 49.5          # 50 × (1 − 0.5/50), not 1 − 0.25/50


def test_overlap_median_covers_every_chunk_not_the_last(rawstore_tmp_db, monkeypatch):
    """R1 diagnostics: the run's median/p90 are over all overlap pairs, whatever the chunk size."""
    monkeypatch.setattr(prices, "CHUNK", 1)
    seed_spy()
    stored = {"AAA": 100.0, "BBB": 100.0, "CCC": 100.0}
    for t in stored:
        rawstore.write_rows("price_bars_daily", [bar_row(t, "2026-10-07", close=10.0, volume=100.0)])
    uw = FakeUW({"AAA": [{"date": "2026-10-07", "close": 10.0, "volume": 110},
                         {"date": "2026-10-08", "close": 10.0, "volume": 1}],
                 "BBB": [{"date": "2026-10-07", "close": 10.0, "volume": 120},
                         {"date": "2026-10-08", "close": 10.0, "volume": 1}],
                 "CCC": [{"date": "2026-10-07", "close": 10.0, "volume": 100.5},
                         {"date": "2026-10-08", "close": 10.0, "volume": 1}]})
    dl = FakeDownload({t: {"2026-10-08": yf_row(10.0)} for t in stored})

    res = prices.ensure_daily_bars(list(stored), uw=uw, yf_download=dl, now=NOW, run_id="r1")

    pv = res["overlap"]["prev_volume"]
    assert res["overlap"]["rows"] == 3 and pv["n"] == 3
    assert round(pv["median"], 6) == 0.10                     # of 0.10, 0.20, 0.005; the last chunk alone is 0.005
    assert pv["over_1pct"] == 2


def test_a_chunk_reads_only_the_window_it_can_write(rawstore_tmp_db, monkeypatch):
    """A filled history is not loaded each night: the chunk read starts at the oldest offered date."""
    seed_spy()
    old = [bar_row("AAA", d, close=5.0) for d in ("2024-01-02", "2024-01-03")]
    rawstore.write_rows("price_bars_daily", old + [bar_row("AAA", "2026-10-07", close=10.0)])
    seen = []
    real = rawstore.query
    def spy(sql, params=None, *a, **k):
        if "FROM price_bars_daily" in sql and "SELECT *" in sql:
            seen.append(params)
        return real(sql, params, *a, **k)
    monkeypatch.setattr(rawstore, "query", spy)
    uw = FakeUW({"AAA": [{"date": "2026-10-07", "close": 10.0, "volume": 1},
                         {"date": "2026-10-08", "close": 11.0, "volume": 1}]})
    dl = FakeDownload({"AAA": {"2026-10-08": yf_row(11.0)}})

    prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl, now=NOW, run_id="r1")

    assert seen and all(p[-1] == date(2026, 10, 7) for p in seen)
    assert [r["close"] for r in rows_for("AAA", "2026-10-08")] == [11.0]


# ── code review B1/B2 and test gaps G1-G4 (arch_reviews/2026-10-09_156_slice_a_code_review.md) ──

_REF = {"SPY": {"2026-03-02": yf_row(600.0), "2026-03-03": yf_row(601.0)},
        "^GSPC": {"1990-01-02": yf_row(350.0), "2026-03-03": yf_row(5000.0)}}


@pytest.mark.parametrize("empty", ["SPY", "^GSPC"])
def test_b1_an_empty_reference_stops_the_fill_before_any_write(rawstore_tmp_db, empty):
    """yfinance returns an empty frame for a failed symbol; that must not drop history for good."""
    rows = {**_REF, "OLD": {"1962-01-02": yf_row(1.0), "2026-03-03": yf_row(2.0)}}
    rows[empty] = {}
    dl = FakeDownload(rows)

    res = prices.fill_history(["OLD"], yf_download=dl, now=datetime(2026, 3, 4, 22, 0, tzinfo=UTC),
                              run_id="r1")

    assert "reference" in res["errors"]
    assert rawstore.get_all("price_bars_daily") == []
    assert rawstore.get_all("price_checks") == []
    assert [c[0] for c in dl.calls] == [["SPY", "^GSPC"]]


def test_b2_fetched_at_is_the_arrival_not_the_run_start(rawstore_tmp_db, monkeypatch):
    """A fill that starts before a split's ex-date open and downloads after it gets Yahoo's
    split-adjusted rows; stamped with the run start they would be divided a second time."""
    clock = [datetime(2026, 3, 4, 14, 0, tzinfo=UTC)]           # 09:00 EST, before the open
    monkeypatch.setattr(calendar, "_now", lambda: clock[0])
    rows = {**_REF, "T": {"2026-03-02": yf_row(50.0), "2026-03-03": yf_row(51.0),
                          "2026-03-04": yf_row(52.0, split=2.0)}}
    inner = FakeDownload(rows)

    def dl(tickers, **kw):
        if list(tickers) == ["T"]:
            clock[0] = datetime(2026, 3, 4, 14, 45, tzinfo=UTC)  # after the 09:30 EST open
        return inner(tickers, **kw)

    prices.fill_history(["T"], yf_download=dl, run_id="r1")

    stamped = {r["fetched_at"] for r in rows_for("T")}
    assert stamped == {datetime(2026, 3, 4, 14, 45, tzinfo=UTC).timestamp()}
    df = prices.daily_bars(["T"], basis="nominal")
    assert list(df["close"]) == [50.0, 51.0]


def test_g1_an_amount_fetched_after_the_split_is_not_divided():
    after = datetime(2026, 3, 5, 21, 35, tzinfo=UTC).timestamp()
    bar = {"ticker": "T", "session_date": date(2026, 2, 26), "open": 48.0, "high": 48.0, "low": 48.0,
           "close": 48.0, "adj_close": None, "volume": 1.0, "split_ratio": None, "adj_source": None,
           "source": "yfinance", "fetched_at": after, "run_id": "r", "run_at": after}
    actions = [
        {"ticker": "T", "ex_date": date(2026, 3, 4), "action": "split", "value": 2.0, "fetched_at": after},
        {"ticker": "T", "ex_date": date(2026, 2, 27), "action": "dividend", "value": 1.0, "fetched_at": after}]
    adj = prices.apply_events([bar], actions, "adjusted")
    assert round(adj[0]["close"], 10) == 47.0             # 48 × (1 − 1/48): the amount is already post-split


def test_g2_daily_bars_window_uses_a_c_prev_outside_it(rawstore_tmp_db):
    ev = datetime(2026, 3, 5, 21, 35, tzinfo=UTC).timestamp()
    rawstore.write_rows("price_bars_daily", [bar_row("T", "2026-03-03", close=40.0, fetched_at=ev),
                                             bar_row("T", "2026-03-04", close=50.0, fetched_at=ev)])
    rawstore.write_rows("price_actions", [
        {"ticker": "T", "ex_date": date(2026, 3, 5), "action": "dividend", "value": 1.0,
         "session_date": date(2026, 3, 5), "source": "yfinance", "fetched_at": ev, "run_id": "r",
         "run_at": ev}])
    df = prices.daily_bars(["T"], basis="adjusted", start=date(2026, 3, 3), end=date(2026, 3, 3))
    assert [round(x, 10) for x in df["close"]] == [39.2]    # C_prev is 03-04's 50, not 03-03's 40


def test_g3_a_far_behind_ticker_gets_its_own_download(rawstore_tmp_db):
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-07", close=10.0),
                                             bar_row("BBB", "2026-06-01", close=20.0)])
    seed_actions("AAA", "2026-10-07")
    seed_actions("BBB", "2026-06-01")
    uw = FakeUW({"AAA": [], "BBB": []})
    dl = FakeDownload({"AAA": {"2026-10-08": yf_row(11.0)}, "BBB": {"2026-10-08": yf_row(21.0)}})

    prices.ensure_daily_bars(["AAA", "BBB"], uw=uw, yf_download=dl, now=NOW, run_id="r1")

    got = [(c[0], c[1]["start"]) for c in dl.calls]
    assert got == [(["AAA"], date(2026, 9, 27)), (["BBB"], date(2026, 5, 22))]


def test_g4_the_fill_marker_records_tried_even_with_no_rows(rawstore_tmp_db):
    dl = FakeDownload(dict(_REF))                         # NODATA comes back empty

    res = prices.fill_history(["NODATA"], yf_download=dl, now=datetime(2026, 3, 4, 22, 0, tzinfo=UTC),
                              run_id="r1")

    marks = {(m["dataset"], m["key"]) for m in rawstore.get_all("price_checks")}
    assert ("price_bars_daily", "NODATA") in marks
    assert res["filled_new"] == 0 and rows_for("NODATA") == []


def test_n2_a_negative_split_or_one_that_multiplies_to_1_is_not_stored():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-03-04"), pd.Timestamp("2026-03-04"),
                            pd.Timestamp("2026-03-05")])
    frame = pd.DataFrame({("T", "Close"): [10.0, 10.0, 10.0], ("T", "Stock Splits"): [2.0, 0.5, -3.0]},
                         index=idx)
    _, actions = prices.rows_from_yf(frame, ["T"], fetched_at=0.0, run_id="r", run_at=0.0)
    assert [a for a in actions["T"] if a["action"] == "split"] == []


def test_n4_a_bare_string_is_one_ticker(rawstore_tmp_db):
    rawstore.write_rows("price_bars_daily", [bar_row("SPY", "2026-10-07", close=600.0)])
    assert list(prices.daily_bars("SPY")["ticker"]) == ["SPY"]


# ── re-review of 5428edb: G6-G8, N1/N11, N6 (arch_reviews/2026-10-09_156_slice_a_rereview_5428edb.md) ──

def _seed_split_day():
    """SPY filled; T stored up to 2026-10-05 (fetched that evening); a 2:1 split ex 2026-10-08."""
    seed_spy("2026-10-07")
    rawstore.write_rows("price_bars_daily", [bar_row(
        "T", "2026-10-05", close=100.0, source="uw",
        fetched_at=datetime(2026, 10, 5, 20, 35, tzinfo=UTC).timestamp())])
    rawstore.write_rows("price_actions", [{
        "ticker": "T", "ex_date": date(2026, 10, 8), "action": "split", "value": 2.0,
        "session_date": date(2026, 10, 8), "source": "yfinance", "fetched_at": 0.0, "run_id": "s",
        "run_at": 0.0}])


def test_g6_nightly_uw_rows_are_stamped_at_arrival(rawstore_tmp_db, monkeypatch):
    """A manual daytime run that starts at 09:20 EDT on a split ex-date and reaches the ticker at
    09:45: UW already serves split-adjusted bars, so they must not be divided again."""
    clock = [datetime(2026, 10, 8, 13, 20, tzinfo=UTC)]
    monkeypatch.setattr(calendar, "_now", lambda: clock[0])
    _seed_split_day()
    uw = FakeUW({"T": [{"date": "2026-10-06", "close": 51, "volume": 1},
                       {"date": "2026-10-07", "close": 52, "volume": 1}]})
    uw.on_call = lambda self, t: clock.__setitem__(0, datetime(2026, 10, 8, 13, 45, tzinfo=UTC))

    prices.ensure_daily_bars(["T"], uw=uw, yf_download=FakeDownload({}), run_id="r1")

    assert list(prices.daily_bars(["T"], basis="nominal")["close"]) == [50.0, 51.0, 52.0]


def test_g6b_nightly_yfinance_rows_are_stamped_at_arrival(rawstore_tmp_db, monkeypatch):
    clock = [datetime(2026, 10, 8, 13, 20, tzinfo=UTC)]
    monkeypatch.setattr(calendar, "_now", lambda: clock[0])
    _seed_split_day()
    inner = FakeDownload({"T": {"2026-10-06": yf_row(51.0), "2026-10-07": yf_row(52.0)}})

    def dl(tickers, **kw):
        clock[0] = datetime(2026, 10, 8, 13, 45, tzinfo=UTC)
        return inner(tickers, **kw)

    prices.ensure_daily_bars(["T"], uw=FakeUW(fail={"T"}), yf_download=dl, run_id="r1")

    assert list(prices.daily_bars(["T"], basis="nominal")["close"]) == [50.0, 51.0, 52.0]


def test_g7_yfinance_zero_event_columns_store_no_action():
    """Real frames carry 0.0 in Dividends, Stock Splits and Capital Gains on days without an event."""
    frame = make_frame({"T": {"2026-10-07": yf_row(10.0, div=0.0, split=0.0, gain=0.0),
                              "2026-10-08": yf_row(11.0, div=0.0, split=0.0, gain=0.0)}})
    bars, actions = prices.rows_from_yf(frame, ["T"], fetched_at=0.0, run_id="r", run_at=0.0)
    assert len(bars["T"]) == 2 and actions["T"] == []


def test_g8_nightly_new_fill_keeps_a_pre_2025_row_on_a_stored_spy_date(rawstore_tmp_db):
    seed_spy("2026-10-07")
    rawstore.write_rows("price_bars_daily", [bar_row("SPY", "2019-12-31", close=320.0)])
    dl = FakeDownload({"NEW": {"2019-12-31": yf_row(5.0), "2019-06-03": yf_row(4.0),
                               "2026-10-08": yf_row(9.0)}})

    res = prices.ensure_daily_bars(["NEW"], uw=FakeUW(), yf_download=dl, now=NOW, run_id="r1")

    kept = {r["session_date"] for r in rows_for("NEW")}
    assert date(2019, 12, 31) in kept and date(2019, 6, 3) not in kept
    assert res["filled_new"] == 1 and res["dropped_off_reference"] == 1


def test_g8b_fill_retries_a_ticker_marked_tried_without_bars(rawstore_tmp_db):
    """How a rate-limited D3 chunk is repaired: the marker says tried, no bar exists, so --fill
    downloads the ticker again."""
    rawstore.write_rows("price_checks", [{"dataset": "price_bars_daily", "source": "yfinance_max",
                                           "key": "AAA", "checked_at": 0.0, "run_id": "old"}])
    dl = FakeDownload({**_REF, "AAA": {"2026-03-02": yf_row(10.0), "2026-03-03": yf_row(11.0)}})

    prices.fill_history(["AAA"], yf_download=dl, now=datetime(2026, 3, 4, 22, 0, tzinfo=UTC), run_id="r1")

    assert ["AAA"] in [c[0] for c in dl.calls]
    assert [r["close"] for r in sorted(rows_for("AAA"), key=lambda r: r["session_date"])] == [10.0, 11.0]


def test_n11_a_zero_price_is_never_stored_and_is_counted():
    frame = make_frame({"T": {"2026-10-06": yf_row(50.0), "2026-10-07": yf_row(0.0),
                              "2026-10-08": yf_row(49.0, o=0.0, low=0.0)}})
    counts: dict = {}
    bars, _ = prices.rows_from_yf(frame, ["T"], fetched_at=0.0, run_id="r", run_at=0.0, counts=counts)
    assert [b["close"] for b in bars["T"]] == [50.0, 49.0]
    assert bars["T"][1]["open"] is None and bars["T"][1]["low"] is None
    assert counts == {"dropped_no_close": 1}

    uw_counts: dict = {}
    uw_rows = prices.rows_from_uw("T", [{"date": "2026-10-07", "close": "0", "volume": 1},
                                        {"date": "2026-10-08", "close": "12.5", "volume": 1}],
                                  fetched_at=0.0, run_id="r", run_at=0.0, counts=uw_counts)
    assert [r["close"] for r in uw_rows] == [12.5] and uw_counts == {"dropped_no_close": 1}


def test_g9_a_non_positive_open_high_low_and_adj_close_are_never_stored():
    """D4 / D14: UW O/H/L <= 0 and yfinance Adj Close <= 0 are stored as NULL, never as a price."""
    uw_rows = prices.rows_from_uw(
        "T", [{"date": "2026-10-08", "open": "0", "high": "-1", "low": "0", "close": "12.5", "volume": 1}],
        fetched_at=0.0, run_id="r", run_at=0.0)
    assert (uw_rows[0]["open"], uw_rows[0]["high"], uw_rows[0]["low"]) == (None, None, None)
    assert uw_rows[0]["close"] == 12.5

    frame = make_frame({"T": {"2026-10-08": yf_row(49.0, adj=0.0)}})
    bars, _ = prices.rows_from_yf(frame, ["T"], fetched_at=0.0, run_id="r", run_at=0.0)
    assert bars["T"][0]["close"] == 49.0 and bars["T"][0]["adj_close"] is None


def test_n11b_a_zero_c_prev_skips_the_event_instead_of_crashing():
    bars = [bar_row("T", "2026-03-02", close=50.0), bar_row("T", "2026-03-03", close=0.0),
            bar_row("T", "2026-03-04", close=49.5)]
    actions = [{"ticker": "T", "ex_date": date(2026, 3, 4), "action": "dividend", "value": 0.5,
                "fetched_at": 0.0}]
    adj = prices.apply_events(bars, actions, "adjusted")
    assert adj.attrs["skipped_events"] == 1


def test_n6_an_index_symbol_is_never_written(rawstore_tmp_db):
    dl = FakeDownload(dict(_REF))
    res = prices.fill_history(["^GSPC", "^VIX"], yf_download=dl,
                              now=datetime(2026, 3, 4, 22, 0, tzinfo=UTC), run_id="r1")
    assert res["scope"] == 0
    assert {r["ticker"] for r in rawstore.get_all("price_bars_daily")} == {"SPY"}


# ── prices.duckdb (#156, avo 2026-10-09) ─────────────────────────────────────

def _in_file(db: str, sql: str) -> list[tuple]:
    with rawstore.connect(read_only=True, db=db) as conn:
        return conn.execute(sql).fetchall()


def test_a_chunk_commits_its_bars_actions_and_watermarks_in_prices_duckdb(rawstore_tmp_db):
    """R5 in one file: the bars, the actions and the actions watermark of a chunk land in prices.duckdb together,
    and raw's ``source_checks`` (D22's identity stamps) gets nothing."""
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-07", close=10.0)])
    uw = FakeUW({"AAA": [{"date": "2026-10-08", "close": 11.0, "volume": 1}]})
    dl = FakeDownload({"AAA": {"2026-10-07": yf_row(10.0), "2026-10-08": yf_row(11.0, div=0.24)}})

    prices.ensure_daily_bars(["AAA"], uw=uw, yf_download=dl, now=NOW, run_id="r1")

    assert _in_file("prices", "SELECT ticker, session_date::TEXT FROM price_bars_daily WHERE ticker = 'AAA' "
                              "ORDER BY 2") == [("AAA", "2026-10-07"), ("AAA", "2026-10-08")]
    assert _in_file("prices", "SELECT ticker, action FROM price_actions") == [("AAA", "dividend")]
    assert ("price_actions", "yfinance", "AAA") in _in_file("prices", "SELECT dataset, source, key FROM price_checks")
    assert _in_file("raw", "SELECT * FROM source_checks") == []


# ── Data Health (#124 S4): the typed numbers the note is derived from ────────

def test_health_is_the_summary_numbers_typed(rawstore_tmp_db):
    """``health`` and ``health_summary`` are one computation: covered is the scope minus the tickers behind it,
    and the source share is the newest session's rows per source (Health stores a share, the dict a count)."""
    from RevenueChaser.core import datahealth

    rawstore.write_rows("equity_universe", [{"ticker": "AAA", "market_cap": 1e9},
                                            {"ticker": "BBB", "market_cap": 1e9}])
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-08", close=10.0, source="uw"),
                                             bar_row("BBB", "2026-10-08", close=11.0)])

    n = prices.health_summary(now=NOW)
    h = prices.health(now=NOW)

    scope = len(prices.bar_scope())
    assert n["scope"] == scope and n["behind"] == scope - 2, "only AAA and BBB have a bar on the newest session"
    assert h.covered == n["scope"] - n["behind"] == 2
    assert h.expected == n["scope"] == scope
    assert (h.newest, h.expected_as_of) == (n["newest_session"], n["latest_complete"]) == ("2026-10-08", "2026-10-08")
    assert n["source_share"] == {"uw": 1, "yfinance": 1}
    assert h.source_share == {"uw": 0.5, "yfinance": 0.5}
    assert h.problems == ()
    assert datahealth.coverage_text(h) == f"{2 / scope * 100:.1f}% (2 of {scope:,})"


def test_health_names_a_calendar_error_and_reports_no_expected_as_of(rawstore_tmp_db):
    """A calendar error leaves no latest complete session: the numbers stay the store's, and the fact is a problem."""
    from RevenueChaser.core import datahealth

    later = datetime(2029, 1, 2, 20, 35, tzinfo=UTC)      # past the holiday table's end
    n = prices.health_summary(now=later)
    h = prices.health(now=later)

    assert n["calendar_error"] is True
    assert (h.newest, h.expected_as_of) == (n["newest_session"], n["latest_complete"]) == (None, None)
    assert h.covered == n["scope"] - n["behind"] == 0 and h.expected == n["scope"] > 0
    assert h.problems == ("calendar error: the latest complete session is unavailable",)
    assert datahealth.coverage_text(h) == f"0.0% (0 of {n['scope']:,})", "a real population with nothing in it"
