"""U1 (#402): one yfinance session reset after Yahoo refuses the crumb (HTTP 401 "Invalid Crumb").

``yf_session.recover`` decides, ``yf_session._reset`` drops yfinance's cookie and crumb. Offline: ``_reset`` is checked
against the installed yfinance with its on-disk cookie cache faked; everything else fakes ``_reset``.
"""
from __future__ import annotations

import logging

import pytest

from RevenueChaser.core.providers import yf_session

PC_LINE = 'HTTP Error 401: {"finance":{"result":null,"error":{"code":"Unauthorized","description":"Invalid Crumb"}}}'


@pytest.fixture
def resets(monkeypatch):
    calls = []

    def fake():
        calls.append("reset")
        return ["crumb", "cookie"]

    monkeypatch.setattr(yf_session, "_reset", fake)
    return calls


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(yf_session, "clock", lambda: now["t"])
    return now


def _yahoo_logs_401():
    logging.getLogger("yfinance").error(PC_LINE)


@pytest.mark.parametrize("text", [
    PC_LINE,
    "401 Client Error: Unauthorized for url: https://query2.finance.yahoo.com/v10/finance/quoteSummary/AAPL",
    "AAPL: possibly delisted; no price data found  (period=2y) (Yahoo status_code = 401)",
    "Invalid Crumb",
])
def test_a_401_is_recognised(text):
    assert yf_session.is_auth_error(text)


@pytest.mark.parametrize("text", [
    "Too Many Requests. Rate limited. Try after a while.",
    "AAPL: possibly delisted; no price data found  (period=2y)",
    "HTTP Error 404: Not Found",
    "AAPL: price 401.25 is out of range",
    None,
])
def test_other_failures_are_not_a_401(text):
    assert not yf_session.is_auth_error(text)


def test_the_watcher_notes_a_401_on_the_yfinance_logger_and_drops_nothing(caplog, resets, clock):
    with caplog.at_level(logging.ERROR, logger="yfinance"):
        _yahoo_logs_401()
    assert any("Invalid Crumb" in r.getMessage() for r in caplog.records)      # still logged
    assert yf_session.recover(started=999.0) is True
    assert resets == ["reset"]


def test_a_failure_with_no_401_resets_nothing(resets, clock):
    assert yf_session.recover(started=999.0) is False
    assert yf_session.recover(started=999.0, error=RuntimeError("Too Many Requests. Rate limited.")) is False
    assert resets == []


def test_a_401_raised_by_the_read_itself_resets(resets, clock):
    assert yf_session.recover(started=999.0, error=RuntimeError(PC_LINE)) is True
    assert resets == ["reset"]


def test_one_reset_serves_every_read_that_was_in_flight(resets, clock):
    _yahoo_logs_401()
    clock["t"] = 1001.0
    assert yf_session.recover(started=1000.0) is True           # resets
    clock["t"] = 1002.0
    assert yf_session.recover(started=1000.5) is True           # began before the reset: just retry
    assert resets == ["reset"]


def test_a_401_after_a_fresh_reset_is_left_alone_until_the_cooldown_ends(resets, clock):
    _yahoo_logs_401()
    assert yf_session.recover(started=999.0) is True
    clock["t"] = 1010.0
    _yahoo_logs_401()                                            # the new crumb is refused too
    clock["t"] = 1011.0
    assert yf_session.recover(started=1010.0) is False          # no second reset inside the cooldown
    clock["t"] = 1000.0 + yf_session.COOLDOWN_S + 1
    assert yf_session.recover(started=clock["t"] - 0.5) is True  # past it, a pending 401 resets again
    assert resets == ["reset", "reset"]


def test_after_a_reset_a_failure_with_no_new_401_resets_nothing(resets, clock):
    _yahoo_logs_401()
    assert yf_session.recover(started=999.0) is True
    clock["t"] = 1000.0 + yf_session.COOLDOWN_S + 1
    assert yf_session.recover(started=clock["t"] - 0.5) is False
    assert resets == ["reset"]


# ── the reset itself, on the installed yfinance ──────────────────────────────

class _CookieStore:
    def __init__(self):
        self.calls = []

    def store(self, key, cookie):
        self.calls.append((key, cookie))


def test_the_reset_drops_the_shared_crumb_cookie_and_saved_cookie(monkeypatch):
    from yfinance import cache as yf_cache
    from yfinance import data as yf_data
    shared = yf_data.YfData()
    monkeypatch.setattr(shared, "_crumb", "stale-crumb")
    monkeypatch.setattr(shared, "_cookie", True)
    shared._session.cookies.set("A3", "stale", domain=".yahoo.com")
    saved = _CookieStore()
    monkeypatch.setattr(yf_cache, "get_cookie_cache", lambda: saved)
    cleared = yf_session._reset()
    assert shared._crumb is None and shared._cookie is None
    assert len(shared._session.cookies) == 0
    assert {k for k, v in saved.calls if v is None} >= {"curlCffi", "basic", "csrf"}
    assert cleared == ["crumb", "cookie", "session cookies", "saved cookie"]


def test_the_reset_never_raises_when_yfinance_has_no_such_parts(monkeypatch):
    from yfinance import cache as yf_cache
    from yfinance import data as yf_data

    def no_shared():
        raise AttributeError("module 'yfinance.data' has no attribute 'YfData'")

    def no_cache():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(yf_data, "YfData", no_shared)
    monkeypatch.setattr(yf_cache, "get_cookie_cache", no_cache)
    assert yf_session._reset() == []
