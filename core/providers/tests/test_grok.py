"""SuperGrok chat client (#388 Grok writer) — body mapping, endpoint, auth, allowance gate, cache (no live network).

Every request goes to a fake client; the SuperGrok login is a temp ``auth.json`` (``RC_XAI_AUTH_FILE``) and the
allowance reading is a patched ``subscription_usage.fetch_grok``. Nothing reaches xAI.
"""
from __future__ import annotations

import json
import logging
import urllib.error
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from RevenueChaser.core import offpeak, rawstore, subscription_usage
from RevenueChaser.core.providers import deepseek
from RevenueChaser.core.providers import grok

TOKEN = "sentinel-grok-token-7f3a"
NOW = datetime(2026, 10, 16, 7, 0, tzinfo=timezone.utc)          # a Friday 07:00 UTC: DeepSeek peak


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.headers: dict = {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _ok(content="{\"products\": [\"gpu\"]}", usage=None):
    return _Resp(200, {"choices": [{"message": {"content": content}}], "usage": usage or {"total_tokens": 9}})


class _Client:
    """Records every post; answers from a script (a response is returned, an exception raised)."""

    def __init__(self, *script):
        self._script = list(script) or [_ok()]
        self.posts: list[dict] = []

    def post(self, url, json=None, headers=None, **k):
        self.posts.append({"url": url, "json": json, "headers": dict(headers or {})})
        step = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(step, BaseException):
            raise step
        return step

    def close(self):
        pass


def _window(percent, key="seven_day"):
    return subscription_usage.Window(key=key, percent=percent, resets_at=NOW + timedelta(days=2))


@pytest.fixture(autouse=True)
def login(tmp_path, monkeypatch):
    """A valid Hermes xAI login in a temp file, an allowance with room, no sleeps, a fresh gate."""
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"providers": {"xai-oauth": {"tokens": {"access_token": TOKEN}}}}), encoding="utf-8")
    monkeypatch.setenv("RC_XAI_AUTH_FILE", str(path))
    monkeypatch.delenv("XAI_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("RC_GROK_BASE", raising=False)
    monkeypatch.delenv("RC_GROK_CAP", raising=False)
    reads = []
    monkeypatch.setattr(subscription_usage, "fetch_grok", lambda now=None: reads.append(now) or [_window(10)])
    monkeypatch.setattr(grok, "_sleep", lambda s: None)
    grok.reset_gate()
    return reads


def _call(client, **kw):
    kw.setdefault("now", NOW)
    return grok.complete("sys", "user text", client=client, **kw)


# ── the request ─────────────────────────────────────────────────────────────

def test_deepseek_knobs_are_mapped_explicitly_never_sent_blindly():
    """thinking / reasoning_effort / temperature are DeepSeek parameters: dropped; json_mode -> response_format."""
    c = _Client()
    _call(c, thinking=True, reasoning_effort="high", temperature=0.3, max_tokens=8000, json_mode=True,
          use_cache=False)
    assert c.posts[0]["json"] == {
        "model": grok.MODEL,
        "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "user text"}],
        "max_tokens": 8000, "stream": False, "response_format": {"type": "json_object"}}


def test_without_json_mode_no_response_format_is_sent():
    c = _Client()
    _call(c, thinking=False, json_mode=False, use_cache=False)
    body = c.posts[0]["json"]
    assert "response_format" not in body and "temperature" not in body and "thinking" not in body


def test_the_call_goes_to_the_cli_proxy_chat_endpoint_with_the_bearer():
    c = _Client()
    _call(c, use_cache=False)
    assert c.posts[0]["url"] == "https://cli-chat-proxy.grok.com/v1/chat/completions"
    assert c.posts[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("override,expected", [
    ("https://other.grok.com/v2/", "https://other.grok.com/v2/chat/completions"),
    ("http://127.0.0.1:8765/v1", "http://127.0.0.1:8765/v1/chat/completions"),
])
def test_a_base_override_on_a_grok_host_or_loopback_is_used(monkeypatch, override, expected):
    monkeypatch.setenv("RC_GROK_BASE", override)
    c = _Client()
    _call(c, use_cache=False)
    assert c.posts[0]["url"] == expected


@pytest.mark.parametrize("override", [
    "https://api.x.ai/v1",                      # the metered developer API: never with the subscription token
    "https://example.com/v1",
    "http://cli-chat-proxy.grok.com/v1",        # plain http off loopback
    "https://evilgrok.com/v1",
    "https://cli-chat-proxy.grok.com.evil.net/v1",
])
def test_a_base_override_elsewhere_is_ignored_with_a_warning(monkeypatch, caplog, override):
    monkeypatch.setenv("RC_GROK_BASE", override)
    c = _Client()
    with caplog.at_level(logging.WARNING, logger=grok.logger.name):
        _call(c, use_cache=False)
    assert c.posts[0]["url"] == "https://cli-chat-proxy.grok.com/v1/chat/completions"
    assert any("RC_GROK_BASE" in r.getMessage() for r in caplog.records)
    assert not any(override in r.getMessage() for r in caplog.records)


# ── auth: fail closed, never leak the token ─────────────────────────────────

def test_a_missing_login_is_an_auth_error_before_any_request(tmp_path, monkeypatch, login):
    monkeypatch.setenv("RC_XAI_AUTH_FILE", str(tmp_path / "nope.json"))
    c = _Client()
    with pytest.raises(grok.GrokAuthError):
        _call(c, use_cache=False)
    assert c.posts == [] and login == []


def test_an_expired_login_is_an_auth_error_before_any_request(tmp_path, monkeypatch, login):
    path = tmp_path / "expired.json"
    path.write_text(json.dumps({"providers": {"xai-oauth": {"tokens": {
        "access_token": TOKEN, "expires_at": (NOW - timedelta(minutes=5)).timestamp()}}}}), encoding="utf-8")
    monkeypatch.setenv("RC_XAI_AUTH_FILE", str(path))
    c = _Client()
    with pytest.raises(grok.GrokAuthError) as err:
        _call(c, use_cache=False)
    assert c.posts == [] and login == []
    assert TOKEN not in str(err.value)


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_is_an_auth_error_that_never_carries_the_token(caplog, status):
    c = _Client(_Resp(status, text=f"invalid bearer {TOKEN}"))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(grok.GrokAuthError) as err:
            _call(c, use_cache=False)
    assert TOKEN not in str(err.value) and TOKEN not in repr(err.value.args)
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("status", [400, 404, 422, 426])
def test_another_4xx_is_a_request_error_with_the_body_scrubbed(status):
    c = _Client(_Resp(status, text=f"model not found; you sent {TOKEN}"))
    with pytest.raises(grok.GrokRequestError) as err:
        _call(c, use_cache=False)
    assert "model not found" in str(err.value) and TOKEN not in str(err.value)
    assert len(c.posts) == 1


def test_a_network_error_never_carries_the_token():
    c = _Client(httpx.ReadTimeout(f"read timed out {TOKEN}"))
    with pytest.raises(grok.GrokTransientError) as err:
        _call(c, use_cache=False)
    assert TOKEN not in str(err.value)


# ── retries and typed errors ────────────────────────────────────────────────

def test_a_429_is_retried_once_then_transient():
    c = _Client(_Resp(429, text="slow down"))
    with pytest.raises(grok.GrokTransientError):
        _call(c, use_cache=False)
    assert len(c.posts) == grok.MAX_ATTEMPTS == 2


def test_a_5xx_is_transient_and_not_retried():
    c = _Client(_Resp(502, text="bad gateway"))
    with pytest.raises(grok.GrokTransientError):
        _call(c, use_cache=False)
    assert len(c.posts) == 1


def test_a_connect_error_is_retried_then_answered():
    c = _Client(httpx.ConnectError("refused"), _ok())
    assert json.loads(_call(c, use_cache=False)) == {"products": ["gpu"]}
    assert len(c.posts) == 2


def test_a_402_is_the_allowance_not_an_auth_failure():
    c = _Client(_Resp(402, text="subscription quota exhausted"))
    with pytest.raises(grok.GrokAllowanceError):
        _call(c, use_cache=False)


def test_a_reply_without_content_is_a_generic_grok_error():
    c = _Client(_Resp(200, {"choices": [{"message": {"content": ""}}]}))
    with pytest.raises(grok.GrokError) as err:
        _call(c, use_cache=False)
    assert type(err.value) is grok.GrokError


def test_content_given_as_parts_is_joined():
    c = _Client(_Resp(200, {"choices": [{"message": {"content": [
        {"type": "text", "text": "{\"products\": "}, {"type": "text", "text": "[\"gpu\"]}"}]}}]}))
    assert json.loads(_call(c, use_cache=False)) == {"products": ["gpu"]}


# ── the allowance gate (SuperGrok bills by allowance, not by time of day) ───

def test_an_exhausted_allowance_stops_before_any_request(monkeypatch):
    monkeypatch.setattr(subscription_usage, "fetch_grok", lambda now=None: [_window(95)])
    c = _Client()
    with pytest.raises(grok.GrokAllowanceError) as err:
        _call(c, use_cache=False)
    assert c.posts == []
    assert err.value.next_reset == NOW + timedelta(days=2)


def test_the_weekly_cap_defaults_to_90(monkeypatch):
    monkeypatch.delenv("RC_GROK_CAP", raising=False)
    monkeypatch.setattr(subscription_usage, "fetch_grok", lambda now=None: [_window(91)])
    c = _Client()
    with pytest.raises(grok.GrokAllowanceError):
        _call(c, use_cache=False)
    assert c.posts == []


def test_rc_grok_cap_raises_the_weekly_cap(monkeypatch):
    monkeypatch.setenv("RC_GROK_CAP", "95")
    monkeypatch.setattr(subscription_usage, "fetch_grok", lambda now=None: [_window(91)])
    c = _Client()
    _call(c, use_cache=False)
    assert len(c.posts) == 1


def test_rc_grok_cap_can_also_lower_the_weekly_cap(monkeypatch):
    monkeypatch.setenv("RC_GROK_CAP", "50")
    monkeypatch.setattr(subscription_usage, "fetch_grok", lambda now=None: [_window(60)])
    c = _Client()
    with pytest.raises(grok.GrokAllowanceError):
        _call(c, use_cache=False)
    assert c.posts == []


def test_a_stop_at_a_custom_cap_names_that_cap_and_the_reset(monkeypatch):
    """describe() and next_available() must get the custom cap too, not only is_within_budget()."""
    monkeypatch.setenv("RC_GROK_CAP", "95")
    monkeypatch.setattr(subscription_usage, "fetch_grok", lambda now=None: [_window(96)])
    with pytest.raises(grok.GrokAllowanceError) as err:
        _call(_Client(), use_cache=False)
    assert "cap 95%" in str(err.value) and err.value.next_reset == NOW + timedelta(days=2)


def test_the_reset_to_wait_for_is_that_of_the_window_over_the_custom_cap(monkeypatch):
    """A 92% window is under a 95% cap and must not be offered as the reset; the 97% one is."""
    monkeypatch.setenv("RC_GROK_CAP", "95")
    soon = subscription_usage.Window(key="period", percent=92, resets_at=NOW + timedelta(hours=3))
    late = subscription_usage.Window(key="weekly", percent=97, resets_at=NOW + timedelta(days=2))
    monkeypatch.setattr(subscription_usage, "fetch_grok", lambda now=None: [soon, late])
    with pytest.raises(grok.GrokAllowanceError) as err:
        _call(_Client(), use_cache=False)
    assert err.value.next_reset == NOW + timedelta(days=2)


def test_a_window_below_a_custom_cap_has_no_reset_to_wait_for():
    """next_available() with the default cap would report a reset for a 92% window at cap 95; with the cap, none."""
    w = [_window(92)]
    assert subscription_usage.next_available(w, now=NOW) == NOW + timedelta(days=2)
    assert subscription_usage.next_available(w, max_week=95.0, now=NOW) is None


@pytest.mark.parametrize("bad", ["abc", "", "96", "100", "101", "250", "0", "-5", "nan", "inf"])
def test_an_invalid_rc_grok_cap_falls_back_to_90(monkeypatch, bad):
    monkeypatch.setenv("RC_GROK_CAP", bad)
    monkeypatch.setattr(subscription_usage, "fetch_grok", lambda now=None: [_window(91)])
    c = _Client()
    with pytest.raises(grok.GrokAllowanceError):
        _call(c, use_cache=False)
    assert c.posts == []


def test_the_allowance_is_read_before_each_batch(monkeypatch, login):
    monkeypatch.setattr(grok, "GATE_EVERY", 3)
    c = _Client()
    for i in range(7):
        _call(c, use_cache=False)
    assert len(c.posts) == 7 and len(login) == 3                      # before calls 1, 4 and 7


def test_an_unreadable_allowance_stops_rather_than_spend_blind(monkeypatch):
    def broken(now=None):
        raise subscription_usage.SubscriptionUsageError("SuperGrok usage request failed (HTTP 500).")

    monkeypatch.setattr(subscription_usage, "fetch_grok", broken)
    c = _Client()
    with pytest.raises(grok.GrokAllowanceError):
        _call(c, use_cache=False)
    assert c.posts == []


@pytest.mark.parametrize("exc", [ConnectionResetError("reset by peer"), KeyError("windows"), TypeError("bad")])
def test_an_allowance_read_that_fails_any_other_way_also_stops(monkeypatch, exc):
    """A reading that fails outside SubscriptionUsageError (a dropped connection, an odd billing shape) is still an
    allowance stop: never a per-group failure that reads the billing endpoint again for every group."""
    def broken(now=None):
        raise exc

    monkeypatch.setattr(subscription_usage, "fetch_grok", broken)
    c = _Client()
    with pytest.raises(grok.GrokAllowanceError):
        _call(c, use_cache=False)
    assert c.posts == []


def test_a_rejected_billing_read_is_an_auth_error(monkeypatch):
    def rejected(now=None):
        try:
            raise urllib.error.HTTPError("https://cli-chat-proxy.grok.com/v1/billing", 401, "no", {}, None)
        except urllib.error.HTTPError as exc:
            raise subscription_usage.SubscriptionUsageError("SuperGrok subscription API key was rejected") from exc

    monkeypatch.setattr(subscription_usage, "fetch_grok", rejected)
    with pytest.raises(grok.GrokAuthError):
        _call(_Client(), use_cache=False)


def test_there_is_no_off_peak_gate(monkeypatch):
    """At DeepSeek peak, with the off-peak gate on and no override, a Grok call still goes out."""
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "1")
    assert not offpeak.is_off_peak(NOW) and deepseek.peak_blocked(NOW)
    c = _Client()
    _call(c, use_cache=False)
    assert len(c.posts) == 1


def test_calls_are_counted_and_cost_nothing():
    before = grok.calls()
    c = _Client(_Resp(429), _ok())
    _call(c, use_cache=False)
    assert grok.calls() - before == 2                                  # every request sent, the retry included
    assert grok.COST_USD_PER_CALL == 0.0


# ── the durable cache ───────────────────────────────────────────────────────

def test_a_cache_hit_needs_no_login_no_allowance_and_no_request(rawstore_tmp_db, tmp_path, monkeypatch, login):
    _call(_Client(_ok("{\"products\": [\"cached\"]}")))
    monkeypatch.setenv("RC_XAI_AUTH_FILE", str(tmp_path / "gone.json"))
    reads = len(login)
    c = _Client()
    assert json.loads(_call(c)) == {"products": ["cached"]}
    assert c.posts == [] and len(login) == reads


def test_the_reply_is_cached_under_the_grok_model(rawstore_tmp_db):
    _call(_Client(), ticker="aaa", prompt_version="v2")
    rows = rawstore.get_all("llm_cache")
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == grok.MODEL and row["ticker"] == "AAA" and row["prompt_version"] == "v2"
    assert row["response"]["provider"] == "grok" and row["response"]["content"]


def test_a_grok_reply_never_replays_as_deepseek_or_back(rawstore_tmp_db, monkeypatch):
    """Same model id, same prompt: the provider alone keeps the two caches apart."""
    monkeypatch.setattr(grok, "MODEL", deepseek.MODEL)
    deepseek._cache_put(deepseek._cache_key("sys", "user text", True, deepseek.MODEL, "high"),
                        "{\"products\": [\"from deepseek\"]}", {"thinking": True, "effort": "high"})
    deepseek._cache_put(deepseek._cache_key("sys", "user text", False, deepseek.MODEL, None),
                        "{\"products\": [\"from deepseek\"]}", {})
    c = _Client(_ok("{\"products\": [\"from grok\"]}"))
    assert "from grok" in _call(c, thinking=True, reasoning_effort="high")
    assert len(c.posts) == 1                                            # a DeepSeek hit was not replayed
    c = _Client(_ok("{\"products\": [\"other\"]}"))
    assert "from grok" in _call(c, thinking=False)                      # Grok's own reply (the knobs are not sent)
    assert c.posts == []
    # and DeepSeek does not replay the Grok reply: its cache key is its own
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "0")
    rawstore.execute("DELETE FROM llm_cache WHERE CAST(response AS VARCHAR) LIKE '%from deepseek%'")
    ds_client = _Client(_ok("{\"products\": [\"fresh deepseek\"]}"))
    assert "fresh deepseek" in deepseek.complete("sys", "user text", thinking=True, reasoning_effort="high",
                                                 client=ds_client)


def test_a_reply_the_caller_rejects_is_not_cached(rawstore_tmp_db):
    _call(_Client(_ok("not json")), validate=lambda c: False)
    assert rawstore.get_all("llm_cache") == []
