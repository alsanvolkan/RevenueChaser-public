"""DeepSeek client — off-peak gate, cache, key handling (no live network)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from RevenueChaser.core.providers import deepseek as ds


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text
        self.headers: dict = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=httpx.Request("POST", "http://x"),
                                        response=self)


class _Client:
    def __init__(self, resp):
        self._resp = resp
        self.calls = 0
        self.last_body = None

    def post(self, *a, **k):
        self.calls += 1
        self.last_body = k.get("json")
        return self._resp

    def close(self):
        pass


def _ok(content):
    return _Resp(200, {"choices": [{"message": {"content": content}}], "usage": {}})


class _SeqClient:
    """Answers each ``post`` from a script: a response is returned, an exception raised."""

    def __init__(self, *script):
        self._script = list(script)
        self.calls = 0

    def post(self, *a, **k):
        self.calls += 1
        step = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(step, BaseException):
            raise step
        return step

    def close(self):
        pass


class _Knobs:
    """The two retry patch points: recorded sleeps and a settable jitter source."""

    def __init__(self):
        self.sleeps: list[float] = []
        self.rand = 0.5


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    """Never really sleep; make the jitter deterministic. Test-local, not in the root conftest."""
    k = _Knobs()
    monkeypatch.setattr(ds, "_sleep", k.sleeps.append)
    monkeypatch.setattr(ds, "_random", lambda: k.rand)
    return k


@pytest.fixture
def live(monkeypatch):
    """Off-peak gate off and a key present: the request path itself is under test."""
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "0")
    monkeypatch.setattr(ds, "key_for", lambda slug: "k")


@pytest.fixture
def peak(monkeypatch):
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "1")
    monkeypatch.setattr(ds.offpeak, "is_off_peak", lambda now=None: False)
    monkeypatch.setattr(ds.offpeak, "next_off_peak",
                        lambda now=None: datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc))
    return None


def test_peak_raises_without_http(rawstore_tmp_db, peak, monkeypatch):
    monkeypatch.setattr(ds, "key_for", lambda slug: "k")
    client = _Client(_ok("{}"))
    with pytest.raises(ds.PeakHoursError):
        ds.complete("sys", "user", client=client)
    assert client.calls == 0


def test_missing_key_raises(rawstore_tmp_db, monkeypatch):
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "0")
    monkeypatch.setattr(ds, "key_for", lambda slug: "")
    with pytest.raises(ds.DeepSeekError):
        ds.complete("s", "u", client=_Client(_ok("{}")))


def test_missing_key_is_an_auth_error(rawstore_tmp_db, monkeypatch):
    """Callers stop on auth errors, so a missing key must not look transient."""
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "0")
    monkeypatch.setattr(ds, "key_for", lambda slug: "")
    client = _Client(_ok("{}"))
    with pytest.raises(ds.DeepSeekAuthError):
        ds.complete("s", "u", client=client)
    assert client.calls == 0


@pytest.mark.parametrize("status", [429, 503])
def test_server_errors_surface_as_transient(rawstore_tmp_db, monkeypatch, status):
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "0")
    monkeypatch.setattr(ds, "key_for", lambda slug: "k")
    with pytest.raises(ds.DeepSeekTransientError) as exc:
        ds.complete("s", "u", client=_Client(_Resp(status, text="busy")))
    assert isinstance(exc.value, ds.DeepSeekError)         # one exception family
    assert not isinstance(exc.value, ds.DeepSeekAuthError)


def test_timeout_surfaces_as_transient(rawstore_tmp_db, monkeypatch):
    import httpx
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "0")
    monkeypatch.setattr(ds, "key_for", lambda slug: "k")

    class _Slow(_Client):
        def post(self, *a, **k):
            raise httpx.ReadTimeout("timed out")

    with pytest.raises(ds.DeepSeekTransientError):
        ds.complete("s", "u", client=_Slow(None))


def test_success_is_cached(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core import rawstore
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "0")
    monkeypatch.setattr(ds, "key_for", lambda slug: "k")
    client = _Client(_ok('{"a": 1}'))
    out1 = ds.complete("s", "u", client=client)
    assert out1 == '{"a": 1}' and client.calls == 1
    assert rawstore.count("llm_cache") == 1  # durable cache row
    # second call is a cache hit: no HTTP
    out2 = ds.complete("s", "u", client=client)
    assert out2 == '{"a": 1}' and client.calls == 1


def test_cached_result_is_served_even_at_peak(rawstore_tmp_db, monkeypatch):
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "0")
    monkeypatch.setattr(ds, "key_for", lambda slug: "k")
    client = _Client(_ok('{"a": 2}'))
    ds.complete("s", "u", client=client)  # populate cache
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "1")
    monkeypatch.setattr(ds.offpeak, "is_off_peak", lambda now=None: False)
    # cache hit short-circuits before the gate
    assert ds.complete("s", "u", client=client) == '{"a": 2}'
    assert client.calls == 1


def test_http_error_raises(rawstore_tmp_db, monkeypatch):
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "0")
    monkeypatch.setattr(ds, "key_for", lambda slug: "k")
    with pytest.raises(ds.DeepSeekError):
        ds.complete("s", "u", client=_Client(_Resp(400, text="bad request")))


def test_auth_error_is_specific(rawstore_tmp_db, monkeypatch):
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "0")
    monkeypatch.setattr(ds, "key_for", lambda slug: "bad")
    with pytest.raises(ds.DeepSeekAuthError):
        ds.complete("s", "u", client=_Client(_Resp(401, text="auth fails")))


def test_thinking_body_has_effort_and_no_temperature(rawstore_tmp_db, monkeypatch):
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "0")
    monkeypatch.setattr(ds, "key_for", lambda slug: "k")
    c = _Client(_ok('{"a": 1}'))
    ds.complete("s", "u", thinking=True, reasoning_effort="low", max_tokens=8000, client=c)
    assert c.last_body["thinking"] == {"type": "enabled"}
    assert c.last_body["reasoning_effort"] == "low"
    assert "temperature" not in c.last_body


def test_non_thinking_body_has_temperature(rawstore_tmp_db, monkeypatch):
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "0")
    monkeypatch.setattr(ds, "key_for", lambda slug: "k")
    c = _Client(_ok('{"a": 1}'))
    ds.complete("s", "u", thinking=False, client=c)
    assert c.last_body["thinking"] == {"type": "disabled"}
    assert "temperature" in c.last_body and "reasoning_effort" not in c.last_body


# --- peak override written by the Hermes usage card -----------------------------

_PEAK_NOW = datetime(2026, 9, 23, 7, 30, tzinfo=timezone.utc)  # Wednesday, inside 06-10 UTC


def _write_gate(path, allow, until):
    path.write_text(json.dumps({"allowPeak": allow, "until": until}), encoding="utf-8")


def test_gate_file_honours_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_GATE_FILE", str(tmp_path / "g.json"))
    assert ds.gate_file() == tmp_path / "g.json"


def test_gate_file_default_is_in_hermes_home(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_GATE_FILE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert ds.gate_file() == tmp_path / "deepseek_gate.json"


def test_override_missing_file_is_blocked():
    assert ds.peak_override_active(_PEAK_NOW) is False


def test_override_active_until_the_window_ends(tmp_path, monkeypatch):
    gate = tmp_path / "g.json"
    monkeypatch.setenv("DEEPSEEK_GATE_FILE", str(gate))
    end = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc).timestamp()
    _write_gate(gate, True, end)
    assert ds.peak_override_active(_PEAK_NOW) is True
    assert ds.peak_override_active(datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)) is False


@pytest.mark.parametrize("body", ['{"allowPeak": false, "until": 9999999999}', "{bad json", "[]",
                                  '{"allowPeak": true, "until": "soon"}'])
def test_override_fails_closed(tmp_path, monkeypatch, body):
    gate = tmp_path / "g.json"
    monkeypatch.setenv("DEEPSEEK_GATE_FILE", str(gate))
    gate.write_text(body, encoding="utf-8")
    assert ds.peak_override_active(_PEAK_NOW) is False


def test_override_lets_a_peak_call_through(rawstore_tmp_db, peak, monkeypatch, tmp_path):
    gate = tmp_path / "g.json"
    monkeypatch.setenv("DEEPSEEK_GATE_FILE", str(gate))
    _write_gate(gate, True, _PEAK_NOW.timestamp() + 3600)
    monkeypatch.setattr(ds, "key_for", lambda slug: "k")
    client = _Client(_ok('{"ok": 1}'))
    assert ds.complete("sys", "user", client=client, now=_PEAK_NOW) == '{"ok": 1}'
    assert client.calls == 1


def test_expired_override_still_blocks(rawstore_tmp_db, peak, monkeypatch, tmp_path):
    gate = tmp_path / "g.json"
    monkeypatch.setenv("DEEPSEEK_GATE_FILE", str(gate))
    _write_gate(gate, True, _PEAK_NOW.timestamp() - 1)
    monkeypatch.setattr(ds, "key_for", lambda slug: "k")
    client = _Client(_ok("{}"))
    with pytest.raises(ds.PeakHoursError):
        ds.complete("sys", "user", client=client, now=_PEAK_NOW)
    assert client.calls == 0


# --- D2 retry (#167 / P4 + P11): one retry, only where the request never ran -----

def test_429_then_200_retries_once(rawstore_tmp_db, live, knobs):
    client = _SeqClient(_Resp(429), _ok('{"a": 1}'))
    assert ds.complete("s", "u", client=client) == '{"a": 1}'
    assert client.calls == 2
    assert len(knobs.sleeps) == 1


def test_429_twice_ends_transient_after_two_calls(rawstore_tmp_db, live):
    client = _SeqClient(_Resp(429, text="slow down"))
    with pytest.raises(ds.DeepSeekTransientError):
        ds.complete("s", "u", client=client)
    assert client.calls == 2


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_final_after_one_call(rawstore_tmp_db, live, knobs, status):
    """The server may have run (and billed) the request: no retry."""
    client = _SeqClient(_Resp(status, text="boom"))
    with pytest.raises(ds.DeepSeekTransientError):
        ds.complete("s", "u", client=client)
    assert client.calls == 1
    assert knobs.sleeps == []


@pytest.mark.parametrize("exc", [
    pytest.param("ConnectError", id="connect-error"),
    pytest.param("ConnectTimeout", id="connect-timeout"),
    pytest.param("PoolTimeout", id="pool-timeout"),
])
def test_pre_send_errors_are_retried(rawstore_tmp_db, live, exc):
    """The request never left the machine, so it is safe to send again."""
    import httpx
    client = _SeqClient(getattr(httpx, exc)("no route"), _ok('{"a": 1}'))
    assert ds.complete("s", "u", client=client) == '{"a": 1}'
    assert client.calls == 2


def test_connect_error_twice_is_transient(rawstore_tmp_db, live):
    import httpx
    client = _SeqClient(httpx.ConnectError("no route"))
    with pytest.raises(ds.DeepSeekTransientError):
        ds.complete("s", "u", client=client)
    assert client.calls == 2


@pytest.mark.parametrize("exc", ["ReadTimeout", "WriteTimeout", "RemoteProtocolError", "ReadError"])
def test_errors_after_the_send_are_final(rawstore_tmp_db, live, knobs, exc):
    """The POST may already be billed: a read-side failure is never replayed."""
    import httpx
    client = _SeqClient(getattr(httpx, exc)("cut off"), _ok('{"a": 1}'))
    with pytest.raises(ds.DeepSeekTransientError):
        ds.complete("s", "u", client=client)
    assert client.calls == 1
    assert knobs.sleeps == []


def test_retry_after_is_capped_after_the_jitter(rawstore_tmp_db, live, knobs):
    """``Retry-After: 999`` with the largest jitter must still wait at most 30 s."""
    knobs.rand = 1.0
    limited = _Resp(429)
    limited.headers = {"Retry-After": "999"}
    client = _SeqClient(limited, _ok('{"a": 1}'))
    ds.complete("s", "u", client=client)
    assert knobs.sleeps == [30.0]


@pytest.mark.parametrize("rand, expected", [(0.0, 10.0), (0.5, 11.25), (1.0, 12.5)])
def test_retry_after_gets_up_to_25_percent_jitter(rawstore_tmp_db, live, knobs, rand, expected):
    knobs.rand = rand
    limited = _Resp(429)
    limited.headers = {"Retry-After": "10"}
    ds.complete("s", "u", client=_SeqClient(limited, _ok('{"a": 1}')))
    assert knobs.sleeps == [pytest.approx(expected)]


@pytest.mark.parametrize("rand, expected", [(0.0, 0.0), (0.5, 1.0), (1.0, 2.0)])
def test_backoff_without_retry_after_is_full_jitter(rawstore_tmp_db, live, knobs, rand, expected):
    """No header: ``uniform(0, base)`` for the first retry, never above the 30 s cap."""
    knobs.rand = rand
    ds.complete("s", "u", client=_SeqClient(_Resp(429), _ok('{"a": 1}')))
    assert knobs.sleeps == [pytest.approx(expected)]


@pytest.mark.parametrize("header", ["Wed, 21 Oct 2026 07:28:00 GMT", "soon", "nan", "-5"])
def test_unusable_retry_after_falls_back_to_backoff(rawstore_tmp_db, live, knobs, header):
    knobs.rand = 1.0
    limited = _Resp(429)
    limited.headers = {"Retry-After": header}
    ds.complete("s", "u", client=_SeqClient(limited, _ok('{"a": 1}')))
    assert len(knobs.sleeps) == 1 and 0.0 <= knobs.sleeps[0] <= 30.0


def test_retry_delay_never_exceeds_the_cap():
    class _R:
        headers = {"Retry-After": "1e9"}
    assert ds._retry_delay(_R(), 1) <= 30.0
    assert ds._retry_delay(None, 12) <= 30.0   # 2 * 2**11 would be 4096 s


# --- a reply we cannot parse is billed, final, and never cached ------------------

def test_non_json_200_is_a_deepseek_error_and_not_cached(rawstore_tmp_db, live, knobs):
    from RevenueChaser.core import rawstore

    class _Html(_Resp):
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    client = _SeqClient(_Html(200, text="<html>bad gateway page</html>"))
    with pytest.raises(ds.DeepSeekError) as exc:
        ds.complete("s", "u", client=client)
    assert not isinstance(exc.value, (ds.DeepSeekTransientError, ds.DeepSeekAuthError))
    assert client.calls == 1 and knobs.sleeps == []
    assert rawstore.count("llm_cache") == 0


def test_a_json_reply_that_is_not_an_object_is_a_deepseek_error(rawstore_tmp_db, live):
    with pytest.raises(ds.DeepSeekError):
        ds.complete("s", "u", client=_SeqClient(_Resp(200, payload=["not", "an", "object"])))


# --- validate= hook: an answer the caller rejects is not replayed from the cache --

def test_validate_false_is_returned_but_not_cached(rawstore_tmp_db, live):
    from RevenueChaser.core import rawstore
    client = _SeqClient(_ok("{}"))
    assert ds.complete("s", "u", client=client, validate=lambda c: False) == "{}"
    assert rawstore.count("llm_cache") == 0
    ds.complete("s", "u", client=client, validate=lambda c: False)
    assert client.calls == 2          # asked again, not replayed


def test_validate_true_is_cached(rawstore_tmp_db, live):
    from RevenueChaser.core import rawstore
    client = _SeqClient(_ok('{"a": 1}'))
    ds.complete("s", "u", client=client, validate=lambda c: True)
    assert rawstore.count("llm_cache") == 1
    assert ds.complete("s", "u", client=client) == '{"a": 1}'
    assert client.calls == 1


def test_validate_sees_the_content(rawstore_tmp_db, live):
    seen = []
    ds.complete("s", "u", client=_SeqClient(_ok('{"a": 1}')),
                validate=lambda c: seen.append(c) or True)
    assert seen == ['{"a": 1}']


def test_a_raising_validate_counts_as_invalid(rawstore_tmp_db, live):
    from RevenueChaser.core import rawstore

    def boom(content):
        raise RuntimeError("caller bug")

    assert ds.complete("s", "u", client=_SeqClient(_ok('{"a": 1}')), validate=boom) == '{"a": 1}'
    assert rawstore.count("llm_cache") == 0


# --- P13: a naive ``now`` is UTC, like core.offpeak reads it ---------------------

@pytest.fixture
def east_of_utc():
    """Run with the local zone 12 h east of UTC, so a naive datetime read as local differs."""
    import os
    import time
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is not available on this platform")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "XYZ-12"        # POSIX syntax: UTC+12, no DST, needs no tz database
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_naive_now_is_read_as_utc_not_local(tmp_path, monkeypatch, east_of_utc):
    gate = tmp_path / "g.json"
    monkeypatch.setenv("DEEPSEEK_GATE_FILE", str(gate))
    until = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc).timestamp()
    _write_gate(gate, True, until)
    # 10:30 UTC is past the window end. Read as local (UTC+12) it would be 22:30 UTC the day before.
    assert ds.peak_override_active(datetime(2026, 9, 23, 10, 30)) is False
    assert ds.peak_override_active(datetime(2026, 9, 23, 9, 30)) is True


# --- the base URL is read when the call is made ----------------------------------

def test_base_url_follows_the_environment_at_call_time(rawstore_tmp_db, live, monkeypatch):
    seen = []

    class _Url(_SeqClient):
        def post(self, url, *a, **k):
            seen.append(url)
            return super().post(url, *a, **k)

    monkeypatch.setenv("RC_DEEPSEEK_BASE", "http://127.0.0.1:9/")
    ds.complete("s", "u", client=_Url(_ok('{"a": 1}')))
    assert seen == ["http://127.0.0.1:9/chat/completions"]
    monkeypatch.delenv("RC_DEEPSEEK_BASE")
    assert ds.base_url() == "https://api.deepseek.com"


def test_the_misnamed_sec_base_is_gone():
    assert not hasattr(ds, "SEC_BASE")
    assert "SEC_BASE" not in ds.__all__
