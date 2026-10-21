"""Daily OHLC normalisation (plan MB4 / review X2).

Unusual Whales returns up to three rows per day on its daily candle endpoint —
pre-market (``pr``), regular (``r``) and after-hours (``po``) — sometimes
newest-first. Every consumer needs one regular-session bar per trading day,
oldest first; this is now done once, in the provider.
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.core.providers import ohlc as ohlc_mod
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.core.providers.unusual_whales import UWClient
from RevenueChaser.core.vol_metrics import clean_ohlc


def _bar(d, mt, close, vol=100, o=None):
    o = close if o is None else o
    return {"date": d, "market_time": mt, "open": o, "high": close + 1,
            "low": close - 1, "close": close, "volume": vol}


# Newest-first, three sessions a day, and today (10-01) only pre-market so far.
RAW = [
    _bar("2026-10-01", "pr", 99.0),
    _bar("2026-09-30", "po", 51.0, vol=5),
    _bar("2026-09-30", "r", 50.0, vol=1000),
    _bar("2026-09-30", "pr", 49.0, vol=7),
    _bar("2026-09-29", "po", 41.0),
    _bar("2026-09-29", "r", 40.0),
    _bar("2026-09-29", "pr", 39.0),
    _bar("2026-09-26", "r", 30.0),
]


def test_one_regular_bar_per_day_oldest_first():
    bars = ohlc_mod.regular_session_bars(RAW)
    assert [(b["date"], b["close"]) for b in bars] == [
        ("2026-09-26", 30.0), ("2026-09-29", 40.0), ("2026-09-30", 50.0)]
    assert bars[-1]["volume"] == 1000            # regular-session volume, not after-hours


def test_a_day_with_only_extended_hours_rows_is_not_a_bar():
    # 10-01 has only a pre-market print: an intraday, incomplete day.
    assert "2026-10-01" not in [b["date"] for b in ohlc_mod.regular_session_bars(RAW)]


def test_rows_without_a_session_field_are_regular_and_deduped_by_date():
    rows = [{"date": "2026-09-30", "close": 5.0}, {"date": "2026-09-29", "close": 4.0},
            {"date": "2026-09-30", "close": 6.0}, {"date": "2026-09-28", "close": None}]
    bars = ohlc_mod.regular_session_bars(rows)
    assert [(b["date"], b["close"]) for b in bars] == [("2026-09-29", 4.0), ("2026-09-30", 6.0)]


def test_datetime_field_and_garbage_rows_are_handled():
    rows = [{"datetime": "2026-09-30T00:00:00Z", "market_time": "r", "close": "7.5"},
            "not a dict", {"market_time": "r", "close": 1.0}]
    bars = ohlc_mod.regular_session_bars(rows)
    assert [(b["date"], b["close"]) for b in bars] == [("2026-09-30", "7.5")]


class _Fake(UWClient):
    def __init__(self, rows):
        self.seen = []
        self._rows = rows

    def get_result(self, path, params=None):
        self.seen.append((path, dict(params or {})))
        return ProviderResult("ok", {"data": list(self._rows)}, None, path)


def test_etf_ohlc_asks_for_enough_rows_and_returns_sessions():
    uw = _Fake(RAW)
    bars = uw.etf_ohlc("SPY", days=2)
    # ``limit`` counts rows (up to 3 a day), so ask for 3× and trim to sessions.
    assert uw.seen[0][1]["limit"] == 6
    assert [b["date"] for b in bars] == ["2026-09-29", "2026-09-30"]


def test_weekly_candles_are_deduped_and_sorted_but_not_session_filtered():
    rows = [{"date": "2026-09-28", "close": 2.0}, {"date": "2026-09-21", "close": 1.0}]
    uw = _Fake(rows)
    bars = uw.etf_ohlc("SPY", days=156, candle_size="1w")
    assert uw.seen[0][1]["limit"] == 156
    assert [b["close"] for b in bars] == [1.0, 2.0]


def test_ohlc_daily_and_latest_use_the_regular_session():
    uw = _Fake(RAW)
    assert [b["date"] for b in uw.ohlc_daily("SPY", days=500)] == [
        "2026-09-26", "2026-09-29", "2026-09-30"]
    latest = uw.ohlc_latest("SPY")
    assert latest["date"] == "2026-09-30" and latest["market_time"] == "r"
    assert latest["volume"] == 1000


def test_clean_ohlc_is_session_safe_for_stored_rows_too():
    bars = clean_ohlc(RAW, as_of=date(2026, 10, 1))
    assert [b["close"] for b in bars] == [30.0, 40.0, 50.0]   # oldest first, regular only
