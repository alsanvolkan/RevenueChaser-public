"""PL1 / ADR-1 — a provider outage must not look like "no data".

``UWClient._get`` keeps its old contract (a dict/list, ``{}`` on final failure)
so no caller breaks, but it now retries transient failures (429, 5xx,
timeouts, connect errors) and records every final failure on the client, where
``health()`` exposes it to the job and the board. ``get_result`` is the typed
``ProviderResult`` API for new code.
"""
from __future__ import annotations

import random

import httpx
import pytest

from RevenueChaser.core.providers import unusual_whales as uwmod
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.core.providers.unusual_whales import UWClient


class _Resp:
    def __init__(self, status: int, payload=None, text: str = "", headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"data": []}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self)


@pytest.fixture(autouse=True)
def _full_step_delay(monkeypatch):
    """Jitter off by default: ``_random() == 1`` makes every delay the full exponential step, so the
    delay assertions below stay exact. The jitter tests set their own value."""
    monkeypatch.setattr(uwmod, "_random", lambda: 1.0)


def _scripted(monkeypatch, *outcomes, key: str = "k"):
    """A client whose transport returns/raises ``outcomes`` in order; the
    injected sleep records delays instead of waiting."""
    sleeps: list[float] = []
    c = UWClient(key=key, sleep=sleeps.append)
    seq = list(outcomes)
    calls = []

    def _get(url, params=None):
        calls.append(url)
        out = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(c._client, "get", _get)
    return c, sleeps, calls


