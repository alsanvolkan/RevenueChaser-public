"""Shared EDGAR client — throttle, backoff, User-Agent (no live network)."""
from __future__ import annotations

import httpx
import pytest

from RevenueChaser.core.providers import sec_edgar as se


class _Resp:
    def __init__(self, status=200, content=b"{}", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "http://x"),
                response=self,
            )


class _Client:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return self._responses.pop(0)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Record sleeps instead of performing them."""
    sleeps: list[float] = []
    monkeypatch.setattr(se.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


class TestThrottle:
    def test_interval_is_at_most_ten_per_second(self):
        assert se._THROTTLE_INTERVAL <= 0.10

    def test_second_call_sleeps(self, _no_sleep, monkeypatch):
        # The shared bucket absorbs a burst without sleeping. This test is the fallback:
        # when that file cannot be used, the in-process interval still spaces the second call.
        monkeypatch.setattr(se.sec_throttle, "pace", lambda: False)
        se._throttle_last[0] = 0.0
        se._throttle()
        se._throttle()
        assert _no_sleep and 0 < _no_sleep[0] <= se._THROTTLE_INTERVAL


class TestUserAgent:
    def test_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("RC_SEC_USER_AGENT", raising=False)
        assert se._headers()["User-Agent"] == se.SEC_USER_AGENT_DEFAULT

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("RC_SEC_USER_AGENT", "Jane Doe jane@example.com")
        assert se._headers()["User-Agent"] == "Jane Doe jane@example.com"

    def test_owned_request_sends_a_user_agent(self, monkeypatch):
        captured = {}

        def _fake_get(url, **kw):
            captured.update(kw)
            return _Resp(200, b"ok")

        monkeypatch.setattr(se.httpx, "get", _fake_get)
        se.get_bytes("http://x")
        assert "User-Agent" in captured.get("headers", {})


class TestBackoff:
    def test_429_then_success_is_retried(self, _no_sleep):
        client = _Client([_Resp(429), _Resp(200, b'{"ok": true}')])
        assert se.get_json("http://x", client) == {"ok": True}
        assert len(client.calls) == 2
        assert _no_sleep  # backed off once

    def test_retry_after_header_is_honored(self, _no_sleep):
        client = _Client([_Resp(429, headers={"Retry-After": "3"}), _Resp(200, b"{}")])
        se.get_bytes("http://x", client)
        assert 3.0 in _no_sleep  # the backoff, not just the throttle gap

    def test_5xx_is_retried(self, _no_sleep):
        client = _Client([_Resp(503), _Resp(502), _Resp(200, b"ok")])
        assert se.get_text("http://x", client) == "ok"
        assert len(client.calls) == 3

    def test_4xx_raises_immediately_without_retry(self, _no_sleep):
        client = _Client([_Resp(400)])
        with pytest.raises(httpx.HTTPStatusError):
            se.get_bytes("http://x", client)
        assert len(client.calls) == 1
        # only the throttle gap may have slept — no backoff
        assert all(s <= se._THROTTLE_INTERVAL for s in _no_sleep)

    def test_404_raises(self):
        client = _Client([_Resp(404)])
        with pytest.raises(httpx.HTTPStatusError):
            se.get_bytes("http://x", client)


class TestFetchFacts:
    def test_404_returns_none(self, monkeypatch):
        monkeypatch.setattr(se, "get_json", lambda url, client=None: (_ for _ in ()).throw(
            httpx.HTTPStatusError("404", request=httpx.Request("GET", "http://x"),
                                  response=_Resp(404))))
        assert se._fetch_facts(123) is None

    def test_other_errors_propagate(self, monkeypatch):
        def _boom(url, client=None):
            raise httpx.HTTPStatusError("500", request=httpx.Request("GET", "http://x"),
                                        response=_Resp(500))
        monkeypatch.setattr(se, "get_json", _boom)
        with pytest.raises(httpx.HTTPStatusError):
            se._fetch_facts(123)


@pytest.fixture(autouse=True)
def _fresh_breaker():
    se.reset_breaker()
    yield
    se.reset_breaker()


class TestRetryAfterCap:
    def test_huge_retry_after_is_capped(self, _no_sleep):
        client = _Client([_Resp(429, headers={"Retry-After": "86400"}), _Resp(200, b"{}")])
        se.get_bytes("http://x", client)
        waits = [s for s in _no_sleep if s > se._THROTTLE_INTERVAL]
        assert waits and max(waits) <= se._RETRY_AFTER_CAP

    def test_retry_after_below_cap_is_honored(self, _no_sleep):
        client = _Client([_Resp(429, headers={"Retry-After": "3"}), _Resp(200, b"{}")])
        se.get_bytes("http://x", client)
        assert 3.0 in _no_sleep

    def test_unparsable_retry_after_falls_back_to_backoff(self, _no_sleep):
        client = _Client([_Resp(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
                          _Resp(200, b"{}")])
        se.get_bytes("http://x", client)
        assert se._RETRY_BASE ** 0 in _no_sleep


class _FlakyClient:
    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def get(self, url):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestTransportRetries:
    def test_transport_error_is_retried_then_succeeds(self, _no_sleep):
        client = _FlakyClient([httpx.ConnectError("boom"), httpx.ReadTimeout("slow"),
                               _Resp(200, b"ok")])
        assert se.get_bytes("http://x", client) == b"ok"
        assert client.calls == 3
        assert any(s >= 1.0 for s in _no_sleep)  # backed off between attempts

    def test_transport_error_raises_after_max_retries(self):
        client = _FlakyClient([httpx.ConnectError("boom")] * se._MAX_RETRIES)
        with pytest.raises(httpx.ConnectError):
            se.get_bytes("http://x", client)
        assert client.calls == se._MAX_RETRIES


class TestBreaker:
    def _fail_once(self):
        client = _Client([_Resp(503)] * se._MAX_RETRIES)
        with pytest.raises(httpx.HTTPStatusError):
            se.get_bytes("http://x", client)

    def test_opens_after_n_consecutive_final_failures(self):
        for _ in range(se._BREAKER_THRESHOLD):
            self._fail_once()
        client = _Client([_Resp(200, b"ok")])
        with pytest.raises(se.SECUnavailable):
            se.get_bytes("http://x", client)
        assert client.calls == []  # no request while open

    def test_one_short_of_threshold_stays_closed(self):
        for _ in range(se._BREAKER_THRESHOLD - 1):
            self._fail_once()
        assert se.get_bytes("http://x", _Client([_Resp(200, b"ok")])) == b"ok"

    def test_success_resets_the_failure_count(self):
        for _ in range(se._BREAKER_THRESHOLD - 1):
            self._fail_once()
        se.get_bytes("http://x", _Client([_Resp(200, b"ok")]))
        for _ in range(se._BREAKER_THRESHOLD - 1):
            self._fail_once()
        assert se.get_bytes("http://x", _Client([_Resp(200, b"ok")])) == b"ok"

    def test_404_is_not_a_failure(self):
        for _ in range(se._BREAKER_THRESHOLD + 2):
            with pytest.raises(httpx.HTTPStatusError):
                se.get_bytes("http://x", _Client([_Resp(404)]))
        assert se.get_bytes("http://x", _Client([_Resp(200, b"ok")])) == b"ok"

    def test_403_opens_at_once(self):
        with pytest.raises(httpx.HTTPStatusError):
            se.get_bytes("http://x", _Client([_Resp(403)]))
        with pytest.raises(se.SECUnavailable):
            se.get_bytes("http://x", _Client([_Resp(200, b"ok")]))

    def test_transport_failures_count(self):
        for _ in range(se._BREAKER_THRESHOLD):
            with pytest.raises(httpx.ConnectError):
                se.get_bytes("http://x", _FlakyClient([httpx.ConnectError("x")] * se._MAX_RETRIES))
        with pytest.raises(se.SECUnavailable):
            se.get_bytes("http://x", _Client([_Resp(200, b"ok")]))

    def test_closes_after_cooldown(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(se, "_clock", lambda: now[0])
        with pytest.raises(httpx.HTTPStatusError):
            se.get_bytes("http://x", _Client([_Resp(403)]))
        now[0] += se._BREAKER_COOLDOWN + 1
        assert se.get_bytes("http://x", _Client([_Resp(200, b"ok")])) == b"ok"

    def test_reset_breaker_closes_it(self):
        with pytest.raises(httpx.HTTPStatusError):
            se.get_bytes("http://x", _Client([_Resp(403)]))
        se.reset_breaker()
        assert se.get_bytes("http://x", _Client([_Resp(200, b"ok")])) == b"ok"

    def test_open_state_is_visible(self):
        assert se.breaker_open() is False
        with pytest.raises(httpx.HTTPStatusError):
            se.get_bytes("http://x", _Client([_Resp(403)]))
        assert se.breaker_open() is True


class TestHeadersCleanup:
    def test_legacy_alias_is_gone(self):
        assert not hasattr(se, "SEC_HEADERS")

    def test_default_user_agent_warns_once(self, monkeypatch, caplog):
        import logging
        monkeypatch.delenv("RC_SEC_USER_AGENT", raising=False)
        monkeypatch.setattr(se, "_ua_warned", False)
        with caplog.at_level(logging.WARNING, logger=se.logger.name):
            se._headers()
            se._headers()
        hits = [r for r in caplog.records if "RC_SEC_USER_AGENT" in r.getMessage()]
        assert len(hits) == 1

    def test_set_user_agent_does_not_warn(self, monkeypatch, caplog):
        import logging
        monkeypatch.setenv("RC_SEC_USER_AGENT", "Jane jane@example.com")
        monkeypatch.setattr(se, "_ua_warned", False)
        with caplog.at_level(logging.WARNING, logger=se.logger.name):
            se._headers()
        assert not [r for r in caplog.records if "RC_SEC_USER_AGENT" in r.getMessage()]
