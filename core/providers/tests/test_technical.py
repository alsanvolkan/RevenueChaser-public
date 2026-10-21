"""technical.py tests — SMA/volume-ratio/52w-distance derivation (synthetic yfinance, no network)."""
from __future__ import annotations

import pandas as pd

from RevenueChaser.core.providers import technical as tech


class _FakeTicker:
    def __init__(self, info=None, hist=None, info_error=False):
        self._info = info or {}
        self._hist = hist if hist is not None else pd.DataFrame()
        self._info_error = info_error

    @property
    def info(self):
        if self._info_error:
            raise RuntimeError("network down")
        return self._info

    def history(self, period=None):
        return self._hist


def _hist(n: int, base_close: float = 100.0) -> pd.DataFrame:
    dates = pd.date_range("2023-01-01", periods=n, freq="D")
    closes = [base_close + i for i in range(n)]
    highs = [c + 1 for c in closes]
    volumes = [1000 + i * 10 for i in range(n)]
    return pd.DataFrame({"Close": closes, "High": highs, "Volume": volumes}, index=dates)


def test_fetch_technical_swallows_exception_and_returns_defaults(monkeypatch):
    fake = _FakeTicker(info_error=True)
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: fake)

    out = tech.fetch_technical("ZZZZ")
    assert out == {
        "price": None, "high_52w": None, "dist_52w_pct": None,
        "sma50": None, "above_sma50": None,
        "vol_10d": None, "vol_90d": None, "vol_ratio": None,
    }


def test_fetch_technical_happy_path_full_history(monkeypatch):
    hist = _hist(100, base_close=100.0)
    fake = _FakeTicker(info={"currentPrice": 199.0, "fiftyTwoWeekHigh": 250.0}, hist=hist)
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: fake)

    out = tech.fetch_technical("AAPL")

    assert out["price"] == 199.0
    assert out["high_52w"] == 250.0
    assert out["dist_52w_pct"] == (250.0 - 199.0) / 250.0
    assert out["sma50"] is not None
    assert out["above_sma50"] is True  # price 199 > sma50 of an upward-trending series
    assert out["vol_10d"] is not None
    assert out["vol_90d"] is not None
    assert out["vol_ratio"] == out["vol_10d"] / out["vol_90d"]


def test_fetch_technical_insufficient_history_for_sma_and_90d_volume(monkeypatch):
    hist = _hist(20)  # < 50 rows and < 90 rows
    fake = _FakeTicker(info={}, hist=hist)
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: fake)

    out = tech.fetch_technical("ZZZZ")

    assert out["sma50"] is None
    assert out["above_sma50"] is None
    assert out["vol_90d"] is None
    assert out["vol_ratio"] is None
    # 20 rows is still >= 10, so vol_10d should be derivable
    assert out["vol_10d"] is not None
    # price/high_52w derived from history when info is empty
    assert out["price"] == hist["Close"].iloc[-1]
    assert out["high_52w"] == hist["High"].max()


def test_fetch_technical_empty_history_uses_info_only(monkeypatch):
    fake = _FakeTicker(info={"currentPrice": 50.0, "fiftyTwoWeekHigh": 60.0}, hist=pd.DataFrame())
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: fake)

    out = tech.fetch_technical("ZZZZ")

    assert out["price"] == 50.0
    assert out["high_52w"] == 60.0
    assert out["dist_52w_pct"] == (60.0 - 50.0) / 60.0
    assert out["sma50"] is None
    assert out["vol_10d"] is None


def test_fetch_technical_no_high_52w_no_price_returns_no_distance(monkeypatch):
    fake = _FakeTicker(info={}, hist=pd.DataFrame())
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: fake)

    out = tech.fetch_technical("ZZZZ")
    assert out["price"] is None
    assert out["high_52w"] is None
    assert out["dist_52w_pct"] is None