class TestRetry:
    def test_retries_a_5xx_then_succeeds(self, monkeypatch):
        c, sleeps, calls = _scripted(monkeypatch, _Resp(503), _Resp(200, {"data": [1]}))
        assert c._get("/stock/AAPL/ohlc") == {"data": [1]}
        assert len(calls) == 2
        assert sleeps == [1.0]
        assert c.health()["failed"] == 0

    def test_retries_timeouts_and_connect_errors(self, monkeypatch):
        c, sleeps, calls = _scripted(
            monkeypatch, httpx.ReadTimeout("slow"), httpx.ConnectError("down"),
            _Resp(200, {"data": [2]}))
        assert c._get("/x") == {"data": [2]}
        assert len(calls) == 3
        assert sleeps == [1.0, 2.0]  # exponential backoff

    def test_429_honours_retry_after(self, monkeypatch):
        c, sleeps, _ = _scripted(
            monkeypatch, _Resp(429, headers={"Retry-After": "7"}), _Resp(200, {"data": []}))
        c._get("/x")
        assert sleeps == [7.0]

    def test_retry_after_is_capped(self, monkeypatch):
        c, sleeps, _ = _scripted(
            monkeypatch, _Resp(429, headers={"Retry-After": "3600"}), _Resp(200, {"data": []}))
        c._get("/x")
        assert sleeps == [30.0]

    def test_unparseable_retry_after_falls_back_to_backoff(self, monkeypatch):
        c, sleeps, _ = _scripted(
            monkeypatch, _Resp(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            _Resp(200, {"data": []}))
        c._get("/x")
        assert sleeps == [1.0]

    def test_gives_up_after_three_attempts(self, monkeypatch):
        c, sleeps, calls = _scripted(monkeypatch, _Resp(500, text="boom"))
        assert c._get("/x") == {}
        assert len(calls) == 3
        assert len(sleeps) == 2  # no sleep after the last attempt

    def test_a_404_is_not_retried(self, monkeypatch):
        c, sleeps, calls = _scripted(monkeypatch, _Resp(404))
        assert c._get("/x") == {}
        assert len(calls) == 1 and sleeps == []

    def test_module_sleep_is_patchable(self, monkeypatch):
        slept = []
        monkeypatch.setattr(uwmod, "_sleep", slept.append)
        c = UWClient(key="k")
        seq = [_Resp(502), _Resp(200, {"data": []})]
        monkeypatch.setattr(c._client, "get", lambda *a, **kw: seq.pop(0))
        c._get("/x")
        assert slept == [1.0]


class TestHealth:
    def test_clean_client_reports_nothing_failed(self):
        h = UWClient(key="k").health()
        assert h == {"calls": 0, "failed": 0, "failures": [], "auth_error": None,
                     "breaker_open": False, "skipped_calls": 0}

    def test_final_failure_is_recorded_while_the_return_value_stays_empty(self, monkeypatch):
        c, _, _ = _scripted(monkeypatch, _Resp(429, text="slow down"))
        assert c._get("/stock/XYZ/ohlc", {"limit": 5}) == {}
        h = c.health()
        assert h["calls"] == 1 and h["failed"] == 1
        f = h["failures"][0]
        assert f["endpoint"] == "/stock/XYZ/ohlc"
        assert f["status"] == 429
        assert f["attempts"] == 3

    def test_transport_failure_records_the_error_kind(self, monkeypatch):
        c, _, _ = _scripted(monkeypatch, httpx.ConnectError("down"))
        assert c._get("/x") == {}
        f = c.health()["failures"][0]
        assert f["status"] is None and f["error"] == "ConnectError"

    def test_endpoint_never_carries_the_key(self, monkeypatch):
        c, _, _ = _scripted(monkeypatch, _Resp(500), key="SECRET-KEY-123")
        c._get("/x")
        assert "SECRET-KEY-123" not in repr(c.health())

    def test_failures_list_is_capped_at_20_but_counted_in_full(self, monkeypatch):
        c, _, _ = _scripted(monkeypatch, _Resp(500))
        for i in range(25):
            c._get(f"/e/{i}")
        h = c.health()
        assert h["calls"] == 25 and h["failed"] == 25
        assert len(h["failures"]) == 20

    def test_successes_count_as_calls(self, monkeypatch):
        c, _, _ = _scripted(monkeypatch, _Resp(200, {"data": [1]}))
        c._get("/a")
        c._get("/b")
        assert c.health()["calls"] == 2 and c.health()["failed"] == 0


class TestAuthUnchanged:
    def test_401_sets_auth_error_is_not_retried_and_shows_in_health(self, monkeypatch):
        c, sleeps, calls = _scripted(monkeypatch, _Resp(401, text="token invalid"))
        assert c._get("/stock/AAPL/info") == {}
        assert len(calls) == 1 and sleeps == []
        assert c.auth_error["status"] == 401
        assert c.auth_error["endpoint"] == "/stock/AAPL/info"
        h = c.health()
        assert h["auth_error"]["status"] == 401
        assert h["failed"] == 1


class TestProviderResult:
    def test_ok(self, monkeypatch):
        c, _, _ = _scripted(monkeypatch, _Resp(200, {"data": [1]}))
        r = c.get_result("/x")
        assert isinstance(r, ProviderResult)
        assert r.status == "ok" and r.ok
        assert r.data == {"data": [1]} and r.error is None and r.endpoint == "/x"

    def test_failed_carries_the_error(self, monkeypatch):
        c, _, _ = _scripted(monkeypatch, _Resp(503))
        r = c.get_result("/x", {"a": 1})
        assert r.status == "failed" and not r.ok
        assert r.data == {}
        assert r.error["status"] == 503 and r.error["attempts"] == 3
        assert c.health()["failed"] == 1

    def test_partial_is_a_valid_status(self):
        r = ProviderResult(status="partial", data=[1], error={"failed": 2})
        assert not r.ok

    def test_unknown_status_is_rejected(self):
        with pytest.raises(ValueError):
            ProviderResult(status="maybe", data=None)


class TestJitter:
    """Full jitter: the delay is a random fraction of the exponential step, so the 8 job workers that
    hit a 429 together do not all retry together. ``Retry-After`` is the server's instruction and is
    honoured as sent."""

    def test_the_delay_is_a_random_fraction_of_the_exponential_step(self, monkeypatch):
        monkeypatch.setattr(uwmod, "_random", lambda: 0.25)
        c, sleeps, _ = _scripted(monkeypatch, _Resp(503), _Resp(503), _Resp(200, {"data": []}))
        c._get("/x")
        assert sleeps == [0.25 * 1.0, 0.25 * 2.0]

    def test_the_bounds_are_zero_and_the_full_step(self, monkeypatch):
        for value, expected in ((0.0, [0.0, 0.0]), (1.0, [1.0, 2.0])):
            monkeypatch.setattr(uwmod, "_random", lambda v=value: v)
            c, sleeps, _ = _scripted(monkeypatch, _Resp(503), _Resp(503), _Resp(200, {"data": []}))
            c._get("/x")
            assert sleeps == expected

    def test_with_the_real_generator_every_delay_stays_inside_its_step(self, monkeypatch):
        monkeypatch.setattr(uwmod, "_random", random.random)
        first = set()
        for _ in range(60):
            c, sleeps, _ = _scripted(monkeypatch, _Resp(503), _Resp(503), _Resp(200, {"data": []}))
            c._get("/x")
            assert 0.0 <= sleeps[0] <= 1.0 and 0.0 <= sleeps[1] <= 2.0
            first.add(sleeps[0])
        assert len(first) > 1                      # not a constant: the delay really is random

    def test_retry_after_is_not_jittered(self, monkeypatch):
        monkeypatch.setattr(uwmod, "_random", lambda: 0.1)
        c, sleeps, _ = _scripted(
            monkeypatch, _Resp(429, headers={"Retry-After": "7"}), _Resp(200, {"data": []}))
        c._get("/x")
        assert sleeps == [7.0]


def _routed(monkeypatch, route, key: str = "k"):
    """A client whose transport answers ``route(url)`` (a response or an exception to raise), and the
    list of URLs that actually went out."""
    c = UWClient(key=key, sleep=lambda s: None)
    calls: list[str] = []

    def _get(url, params=None):
        calls.append(url)
        out = route(url)
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(c._client, "get", _get)
    return c, calls


def _always(out):
    return lambda url: out


class TestBreaker:
    """After ``BREAKER_THRESHOLD`` consecutive *final* 429/5xx/transport failures a client stops
    sending requests: a sustained outage would otherwise cost every remaining call its full retry
    ladder (~400 calls x 3 attempts in a holdings sync). The breaker lives for the client's life;
    a job builds a fresh client."""

    def test_five_final_failures_open_it_and_the_sixth_call_sends_nothing(self, monkeypatch):
        c, calls = _routed(monkeypatch, _always(_Resp(503)))
        for i in range(5):
            assert c.get_result(f"/e/{i}").status == "failed"
        assert len(calls) == 5 * uwmod.MAX_ATTEMPTS
        r = c.get_result("/e/6")
        assert r.status == "failed" and r.data == {}
        assert r.error["error"] == "breaker_open" and r.error["endpoint"] == "/e/6"
        assert len(calls) == 5 * uwmod.MAX_ATTEMPTS         # no HTTP for the 6th call
        assert c._get("/e/7") == {}                          # the dict wrapper too
        assert len(calls) == 5 * uwmod.MAX_ATTEMPTS

    def test_four_failures_leave_it_closed(self, monkeypatch):
        c, calls = _routed(monkeypatch, lambda url: _Resp(503) if "bad" in url else _Resp(200, {"data": [1]}))
        for i in range(4):
            c.get_result(f"/bad/{i}")
        assert c.health()["breaker_open"] is False
        assert c.get_result("/good").status == "ok"

    def test_every_kind_of_provider_failure_counts(self, monkeypatch):
        outcomes = {"/a": _Resp(429), "/b": httpx.ConnectError("down"), "/c": httpx.ReadTimeout("slow"),
                    "/d": _Resp(500), "/e": httpx.ReadError("reset")}
        c, calls = _routed(monkeypatch, lambda url: outcomes[url.removeprefix(uwmod.BASE)])
        for ep in outcomes:
            c.get_result(ep)
        assert c.health()["breaker_open"] is True
        n = len(calls)
        assert c.get_result("/a").error["error"] == "breaker_open" and len(calls) == n

    def test_a_success_in_between_resets_the_counter(self, monkeypatch):
        c, calls = _routed(monkeypatch, lambda url: _Resp(200, {"data": [1]}) if "ok" in url else _Resp(503))
        for i in range(4):
            c.get_result(f"/bad/{i}")
        assert c.get_result("/ok").status == "ok"
        for i in range(4):
            c.get_result(f"/bad/{i}")
        assert c.health()["breaker_open"] is False
        n = len(calls)
        c.get_result("/bad/last")                            # the 5th since the success: opens now
        assert c.health()["breaker_open"] is True and len(calls) == n + uwmod.MAX_ATTEMPTS

    def test_a_404_is_the_requests_own_fault_and_never_counts(self, monkeypatch):
        c, calls = _routed(monkeypatch, _always(_Resp(404)))
        for i in range(12):
            assert c.get_result(f"/stock/NOPE{i}/info").status == "failed"
        assert len(calls) == 12                              # every one went out
        assert c.health()["breaker_open"] is False and c.health()["failed"] == 0

    def test_a_200_with_invalid_json_neither_counts_nor_resets(self, monkeypatch):
        """A body that is not JSON is a failed result for the caller, but it does not say the provider is down
        (so it never opens the breaker) or that it is up (so it never resets the count)."""
        class _NotJson(_Resp):
            def json(self):
                raise ValueError("not json")

        route = lambda url: _NotJson(200) if "junk" in url else _Resp(503)       # noqa: E731
        c, calls = _routed(monkeypatch, route)
        for i in range(12):
            r = c.get_result(f"/junk/{i}")
            assert r.status == "failed" and r.error["error"] == "invalid_json"
        assert len(calls) == 12 and c.health()["breaker_open"] is False          # twelve in a row never open it

        c, calls = _routed(monkeypatch, route)
        for i in range(4):
            c.get_result(f"/bad/{i}")
        c.get_result("/junk/between")                                            # does not reset the four
        assert c.health()["breaker_open"] is False
        c.get_result("/bad/5th")                                                 # the fifth failure opens it
        assert c.health()["breaker_open"] is True
        n = len(calls)
        c.get_result("/bad/6th")
        assert len(calls) == n

    def test_rejected_credentials_do_not_count_either(self, monkeypatch):
        for status in (401, 403):
            c, calls = _routed(monkeypatch, _always(_Resp(status, text="no")))
            for i in range(8):
                c.get_result(f"/x/{i}")
            assert len(calls) == 8 and c.health()["breaker_open"] is False
            assert c.auth_error["status"] == status          # still reported as an auth failure

    def test_health_reports_the_breaker_and_counts_the_skipped_calls(self, monkeypatch):
        c, calls = _routed(monkeypatch, _always(_Resp(503)))
        for i in range(8):
            c.get_result(f"/e/{i}")
        h = c.health()
        assert h["breaker_open"] is True and h["skipped_calls"] == 3
        assert h["calls"] == 8 and h["failed"] == 8          # a skipped call is a call the board lacks
        assert [f["error"] for f in h["failures"]] == ["HTTP 503"] * 5 + ["breaker_open"] * 3
        assert h["failures"][5]["attempts"] == 0 and h["failures"][0]["attempts"] == uwmod.MAX_ATTEMPTS

    def test_the_breaker_belongs_to_one_client(self, monkeypatch):
        c1, _ = _routed(monkeypatch, _always(_Resp(503)))
        for i in range(5):
            c1.get_result(f"/e/{i}")
        assert c1.health()["breaker_open"] is True
        c2, calls2 = _routed(monkeypatch, _always(_Resp(200, {"data": [1]})))
        assert c2.get_result("/e/0").status == "ok" and len(calls2) == 1

    def test_the_result_methods_see_the_open_breaker_as_a_failure(self, monkeypatch):
        c, calls = _routed(monkeypatch, _always(_Resp(503)))
        for i in range(5):
            c.get_result(f"/e/{i}")
        n = len(calls)
        r = c.etf_ohlc_result("XLK", days=30)
        assert r.status == "failed" and len(calls) == n
        assert c.etf_ohlc("XLK", days=30) == []              # the list method keeps its contract
