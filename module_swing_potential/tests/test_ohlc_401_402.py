"""U1 (#402): ``load_ohlc`` resets the yfinance session once after Yahoo refuses the crumb, and retries.

The fake Yahoo refuses every read while yfinance's shared crumb is the old one (``yfinance.data.YfData()._crumb``), the
way it did on 2026-10-14 after 21:29Z, and answers once a reset has dropped that crumb. The reset is the real one, on
the installed yfinance; its on-disk cookie cache is faked. Offline.
"""
from __future__ import annotations

import logging
import time

import pandas as pd
import pytest

from RevenueChaser.module_swing_potential import indicators
from RevenueChaser.module_swing_potential.tests.test_ohlc import _df, _seed_ohlc

STALE_CRUMB = "stale-crumb"
PC_LINE = 'HTTP Error 401: {"finance":{"result":null,"error":{"code":"Unauthorized","description":"Invalid Crumb"}}}'


class Yahoo:
    def __init__(self, shared):
        self.shared = shared
        self.calls: list[str] = []
        self.refuse_new_crumb = False          # a 401 the reset does not cure
        self.raises = False                    # a yfinance that raises the 401 instead of logging it
        self.answers_after = float("inf")      # a test that could loop for ever ends it here: it fails, never hangs

    def refusing(self) -> bool:
        if len(self.calls) > self.answers_after:
            return False
        return self.shared._crumb == STALE_CRUMB or self.refuse_new_crumb

    def ticker(self, symbol):
        yahoo = self

        class _T:
            def history(self, *a, **k):
                yahoo.calls.append(symbol)
                if yahoo.refusing():
                    if yahoo.raises:
                        raise RuntimeError(PC_LINE)
                    logging.getLogger("yfinance").error(f"{symbol}: possibly delisted; no price data found  (period=2y)")
                    return pd.DataFrame()
                return _df(310)

            @property
            def info(self):
                if yahoo.refusing():
                    logging.getLogger("yfinance").error(PC_LINE)      # what yfinance's quote scraper logs
                    return {"trailingPegRatio": None}
                return {"longName": f"{symbol} Inc"}

        return _T()


class _CookieStore:
    def store(self, key, cookie):
        pass


@pytest.fixture
def yahoo(monkeypatch):
    from yfinance import cache as yf_cache
    from yfinance import data as yf_data
    shared = yf_data.YfData()
    monkeypatch.setattr(shared, "_crumb", STALE_CRUMB)
    monkeypatch.setattr(shared, "_cookie", True)
    monkeypatch.setattr(yf_cache, "get_cookie_cache", lambda: _CookieStore())
    fake = Yahoo(shared)
    monkeypatch.setattr("yfinance.Ticker", fake.ticker)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return fake


def _stale(ticker):
    from RevenueChaser.core import rawstore
    _seed_ohlc(ticker, _df(300))
    with rawstore.connect() as conn:
        conn.execute("UPDATE swing_ohlc SET cached_at = ? WHERE ticker = ?", [time.time() - 2 * 86400, ticker])


def test_the_14_oct_shape_an_info_401_then_a_failed_refetch_resets_once_and_the_retry_succeeds(rawstore_tmp_db, yahoo):
    """The 401 is logged by an ``.info`` read; the bar read only comes back empty. One reset, one retry."""
    _stale("AAA")
    indicators.load_ticker_info("ZZZ")                       # logs the 401, as on the PC at 21:29Z
    df = indicators.load_ohlc("AAA")
    assert yahoo.calls == ["AAA", "AAA"]
    assert len(df) == 310 and not df.attrs.get("stale")


def test_a_401_the_read_raises_resets_once_and_the_retry_succeeds(rawstore_tmp_db, yahoo):
    _stale("AAA")
    yahoo.raises = True
    df = indicators.load_ohlc("AAA")
    assert yahoo.calls == ["AAA", "AAA"]
    assert not df.attrs.get("stale")


def test_a_401_the_reset_does_not_cure_is_one_extra_read_then_the_stale_cache(rawstore_tmp_db, yahoo):
    _stale("AAA")
    yahoo.refuse_new_crumb = True
    yahoo.raises = True
    df = indicators.load_ohlc("AAA")
    assert yahoo.calls == ["AAA"] * 4                        # three reads as before, plus the one retry
    assert df.attrs.get("stale") is True


def test_the_next_ticker_gets_no_second_reset_while_yahoo_still_refuses(rawstore_tmp_db, yahoo):
    _stale("AAA")
    _stale("BBB")
    yahoo.refuse_new_crumb = True
    yahoo.raises = True
    indicators.load_ohlc("AAA")
    yahoo.calls.clear()
    df = indicators.load_ohlc("BBB")
    assert yahoo.calls == ["BBB"] * 3                        # the cooldown: the old three reads, no reset
    assert df.attrs.get("stale") is True


def test_a_rate_limit_is_not_a_401_and_the_reads_are_the_old_three(rawstore_tmp_db, monkeypatch):
    calls = []

    class _T:
        def __init__(self, t):
            pass

        def history(self, *a, **k):
            calls.append(1)
            raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")

    monkeypatch.setattr("yfinance.Ticker", _T)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    _stale("AAA")
    df = indicators.load_ohlc("AAA")
    assert len(calls) == 3
    assert df.attrs.get("stale") is True


def test_one_reset_per_call_even_with_no_cooldown(rawstore_tmp_db, yahoo, monkeypatch):
    """The per-call limit alone stops the loop: a 401 every reset fails to cure is never an endless retry."""
    from RevenueChaser.core.providers import yf_session
    monkeypatch.setattr(yf_session, "COOLDOWN_S", 0.0)
    _stale("AAA")
    yahoo.refuse_new_crumb = True
    yahoo.raises = True
    yahoo.answers_after = 10
    df = indicators.load_ohlc("AAA")
    assert yahoo.calls == ["AAA"] * 4
    assert df.attrs.get("stale") is True
