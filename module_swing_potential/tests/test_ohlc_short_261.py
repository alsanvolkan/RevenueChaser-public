"""#261: a short refetch must not replace a longer stored OHLC history."""
from __future__ import annotations

import time

import pandas as pd
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_swing_potential import indicators
from RevenueChaser.module_swing_potential import pipeline
from RevenueChaser.module_swing_potential.tests.test_ohlc import _df, _seed_ohlc


def _yahoo(frame):
    class _T:
        def __init__(self, t):
            pass

        def history(self, *a, **k):
            return frame

    return _T


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """yfinance goes through curl_cffi, so socket patches do not stop it: patch the entry points."""
    def boom(*a, **k):
        raise AssertionError("yfinance was called for real")
    monkeypatch.setattr("yfinance.download", boom)
    monkeypatch.setattr(time, "sleep", lambda s: None)


def _age_cache(ticker: str, days: float):
    with rawstore.connect() as conn:
        conn.execute("UPDATE swing_ohlc SET cached_at = ? WHERE ticker = ?",
                     [time.time() - days * 86400, ticker])


@pytest.fixture
def writes(monkeypatch):
    """Record every series ``_write_ohlc`` wrote. Since C8 (#383 R3) it goes through the one writer
    (``module_swing_potential/ohlc_cache.py``), which decides #261 itself: a refused series is called but not written."""
    seen: list[int] = []
    real = indicators._write_ohlc

    def spy(ticker, df):
        outcome = real(ticker, df)
        if outcome == "written":
            seen.append(len(df))
        return outcome

    monkeypatch.setattr(indicators, "_write_ohlc", spy)
    return seen


def test_a_short_refetch_over_a_long_history_is_refused(rawstore_tmp_db, monkeypatch, writes):
    _seed_ohlc("AAPL", _df(500))
    writes.clear()
    _age_cache("AAPL", 2)
    monkeypatch.setattr("yfinance.Ticker", _yahoo(_df(30)))
    df = indicators.load_ohlc("AAPL")
    assert len(df) == 500
    assert df.attrs.get("stale") is True
    assert df.attrs.get("short_refetch_refused") is True
    assert writes == []
    assert rawstore.count("swing_ohlc") == 500


def test_a_260_bar_refetch_over_500_writes_and_returns_the_new_frame(rawstore_tmp_db, monkeypatch, writes):
    _seed_ohlc("AAPL", _df(500))
    writes.clear()
    _age_cache("AAPL", 2)
    monkeypatch.setattr("yfinance.Ticker", _yahoo(_df(260)))
    df = indicators.load_ohlc("AAPL")
    assert len(df) == 260
    assert not df.attrs.get("short_refetch_refused")
    assert writes == [260]


def test_a_150_bar_refetch_over_100_stored_writes(rawstore_tmp_db, monkeypatch, writes):
    _seed_ohlc("AAPL", _df(100))
    writes.clear()
    _age_cache("AAPL", 2)
    monkeypatch.setattr("yfinance.Ticker", _yahoo(_df(150)))
    df = indicators.load_ohlc("AAPL")
    assert len(df) == 150
    assert not df.attrs.get("short_refetch_refused")
    assert writes == [150]


def test_a_30_bar_refetch_with_nothing_stored_writes(rawstore_tmp_db, monkeypatch, writes):
    monkeypatch.setattr("yfinance.Ticker", _yahoo(_df(30)))
    df = indicators.load_ohlc("NOPE")
    assert len(df) == 30
    assert not df.attrs.get("short_refetch_refused")
    assert writes == [30]


def test_the_pipeline_counts_a_refused_short_frame_as_stale(rawstore_tmp_db, monkeypatch):
    frame = _df(300)
    frame.attrs["stale"] = True
    frame.attrs["short_refetch_refused"] = True
    monkeypatch.setattr(pipeline.screen_common, "load_bars", lambda *a, **k: (frame, frame))
    monkeypatch.setattr(pipeline, "load_ticker_info", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "compute_indicators",
                        lambda df, spy, **k: {"price": 100.0, "atr14": 2.0})
    row = pipeline._compute_row({"ticker": "AAPL"}, None)
    assert "short_refetch_refused" not in row                   # D01 bars are never refused; SPY's flag counts (PR 6)
    assert row["ohlc_stale"] is True                            # a refused frame is still counted stale
    payload = pipeline.build_payload([row], run_stats={})
    assert payload["data_quality"]["short_refetch_refused"] == 0


# ── review round (2026-10-14): boundaries, equal counts, a reused symbol ──────

def _shifted(n: int, start: str):
    frame = _df(n)
    frame.index = pd.bdate_range(start, periods=n)
    return frame


@pytest.mark.parametrize("bars, refused", [(251, True), (252, False)])
def test_the_252_bar_boundary(rawstore_tmp_db, monkeypatch, writes, bars, refused):
    _seed_ohlc("AAPL", _df(500))
    writes.clear()
    _age_cache("AAPL", 2)
    monkeypatch.setattr("yfinance.Ticker", _yahoo(_df(bars)))
    df = indicators.load_ohlc("AAPL")
    assert bool(df.attrs.get("short_refetch_refused")) is refused
    assert len(df) == (500 if refused else bars)
    assert writes == ([] if refused else [bars])


def test_an_equal_bar_count_is_written(rawstore_tmp_db, monkeypatch, writes):
    _seed_ohlc("AAPL", _df(100))
    writes.clear()
    _age_cache("AAPL", 2)
    monkeypatch.setattr("yfinance.Ticker", _yahoo(_df(100)))
    df = indicators.load_ohlc("AAPL")
    assert not df.attrs.get("short_refetch_refused")
    assert writes == [100]


def test_a_short_refetch_with_no_shared_session_is_a_new_listing_and_is_written(rawstore_tmp_db, monkeypatch, writes):
    """A reused symbol: the old listing's bars must not be served stale (F2 default, 2026-10-14)."""
    _seed_ohlc("AAPL", _df(500))                                  # 2024-01-01 onwards
    writes.clear()
    _age_cache("AAPL", 2)
    monkeypatch.setattr("yfinance.Ticker", _yahoo(_shifted(30, "2027-06-01")))
    df = indicators.load_ohlc("AAPL")
    assert not df.attrs.get("short_refetch_refused")
    assert len(df) == 30
    assert writes == [30]


def test_one_shared_session_is_enough_to_refuse(rawstore_tmp_db, monkeypatch, writes):
    stored = _df(500)
    _seed_ohlc("AAPL", stored)
    writes.clear()
    _age_cache("AAPL", 2)
    last = stored.index[-1].strftime("%Y-%m-%d")
    monkeypatch.setattr("yfinance.Ticker", _yahoo(_shifted(30, last)))  # starts on the last stored session
    df = indicators.load_ohlc("AAPL")
    assert df.attrs.get("short_refetch_refused") is True
    assert writes == []
