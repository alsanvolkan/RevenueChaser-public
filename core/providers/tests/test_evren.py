"""EVREN chat client (#360): request shape, key handling, retry, cache, no off-peak gate (no live network)."""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import deepseek, evren

KEY = "sentinel-evren-key-360"


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)
        self.headers: dict = {}

    def json(self):
        return self._payload


class _Client:
    """Answers each ``post`` from a script (the last step repeats)."""

    def __init__(self, *script):
        self._script = list(script)
        self.posts: list[dict] = []

    def post(self, url, **k):
        self.posts.append({"url": url, "json": k.get("json"), "headers": dict(k.get("headers") or {})})
        return self._script.pop(0) if len(self._script) > 1 else self._script[0]

    def close(self):
        pass


def _ok(content='{"products": ["x"]}'):
    return _Resp(200, {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 3}})


@pytest.fixture(autouse=True)
def _env(monkeypatch, rawstore_tmp_db):
    monkeypatch.setenv("EVREN_API_KEY", KEY)
    monkeypatch.delenv("RC_EVREN_BASE", raising=False)
    monkeypatch.setattr(evren, "_sleep", lambda s: None)


def test_the_request_is_an_openai_style_json_completion():
    c = _Client(_ok())
    assert evren.complete("sys", "user", client=c, use_cache=False) == '{"products": ["x"]}'
    post = c.posts[0]
    assert post["url"] == "https://evren-llmapi.ssyz.org.tr/v1/chat/completions"
    body = post["json"]
    assert body["model"] == evren.MODEL and body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}]
    assert "thinking" not in body                       # a DeepSeek-only knob
    assert post["headers"]["Authorization"] == f"Bearer {KEY}"


def test_the_base_url_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv("RC_EVREN_BASE", "http://127.0.0.1:9/v1/")
    c = _Client(_ok())
    evren.complete("s", "u", client=c, use_cache=False)
    assert c.posts[0]["url"] == "http://127.0.0.1:9/v1/chat/completions"


def test_a_missing_key_makes_no_request_and_names_the_variable(monkeypatch):
    monkeypatch.delenv("EVREN_API_KEY")
    c = _Client(_ok())
    with pytest.raises(evren.EvrenAuthError, match="EVREN_API_KEY"):
        evren.complete("s", "u", client=c, use_cache=False)
    assert c.posts == []


def test_a_rejected_key_names_the_variable_never_the_value():
    c = _Client(_Resp(401, text=f"bad key {KEY}"))
    with pytest.raises(evren.EvrenAuthError) as exc:
        evren.complete("s", "u", client=c, use_cache=False)
    assert "EVREN_API_KEY" in str(exc.value) and KEY not in str(exc.value)


def test_the_engine_catches_evren_failures_as_the_writer_failures():
    # build_profiles stops on DeepSeek's auth / transient classes; EVREN's are those classes.
    assert issubclass(evren.EvrenAuthError, deepseek.DeepSeekAuthError)
    assert issubclass(evren.EvrenTransientError, deepseek.DeepSeekTransientError)
    assert issubclass(evren.EvrenError, deepseek.DeepSeekError)


def test_a_429_is_retried_once():
    c = _Client(_Resp(429, text="slow down"), _ok())
    assert evren.complete("s", "u", client=c, use_cache=False)
    assert len(c.posts) == 2


def test_a_5xx_is_final():
    c = _Client(_Resp(503, text="down"), _ok())
    with pytest.raises(evren.EvrenTransientError):
        evren.complete("s", "u", client=c, use_cache=False)
    assert len(c.posts) == 1


def test_a_reply_is_cached_under_its_own_provider():
    c = _Client(_ok())
    evren.complete("s", "u", client=c, ticker="AAA")
    assert evren.complete("s", "u", client=c, ticker="AAA") == '{"products": ["x"]}'
    assert len(c.posts) == 1
    rows = rawstore.get_all("llm_cache")
    assert [r["model"] for r in rows] == [evren.MODEL] and rows[0]["response"]["provider"] == "evren"
    # The same prompt to DeepSeek is another key: the two providers never share a reply.
    assert evren._cache_key("s", "u", evren.MODEL) != deepseek._cache_key("s", "u", False, evren.MODEL)


def test_there_is_no_off_peak_gate(monkeypatch):
    monkeypatch.setattr(deepseek, "peak_blocked", lambda now=None: (_ for _ in ()).throw(AssertionError("gated")))
    c = _Client(_ok())
    assert evren.complete("s", "u", client=c, use_cache=False)
    assert evren.COST_USD_PER_CALL == 0.0


def test_an_echoed_key_never_reaches_an_error_message():
    """Review nit3 (PR 362): the 'not JSON' and 'no content' messages are scrubbed like every other path."""
    class _Bad(_Resp):
        def json(self):
            raise ValueError("not json")

    for resp in (_Bad(200, text=f"echo {KEY}"), _Resp(200, {"choices": [], "echo": KEY})):
        with pytest.raises(evren.EvrenError) as exc:
            evren.complete("s", "u", client=_Client(resp), use_cache=False)
        assert KEY not in str(exc.value)
