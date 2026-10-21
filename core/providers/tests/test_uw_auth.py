"""A rejected API key must be loud, not an empty board.

``UWClient._get`` swallows every exception by design (callers expect ``{}``),
which meant a 401 looked exactly like a quiet market. These tests pin the two
things that make it diagnosable: the client remembers the auth failure, and
``verify`` refuses up front with an actionable message.
"""
from __future__ import annotations

import logging

import httpx
import pytest

from RevenueChaser.core.providers.unusual_whales import UWClient


class _Resp:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload if payload is not None else {"data": []}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self)


def _client(monkeypatch, response, key: str = "k") -> UWClient:
    c = UWClient(key=key)
    monkeypatch.setattr(c._client, "get", lambda *a, **kw: response)
    return c


class TestGetRecordsAuthFailures:
    def test_401_sets_auth_error_and_still_returns_empty(self, monkeypatch):
        c = _client(monkeypatch, _Resp(401, text="token invalid"))
        assert c._get("/stock/AAPL/info") == {}
        assert c.auth_error["status"] == 401
        assert c.auth_error["endpoint"] == "/stock/AAPL/info"

    def test_403_is_also_an_auth_failure(self, monkeypatch):
        c = _client(monkeypatch, _Resp(403))
        c._get("/stock/AAPL/info")
        assert c.auth_error["status"] == 403

    def test_first_auth_failure_logs_the_actionable_message(self, monkeypatch, caplog):
        c = _client(monkeypatch, _Resp(401))
        with caplog.at_level(logging.ERROR):
            c._get("/stock/AAPL/info")
        assert "unusualwhales.com/api" in caplog.text
        assert "UNUSUAL_WHALES_API_KEY" in caplog.text

    def test_repeat_auth_failures_do_not_repeat_the_long_message(self, monkeypatch, caplog):
        c = _client(monkeypatch, _Resp(401))
        c._get("/a")
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            c._get("/b")
        assert "unusualwhales.com/api" not in caplog.text
        assert "credentials rejected" in caplog.text

    def test_non_auth_error_is_warned_but_not_recorded_as_auth(self, monkeypatch, caplog):
        c = _client(monkeypatch, _Resp(500, text="boom"))
        with caplog.at_level(logging.WARNING):
            assert c._get("/x") == {}
        assert c.auth_error is None
        assert "HTTP 500" in caplog.text

    def test_transport_error_is_warned_not_silent(self, monkeypatch, caplog):
        c = UWClient(key="k")

        def _boom(*a, **kw):
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(c._client, "get", _boom)
        with caplog.at_level(logging.WARNING):
            assert c._get("/x") == {}
        assert "ConnectError" in caplog.text
        assert c.auth_error is None

    def test_success_leaves_auth_error_unset(self, monkeypatch):
        c = _client(monkeypatch, _Resp(200, {"data": [1]}))
        assert c._get("/x") == {"data": [1]}
        assert c.auth_error is None


class TestVerify:
    def test_missing_key_is_reported_as_missing(self, monkeypatch):
        # ``UWClient(key="")`` falls back to the ambient key, so blank it.
        monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
        ok, message = UWClient().verify()
        assert ok is False
        assert "missing" in message.lower()
        assert "UNUSUAL_WHALES_API_KEY" in message

    def test_rejected_key_is_reported_as_rejected(self, monkeypatch):
        c = _client(monkeypatch, _Resp(401, text="nope"))
        ok, message = c.verify()
        assert ok is False
        assert "rejected" in message.lower()
        assert "HTTP 401" in message

    def test_valid_key_passes(self, monkeypatch):
        ok, message = _client(monkeypatch, _Resp(200)).verify()
        assert ok is True and message is None

    def test_network_failure_is_not_a_credential_verdict(self, monkeypatch):
        """A flaky probe must not block a run — the caller can fail on its own."""
        c = UWClient(key="k")

        def _boom(*a, **kw):
            raise httpx.ConnectError("offline")

        monkeypatch.setattr(c._client, "get", _boom)
        ok, message = c.verify()
        assert ok is True and message is None

    def test_result_is_cached_so_the_probe_runs_once(self, monkeypatch):
        c = UWClient(key="k")
        calls = []

        def _get(*a, **kw):
            calls.append(1)
            return _Resp(200)

        monkeypatch.setattr(c._client, "get", _get)
        c.verify()
        c.verify()
        assert len(calls) == 1

    def test_active_is_not_a_validity_check(self, monkeypatch):
        """``active`` only means "a key is present" — the old gate's weakness."""
        c = _client(monkeypatch, _Resp(401))
        assert c.active is True
        assert c.verify()[0] is False
