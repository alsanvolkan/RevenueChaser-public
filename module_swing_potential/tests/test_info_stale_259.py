"""#259: a failed or empty info refetch must not drop the expired cached info (stale beats empty)."""
from __future__ import annotations

import time

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_swing_potential import indicators
from RevenueChaser.module_swing_potential import pipeline
from RevenueChaser.module_swing_potential.tests.test_ohlc import _df


class _FailingTicker:
    def __init__(self, t):
        pass

    @property
    def info(self):
        raise RuntimeError("Too Many Requests. Rate limited.")


class _EmptyTicker:
    def __init__(self, t):
        pass

    @property
    def info(self):
        return {}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """yfinance goes through curl_cffi, so socket patches do not stop it: patch the entry points."""
    def boom(*a, **k):
        raise AssertionError("yfinance was called for real")
    monkeypatch.setattr("yfinance.download", boom)
    monkeypatch.setattr(time, "sleep", lambda s: None)


def _seed_info(ticker: str, payload: dict, age_days: float = 2.0):
    rawstore.write_rows("swing_info", [{
        "ticker": ticker, "payload": payload, "cached_at": time.time() - age_days * 86400,
    }])


def test_failed_refetch_serves_the_expired_info_marked_stale(rawstore_tmp_db, monkeypatch):
    _seed_info("AAPL", {"longName": "Apple Inc."})
    monkeypatch.setattr("yfinance.Ticker", _FailingTicker)
    info = indicators.load_ticker_info("AAPL")
    assert info["longName"] == "Apple Inc."
    assert info["_stale"] is True


def test_empty_refetch_serves_the_expired_info_marked_stale(rawstore_tmp_db, monkeypatch):
    _seed_info("AAPL", {"longName": "Apple Inc."})
    monkeypatch.setattr("yfinance.Ticker", _EmptyTicker)
    info = indicators.load_ticker_info("AAPL")
    assert info["longName"] == "Apple Inc."
    assert info["_stale"] is True


def test_the_stored_payload_is_not_mutated(rawstore_tmp_db, monkeypatch):
    _seed_info("AAPL", {"longName": "Apple Inc."})
    monkeypatch.setattr("yfinance.Ticker", _FailingTicker)
    indicators.load_ticker_info("AAPL")
    assert "_stale" not in rawstore.get("swing_info", "AAPL")["payload"]


def test_failed_refetch_without_a_stored_row_is_empty(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr("yfinance.Ticker", _FailingTicker)
    assert indicators.load_ticker_info("NOPE") == {}


def test_a_fresh_row_is_returned_without_calling_yfinance(rawstore_tmp_db, monkeypatch):
    _seed_info("AAPL", {"longName": "Apple Inc."}, age_days=0.0)
    monkeypatch.setattr("yfinance.Ticker",
                        lambda t: pytest.fail("yfinance must not be called for a fresh row"))
    assert indicators.load_ticker_info("AAPL") == {"longName": "Apple Inc."}


def test_the_pipeline_counts_the_stale_info_and_strips_the_marker(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr(pipeline.screen_common, "load_bars", lambda *a, **k: (_df(300), _df(300)))   # D01 (C12)
    monkeypatch.setattr(pipeline, "load_ticker_info",
                        lambda *a, **k: {"longName": "Apple Inc.", "industry": "Tech", "_stale": True})
    monkeypatch.setattr(pipeline, "compute_indicators",
                        lambda df, spy, **k: {"price": 100.0, "atr14": 2.0})
    row = pipeline._compute_row({"ticker": "AAPL"}, None)
    assert row["info_stale"] is True
    assert row["name"] == "Apple Inc."                       # the marker never reaches a card field
    payload = pipeline.build_payload([row], run_stats={})
    assert payload["data_quality"]["stale_info"] == 1


# ── review round (2026-10-14): a stub answer is no answer (F1) ─────────────────

class _StubTicker:
    """yfinance answers a hidden HTTP error (401/404/5xx) with a stub like this one."""

    def __init__(self, t):
        pass

    @property
    def info(self):
        return {"trailingPegRatio": None}


def test_a_stub_answer_keeps_the_expired_row_and_does_not_overwrite_it(rawstore_tmp_db, monkeypatch):
    _seed_info("AAPL", {"longName": "Apple Inc."})
    monkeypatch.setattr("yfinance.Ticker", _StubTicker)
    info = indicators.load_ticker_info("AAPL")
    assert info["longName"] == "Apple Inc." and info["_stale"] is True
    assert rawstore.get("swing_info", "AAPL")["payload"] == {"longName": "Apple Inc."}


def test_a_stub_answer_without_a_stored_row_is_empty_and_writes_nothing(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr("yfinance.Ticker", _StubTicker)
    assert indicators.load_ticker_info("NOPE") == {}
    assert rawstore.get("swing_info", "NOPE") is None


def test_a_real_answer_still_overwrites_an_expired_row(rawstore_tmp_db, monkeypatch):
    _seed_info("AAPL", {"longName": "Apple Inc."})

    class _Fresh:
        def __init__(self, t):
            pass

        @property
        def info(self):
            return {"longName": "Apple Inc. (new)", "trailingPegRatio": None}

    monkeypatch.setattr("yfinance.Ticker", _Fresh)
    info = indicators.load_ticker_info("AAPL")
    assert "_stale" not in info
    assert rawstore.get("swing_info", "AAPL")["payload"]["longName"] == "Apple Inc. (new)"


# ── review round 2 (2026-10-14) ───────────────────────────────────────────────

def test_the_stale_path_writes_nothing(rawstore_tmp_db, monkeypatch):
    _seed_info("AAPL", {"longName": "Apple Inc."})
    before = rawstore.get("swing_info", "AAPL")
    calls = []
    real = rawstore.write_rows
    monkeypatch.setattr(rawstore, "write_rows", lambda *a, **k: (calls.append(a[0]), real(*a, **k))[1])
    monkeypatch.setattr("yfinance.Ticker", _FailingTicker)
    assert indicators.load_ticker_info("AAPL")["_stale"] is True
    assert calls == [] and rawstore.get("swing_info", "AAPL") == before


class _PegStubTicker(_StubTicker):
    @property
    def info(self):
        return {"trailingPegRatio": 2.1}


def test_a_stub_with_a_peg_value_keeps_the_expired_row(rawstore_tmp_db, monkeypatch):
    _seed_info("AAPL", {"longName": "Apple Inc."})
    monkeypatch.setattr("yfinance.Ticker", _PegStubTicker)
    info = indicators.load_ticker_info("AAPL")
    assert info["longName"] == "Apple Inc." and info["_stale"] is True
    assert rawstore.get("swing_info", "AAPL")["payload"] == {"longName": "Apple Inc."}
