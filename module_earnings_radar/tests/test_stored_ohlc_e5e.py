"""E5e (#372): the reaction columns read the stored D01 daily bars (``price_bars_daily``, nominal basis) when they
are current, and ask UW for the year of bars only when they are not.

Offline: a temp prices store seeded with the bars, a fake client counting its ``etf_ohlc`` calls. The stored series
must produce exactly the columns the live answer would: the bars are the same numbers, just split-normalised on read.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from RevenueChaser.core import calendar, rawstore
from RevenueChaser.module_earnings_radar import stored_inputs
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline, REACTION_OHLC_SESSIONS
from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW

SESSION = date(2026, 10, 14)               # Wednesday, the latest complete session
NOW = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc)          # 17:00 New York


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


@pytest.fixture(autouse=True)
def _store(rawstore_tmp_db):
    return rawstore_tmp_db


def _sessions(n, end=SESSION):
    """``n`` weekday dates ending at ``end``, oldest first."""
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def _bars(n=300, end=SESSION, start=100.0):
    out = []
    for i, d in enumerate(_sessions(n, end)):
        close = start + i
        out.append({"date": d.isoformat(), "open": close - 0.5, "high": close + 1.0, "low": close - 1.0,
                    "close": close, "volume": 1000.0 + i})
    return out


def _seed(bars, ticker="AAPL"):
    rawstore.write_rows("price_bars_daily", [
        {"ticker": ticker, "session_date": date.fromisoformat(b["date"]), "open": b["open"], "high": b["high"],
         "low": b["low"], "close": b["close"], "volume": b["volume"], "source": "uw", "fetched_at": 0.0}
        for b in bars])


def _prints():
    return [{"report_date": d, "actual_eps": 1.0, "street_mean_est": 0.9, "report_time": "premarket"}
            for d in ("2026-09-02", "2026-06-03", "2026-03-04", "2025-12-03")]


class _OhlcUW(_FakeUW):
    """Counts the ``etf_ohlc`` requests and slices the series the way the real client does."""

    def __init__(self, bars=None, **over):
        super().__init__(earnings_history=_prints(), **over)
        self.bars = bars or []
        self.requests = []

    def etf_ohlc(self, ticker, days=60, candle_size="1d"):
        self.requests.append((ticker, days))
        return self.bars[-days:]


def _pipeline(fake):
    p = EarningsPipeline(["AAPL"], uw=fake, cal_meta={"AAPL": {
        "earnings_date": "2026-11-05", "full_name": "Aple", "sector": "Technology",
        "expected_move_perc": 0.05}})
    p._screener_cache = {"AAPL": {"close": "100", "call_volume": "0", "put_volume": "0",
                                  "total_open_interest": "0"}}
    p._dgs10_cache = {}
    return p


REACTION_FIELDS = ("earn_m1_pct", "earn_m2_pct", "earn_m3_pct", "earn_m4_pct", "earn_current_pct",
                   "earn_m1_eps", "earn_m2_eps", "earn_m3_eps", "earn_m4_eps", "past_1d", "past_1w",
                   "past_2w", "return_4w", "return_since_er", "since_er_chart", "last_er_label")


def test_identical_stored_bars_give_identical_reaction_columns():
    bars = _bars(300)
    live_p = _pipeline(_OhlcUW(bars))
    live_p._ohlc_bars = stored_inputs.ohlc_bars(["AAPL"])          # the store is empty here
    live_card = live_p._score("AAPL")

    _seed(bars)
    stored_p = _pipeline(_OhlcUW(bars))
    stored_p._ohlc_bars = stored_inputs.ohlc_bars(["AAPL"])
    stored_card = stored_p._score("AAPL")

    assert live_p.uw.requests == [("AAPL", REACTION_OHLC_SESSIONS)]
    assert stored_p.uw.requests == []                             # the store served the year
    for field in REACTION_FIELDS:
        assert getattr(stored_card, field) == getattr(live_card, field), field
    assert stored_card.volatility_edge_score == live_card.volatility_edge_score


def test_the_input_sources_slot_counts_stored_and_live_tickers():
    _seed(_bars(300))
    fake = _OhlcUW()
    p = _pipeline(fake)
    p._ohlc_bars = stored_inputs.ohlc_bars(["AAPL"])
    p._score("AAPL")
    assert fake.requests == []
    assert p.input_sources["ohlc"] == {"stored": 1, "live": 0}


def test_a_ticker_without_stored_bars_reads_live():
    bars = _bars(300)
    fake = _OhlcUW(bars)
    p = _pipeline(fake)
    p._ohlc_bars = stored_inputs.ohlc_bars(["AAPL"])               # empty store: no bars for AAPL
    p._score("AAPL")
    assert fake.requests == [("AAPL", REACTION_OHLC_SESSIONS)]
    assert p.input_sources["ohlc"] == {"stored": 0, "live": 1}


def test_a_stale_last_bar_reads_live():
    """The store's newest bar is a session behind: it cannot feed a reaction anchored on the latest close."""
    _seed(_bars(300, end=date(2026, 10, 13)))
    bars = _bars(300)
    fake = _OhlcUW(bars)
    p = _pipeline(fake)
    p._ohlc_bars = stored_inputs.ohlc_bars(["AAPL"])
    assert p._ohlc_bars["AAPL"][-1]["date"] == "2026-10-13"
    p._score("AAPL")
    assert fake.requests == [("AAPL", REACTION_OHLC_SESSIONS)]
    assert p.input_sources["ohlc"] == {"stored": 0, "live": 1}


