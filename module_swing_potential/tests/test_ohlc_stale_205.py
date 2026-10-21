"""#205: a failed refetch must not drop a cached OHLC series (stale beats empty)."""
from __future__ import annotations

import time

import pandas as pd
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_swing_potential import indicators
from RevenueChaser.module_swing_potential.tests.test_ohlc import _df, _seed_ohlc


class _FailingTicker:
    calls = 0

    def __init__(self, t):
        pass

    def history(self, *a, **k):
        type(self).calls += 1
        raise RuntimeError("Too Many Requests. Rate limited.")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """yfinance goes through curl_cffi, so socket patches do not stop it: patch the entry points."""
    def boom(*a, **k):
        raise AssertionError("yfinance was called for real")
    monkeypatch.setattr("yfinance.Ticker", boom)
    monkeypatch.setattr("yfinance.download", boom)
    monkeypatch.setattr(time, "sleep", lambda s: None)


def _age_cache(ticker: str, days: float):
    with rawstore.connect() as conn:
        conn.execute("UPDATE swing_ohlc SET cached_at = ? WHERE ticker = ?",
                     [time.time() - days * 86400, ticker])


def test_failed_refetch_serves_the_stale_cache(rawstore_tmp_db, monkeypatch):
    _seed_ohlc("AAPL", _df(300))
    _age_cache("AAPL", 2)
    monkeypatch.setattr("yfinance.Ticker", _FailingTicker)
    df = indicators.load_ohlc("AAPL")
    assert len(df) == 300
    assert df.attrs.get("stale") is True
    assert "Close" in df.columns


def test_failed_refetch_without_cache_is_empty_and_flagged(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr("yfinance.Ticker", _FailingTicker)
    df = indicators.load_ohlc("NOPE")
    assert df.empty
    assert df.attrs.get("fetch_failed") is True


def test_successful_refetch_replaces_the_cache_and_is_not_stale(rawstore_tmp_db, monkeypatch):
    _seed_ohlc("AAPL", _df(300))
    _age_cache("AAPL", 2)
    fresh = _df(310)

    class _T:
        def __init__(self, t):
            pass

        def history(self, *a, **k):
            return fresh

    monkeypatch.setattr("yfinance.Ticker", _T)
    df = indicators.load_ohlc("AAPL")
    assert len(df) == 310
    assert not df.attrs.get("stale")


def test_fresh_cache_is_not_marked_stale(rawstore_tmp_db):
    _seed_ohlc("AAPL", _df(300))
    df = indicators.load_ohlc("AAPL")
    assert not df.attrs.get("stale")