def test_the_reader_maps_the_keys_the_pipeline_reads():
    _seed(_bars(300))
    bars = stored_inputs.ohlc_bars(["AAPL"])["AAPL"]
    assert len(bars) == 270                                        # REACTION_OHLC_SESSIONS
    assert bars[0]["date"] < bars[-1]["date"]                      # oldest first
    assert bars[-1]["date"] == SESSION.isoformat()
    assert sorted(bars[-1]) == ["close", "date", "high", "low", "open", "volume"]
    assert all(isinstance(bars[-1][k], float) for k in ("open", "high", "low", "close", "volume"))


def test_a_split_in_the_window_is_applied_on_read():
    """D01 holds nominal bars and applies ``price_actions`` on read: the 2:1 split halves every bar before its
    ex-date, so the stored series is the split-adjusted one (the live UW answer is not). A dividend on the same
    ex-date must NOT touch it — the reader asks for the nominal basis, the one the pipeline's columns compare."""
    bars = _bars(6)
    rawstore.write_rows("price_actions", [{"ticker": "AAPL", "ex_date": date.fromisoformat(bars[4]["date"]),
                                           "action": "split", "value": 2.0, "source": "yfinance",
                                           "fetched_at": 0.0}])
    _seed(bars)

    got = {b["date"]: b for b in stored_inputs.ohlc_bars(["AAPL"])["AAPL"]}

    for b in bars[:4]:                                             # before the ex-date: halved
        assert got[b["date"]]["close"] == pytest.approx(b["close"] / 2)
        assert got[b["date"]]["volume"] == pytest.approx(b["volume"] * 2)
    for b in bars[4:]:                                             # on and after it: as served
        assert got[b["date"]]["close"] == pytest.approx(b["close"])
        assert got[b["date"]]["volume"] == pytest.approx(b["volume"])

    rawstore.write_rows("price_actions", [{"ticker": "AAPL", "ex_date": date.fromisoformat(bars[4]["date"]),
                                           "action": "dividend", "value": 1.0, "source": "yfinance",
                                           "fetched_at": 0.0}])
    with_dividend = {b["date"]: b for b in stored_inputs.ohlc_bars(["AAPL"])["AAPL"]}

    assert with_dividend == got                                    # the nominal basis ignores the dividend


def test_an_empty_store_is_an_empty_map():
    assert stored_inputs.ohlc_bars(["AAPL"]) == {}


def test_a_broken_store_is_an_empty_map(monkeypatch):
    from RevenueChaser.core import prices
    monkeypatch.setattr(prices, "daily_bars", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    assert stored_inputs.ohlc_bars(["AAPL"]) == {}


# ── in the US session the stored series ends yesterday: a name reporting now reads live (review M3) ───────────────

IN_SESSION = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)   # 11:00 New York, Wed: latest complete = Tue 10-13
TUESDAY = date(2026, 10, 13)


def _reporting_pipeline(fake, report_date, report_time="premarket"):
    p = EarningsPipeline(["AAPL"], uw=fake, cal_meta={"AAPL": {
        "earnings_date": report_date.isoformat(), "full_name": "Aple", "sector": "Technology",
        "expected_move_perc": 0.05, "report_time": report_time}})
    p._screener_cache = {"AAPL": {"close": "100", "call_volume": "0", "put_volume": "0",
                                  "total_open_interest": "0"}}
    p._dgs10_cache = {}
    return p


class _ReportingUW(_OhlcUW):
    """The UW answer during the session: the year of bars ends with today's (still forming) bar."""

    def __init__(self, bars, report_date, report_time="premarket"):
        super().__init__(bars)
        self._overrides["earnings_history"] = [
            {"report_date": report_date.isoformat(), "actual_eps": None, "street_mean_est": 1.0,
             "report_time": report_time}, *_prints()]


def test_a_name_reporting_today_reads_live_bars_in_session(monkeypatch):
    """Stored bars end Tue 10-13; 'Current' would compare that close with itself and read +0.0%."""
    monkeypatch.setattr(calendar, "_now", lambda: IN_SESSION)
    _seed(_bars(300, end=TUESDAY))
    fake = _ReportingUW(_bars(300, end=SESSION), SESSION)
    p = _reporting_pipeline(fake, SESSION)
    p._ohlc_bars = stored_inputs.ohlc_bars(["AAPL"])
    assert p._ohlc_bars["AAPL"][-1]["date"] == TUESDAY.isoformat()      # current by the stored rule

    card = p._score("AAPL")

    assert fake.requests == [("AAPL", REACTION_OHLC_SESSIONS)]
    assert p.input_sources["ohlc"] == {"stored": 0, "live": 1}
    assert card.earn_current_pct not in ("", "+0.0%")


def test_a_name_that_reported_after_yesterdays_close_reads_live_bars_in_session(monkeypatch):
    """AMC Tue 10-13: 'Since ER' anchors on Tuesday's close, the last stored bar, so it would read +0.0%."""
    monkeypatch.setattr(calendar, "_now", lambda: IN_SESSION)
    _seed(_bars(300, end=TUESDAY))
    fake = _ReportingUW(_bars(300, end=SESSION), TUESDAY, report_time="postmarket")
    p = _reporting_pipeline(fake, TUESDAY, report_time="postmarket")
    p._ohlc_bars = stored_inputs.ohlc_bars(["AAPL"])

    card = p._score("AAPL")

    assert fake.requests == [("AAPL", REACTION_OHLC_SESSIONS)]
    assert card.return_since_er and "+0.0%" not in card.return_since_er      # e.g. "10-13 M: +0.3%"


def test_a_later_report_keeps_the_stored_bars_in_session(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: IN_SESSION)
    _seed(_bars(300, end=TUESDAY))
    fake = _OhlcUW(_bars(300, end=SESSION))
    p = _reporting_pipeline(fake, date(2026, 10, 15))                # Thursday: no move to show yet
    p._ohlc_bars = stored_inputs.ohlc_bars(["AAPL"])
    p._score("AAPL")
    assert fake.requests == []
    assert p.input_sources["ohlc"] == {"stored": 1, "live": 0}


def test_after_the_close_a_name_reporting_today_keeps_the_stored_bars():
    """17:00 New York: the stored series already ends today, so it is the live answer."""
    _seed(_bars(300))
    fake = _ReportingUW(_bars(300), SESSION)
    p = _reporting_pipeline(fake, SESSION)
    p._ohlc_bars = stored_inputs.ohlc_bars(["AAPL"])
    p._score("AAPL")
    assert fake.requests == []


@pytest.mark.parametrize("now,printed,today", [
    # Mon 10-19 11:00 New York, Friday-AMC print: the previous trading day is three calendar days back
    (datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc), date(2026, 10, 16), date(2026, 10, 19)),
    # Fri 11-27 11:00 New York (after Thanksgiving), Wednesday-AMC print: the holiday sits in between
    (datetime(2026, 11, 27, 16, 0, tzinfo=timezone.utc), date(2026, 11, 25), date(2026, 11, 27)),
], ids=["monday-after-friday", "friday-after-thanksgiving"])
def test_a_print_on_the_previous_trading_day_reads_live_after_a_weekend_or_holiday(monkeypatch, now, printed,
                                                                                   today):
    """Review D-2: "yesterday" is the previous trading day, not the calendar day before (a Sunday, Thanksgiving)."""
    monkeypatch.setattr(calendar, "_now", lambda: now)
    assert calendar.latest_complete_session() == printed
    _seed(_bars(300, end=printed))
    fake = _ReportingUW(_bars(300, end=today), printed, report_time="postmarket")
    p = _reporting_pipeline(fake, printed, report_time="postmarket")
    p._ohlc_bars = stored_inputs.ohlc_bars(["AAPL"])

    card = p._score("AAPL")

    assert fake.requests == [("AAPL", REACTION_OHLC_SESSIONS)]
    assert card.return_since_er and "+0.0%" not in card.return_since_er
