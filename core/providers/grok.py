"""SuperGrok chat client for the Relatedness profile writer (#388): cache, allowance gate, HTTP.

This client is for a SuperGrok subscription rather than a paid xAI API key: it spends the **subscription allowance** and
nothing else (cost 0 USD a call; ``calls()`` is the accounting). It mirrors ``deepseek.complete`` so the profile path
and the theme naming can take either client, with three deliberate differences:

- **No off-peak gate.** SuperGrok bills by allowance, not by time of day. Its gate is the allowance
  (``core/subscription_usage``): the SuperGrok reading is taken before the first request of a batch and again after
  every ``GATE_EVERY`` requests; a window at its ceiling (or no reading at all) raises ``GrokAllowanceError`` before
  any request, so a run stops cleanly and resumes after the reset. Access order: **cache -> login -> allowance ->
  HTTP**; a cached reply costs nothing and needs neither.
- **Auth** is Hermes's xAI OAuth login, read at call time through ``subscription_usage.xai_token`` (the same reader
  the allowance gate uses), so a token Hermes refreshed mid-run is picked up. A missing or expired login is a
  ``GrokAuthError`` before any request. The token is sent only as the bearer header; it never reaches a log line or
  an exception message (every quoted reply text is scrubbed).
- **Parameters are mapped, never passed through.** DeepSeek's ``thinking`` / ``reasoning_effort`` and the
  non-thinking ``temperature`` are not sent (Grok's reasoning models reason by themselves and reject unknown knobs);
  ``json_mode`` becomes ``response_format: {"type": "json_object"}``; ``max_tokens`` is sent as given.

Endpoint: ``RC_GROK_BASE`` (read at call time) + ``/chat/completions``, OpenAI-compatible. The default is the Grok CLI
chat proxy, the host whose ``/v1/billing`` the allowance gate already reads with this login; ``api.x.ai`` is the
metered developer API and is refused, like any host that is not ``grok.com`` / ``*.grok.com`` over https or a
loopback mock: the subscription bearer never leaves xAI's subscription surface. Model: ``RC_GROK_MODEL``. Both are
unconfirmed until one live call: ``python -m RevenueChaser.module_relatedness --grok-ping``.

Errors: ``GrokAuthError`` (401/403, missing or expired login), ``GrokAllowanceError`` (the gate, HTTP 402),
``GrokTransientError`` (429 after one retry, 5xx, network), ``GrokRequestError`` (any other 4xx: the endpoint, the
model or a parameter is wrong, so every call would fail the same way), and ``GrokError`` for a 2xx reply with no
usable content. Retries follow DeepSeek's D2 rule: one retry, only after a 429 or an error raised before the
request left the machine.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import threading
import time
import urllib.error
import urllib.parse
from typing import Callable

import httpx

from .. import rawstore, subscription_usage

logger = logging.getLogger(__name__)

PROVIDER = "grok"
DEFAULT_BASE = "https://cli-chat-proxy.grok.com/v1"
MODEL = os.getenv("RC_GROK_MODEL") or "grok-4.6"
COST_USD_PER_CALL = 0.0      # a subscription allowance, not a per-token bill
MAX_ATTEMPTS = 2             # one call and at most one retry (429 / pre-send failure only)
GATE_EVERY = 25              # requests between two allowance readings ("before each batch")
USER_AGENT = "RevenueChaser/1.0 (relatedness profile writer)"
_RETRY_BASE = 2.0
_MAX_DELAY = 30.0
_PRE_SEND_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SUBSCRIPTION_DOMAIN = "grok.com"

# Patch points for tests.
_sleep = time.sleep
_random = random.random


def _new_client() -> httpx.Client:
    """The HTTP client of a call that was not handed one (tests patch this)."""
    return httpx.Client(timeout=180)


class GrokError(RuntimeError):
    """A Grok reply that cannot be used (2xx without content, not JSON)."""


class GrokAuthError(GrokError):
    """The SuperGrok login is missing, expired or was rejected. Stop; do not retry."""


class GrokTransientError(GrokError):
    """429 after a retry, 5xx, timeout or connection failure: the service, not the request."""


class GrokRequestError(GrokError):
    """Another 4xx: the endpoint, the model or a parameter is wrong. Every call would fail alike: stop the run."""


class GrokAllowanceError(GrokError):
    """The SuperGrok allowance is spent (or could not be read): stop cleanly and resume after ``next_reset``."""

    def __init__(self, message: str, next_reset=None):
        self.next_reset = next_reset
        super().__init__(message)


_state_lock = threading.Lock()
_calls = 0
_gate = {"since": None}      # requests admitted since the last allowance reading; None = read before the next one


def calls() -> int:
    """HTTP requests sent by this process (every attempt, the retry included). Each costs 0 USD."""
    return _calls


def reset_gate() -> None:
    """Make the next request read the allowance first (a run calls this as it starts)."""
    with _state_lock:
        _gate["since"] = None


def _allowed(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme in ("http", "https") and host in _LOOPBACK_HOSTS:
        return True
    return parts.scheme == "https" and (host == _SUBSCRIPTION_DOMAIN or host.endswith("." + _SUBSCRIPTION_DOMAIN))


def base_url() -> str:
    """``RC_GROK_BASE`` at call time (no trailing slash) when it is https on grok.com / *.grok.com or a loopback
    mock; anything else is ignored with a warning naming the variable (never its value)."""
    override = (os.getenv("RC_GROK_BASE") or "").strip().rstrip("/")
    if not override:
        return DEFAULT_BASE
    if _allowed(override):
        return override
    logger.warning("RC_GROK_BASE ignored: the SuperGrok token goes only to https on %s / *.%s or a loopback mock "
                   "(api.x.ai is the metered developer API); using the default endpoint.",
                   _SUBSCRIPTION_DOMAIN, _SUBSCRIPTION_DOMAIN)
    return DEFAULT_BASE


def endpoint() -> str:
    """The chat-completions URL a call posts to."""
    return f"{base_url()}/chat/completions"


def _cache_key(system: str, user: str, model: str) -> str:
    """The ``llm_cache`` key of a Grok reply: the provider leads, so a Grok reply never replays as DeepSeek's (whose
    key is ``deepseek._cache_key``, a hash of model, thinking, effort and the prompt) or back, whatever the model id."""
    h = hashlib.sha256()
    h.update(f"{PROVIDER}\x00{model}\x00{system}\x00{user}".encode("utf-8"))
    return h.hexdigest()[:32]


def _cache_get(key: str) -> str | None:
    """Durable cache read (``llm_cache``). Never raises."""
    try:
        row = rawstore.get("llm_cache", key)
    except Exception:  # noqa: BLE001 - the table may not exist yet
        return None
    resp = (row or {}).get("response")
    if isinstance(resp, str):
        try:
            resp = json.loads(resp)
        except (json.JSONDecodeError, TypeError):
            return None
    return resp.get("content") if isinstance(resp, dict) else None


def _cache_put(key: str, content: str, meta: dict) -> None:
    """Durable cache write (``llm_cache``): the row's ``model`` is the Grok model, its response names the provider."""
    try:
        rawstore.ensure_table("llm_cache")
        rawstore.write_rows("llm_cache", [{
            "cache_key": key,
            "ticker": (meta.get("ticker") or "").upper(),
            "cik": str(meta.get("cik") or ""),
            "model": meta.get("model") or MODEL,
            "thinking": 0,
            "effort": "",
            "prompt_version": meta.get("prompt_version") or "",
            "input_hash": key,
            "response": {"content": content, "usage": meta.get("usage") or {}, "provider": PROVIDER},
            "fetched_at": time.time(),
            "status": "ok",
        }])
    except Exception as exc:  # noqa: BLE001 - a cache write must not fail the call
        logger.warning("llm_cache write failed: %s", exc)


def _scrub(text, token: str) -> str:
    """``text`` (cut to 300 characters) with the token removed: safe for a message or a log."""
    s = str(text or "")
    if token:
        s = s.replace(token, "[token]")
    return s[:300]


def _check_allowance(now) -> None:
    """The allowance gate: a reading before the first request and after every ``GATE_EVERY`` requests."""
    with _state_lock:
        since = _gate["since"]
        if since is not None and since < GATE_EVERY:
            _gate["since"] = since + 1
            return
        try:
            windows = subscription_usage.fetch_grok(now)
        except subscription_usage.SubscriptionUsageError as exc:
            cause = exc.__cause__
            if isinstance(cause, urllib.error.HTTPError) and cause.code in (401, 403):
                raise GrokAuthError(str(exc)) from exc
            raise GrokAllowanceError(f"SuperGrok usage could not be read ({exc}); stopped rather than spend "
                                     "blind.") from exc
        except Exception as exc:  # noqa: BLE001 - a dropped connection or an odd billing shape: stop the run too
            raise GrokAllowanceError(f"SuperGrok usage could not be read ({type(exc).__name__}); stopped rather "
                                     "than spend blind.") from exc
        cap = subscription_usage.grok_weekly_cap()
        if not subscription_usage.is_within_budget(windows, max_week=cap):
            raise GrokAllowanceError(subscription_usage.describe("Grok", windows, max_week=cap, now=now),
                                     subscription_usage.next_available(windows, max_week=cap, now=now))
        _gate["since"] = 1


def preflight(now=None) -> None:
    """The login, then the allowance, read once before a Grok run writes anything or calls any other service (review B
    M1). Raises ``GrokAuthError`` or ``GrokAllowanceError``; the run's first request reads the allowance again."""
    try:
        subscription_usage.xai_token(now)
    except subscription_usage.SubscriptionUsageError as exc:
        raise GrokAuthError(str(exc)) from None
    reset_gate()
    try:
        _check_allowance(now)
    finally:
        reset_gate()


def _retry_delay(resp, attempt: int) -> float:
    raw = (resp.headers.get("Retry-After") or "").strip() if resp is not None else ""
    if raw:
        try:
            wait = float(raw)
        except ValueError:
            wait = math.nan
        if math.isfinite(wait) and wait >= 0:
            return min(_MAX_DELAY, wait)
    return min(_MAX_DELAY, _random() * _RETRY_BASE * 2.0 ** min(max(attempt - 1, 0), 16))


def build_body(system: str, user: str, *, max_tokens: int, json_mode: bool) -> dict:
    """The request body: model, the two messages, ``max_tokens``, no streaming, and the JSON mode when asked.
    Nothing else is sent (see the module note on DeepSeek's knobs)."""
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def complete(system: str, user: str, *, thinking: bool = False,
             reasoning_effort: str | None = None,
             temperature: float = 0.0, max_tokens: int = 1500,
             json_mode: bool = True, use_cache: bool = True,
             client: httpx.Client | None = None, now=None,
             ticker: str | None = None, cik=None,
             prompt_version: str | None = None,
             validate: Callable[[str], bool] | None = None) -> str:
    """One chat completion through SuperGrok: cache, then the login, then the allowance gate, then HTTP.

    The signature is ``deepseek.complete``'s, so a caller swaps the client and nothing else. ``thinking``,
    ``reasoning_effort`` and ``temperature`` are accepted and **not sent** (DeepSeek parameters). ``validate`` vetoes
    caching an answer the caller cannot use, as for DeepSeek. ``use_cache=False`` neither reads nor writes the cache.
    """
    global _calls
    del thinking, reasoning_effort, temperature          # DeepSeek knobs: dropped on purpose, never sent
    key = _cache_key(system, user, MODEL)
    if use_cache:
        hit = _cache_get(key)
        if hit is not None:
            return hit

    try:
        token = subscription_usage.xai_token(now)
    except subscription_usage.SubscriptionUsageError as exc:
        raise GrokAuthError(str(exc)) from None
    _check_allowance(now)

    body = build_body(system, user, max_tokens=max_tokens, json_mode=json_mode)
    own = client is None
    if own:
        client = _new_client()
    url = endpoint()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "application/json", "User-Agent": USER_AGENT}
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            with _state_lock:
                _calls += 1
            try:
                r = client.post(url, json=body, headers=headers)
            except _PRE_SEND_ERRORS as exc:
                if attempt < MAX_ATTEMPTS:
                    _sleep(_retry_delay(None, attempt))
                    continue
                raise GrokTransientError(f"Grok request failed: {_scrub(exc, token)}") from None
            except httpx.HTTPError as exc:   # the request may have run: never replayed
                raise GrokTransientError(f"Grok request failed: {_scrub(exc, token)}") from None
            status = r.status_code
            if status == 429:
                if attempt < MAX_ATTEMPTS:
                    _sleep(_retry_delay(r, attempt))
                    continue
                raise GrokTransientError(f"Grok HTTP 429: {_scrub(r.text, token)}")
            if 500 <= status < 600:
                raise GrokTransientError(f"Grok HTTP {status}: {_scrub(r.text, token)}")
            if status in (401, 403):
                raise GrokAuthError(f"SuperGrok rejected the login (HTTP {status}): {_scrub(r.text, token)}. "
                                    "Renew it with any `hermes` command and re-run.")
            if status == 402:
                raise GrokAllowanceError(f"SuperGrok refused the call (HTTP 402): {_scrub(r.text, token)}")
            if status >= 400:
                raise GrokRequestError(f"Grok HTTP {status} from {url} (model {MODEL}): {_scrub(r.text, token)}")
            return _finish(r, key, token, use_cache, validate, ticker, cik, prompt_version)
        raise GrokError("Grok request failed")  # pragma: no cover - the loop always returns or raises
    finally:
        if own:
            client.close()


def _content(data) -> str | None:
    """The message text of an OpenAI-compatible reply (a string, or a list of text parts joined)."""
    choices = data.get("choices") if isinstance(data, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return content if isinstance(content, str) and content.strip() else None


def _finish(r, key: str, token: str, use_cache: bool, validate, ticker, cik, prompt_version) -> str:
    """Turn a 2xx reply into its content (and cache it when the caller accepts it)."""
    try:
        data = r.json()
    except ValueError:
        raise GrokError(f"Grok returned a reply that is not JSON: {_scrub(r.text, token)}") from None
    content = _content(data)
    if content is None:
        raise GrokError(f"Grok returned no content: {_scrub(json.dumps(data)[:300], token)}")
    cacheable = use_cache
    if cacheable and validate is not None:
        try:
            cacheable = bool(validate(content))
        except Exception as exc:  # noqa: BLE001 - a caller's bug must not lose the answer
            logger.warning("grok validate hook raised %s; not caching", type(exc).__name__)
            cacheable = False
    if cacheable:
        _cache_put(key, content, {"model": MODEL, "usage": data.get("usage"), "ticker": ticker, "cik": cik,
                                  "prompt_version": prompt_version})
    return content


__all__ = ["complete", "preflight", "build_body", "endpoint", "base_url", "calls", "reset_gate", "MODEL", "PROVIDER",
           "COST_USD_PER_CALL", "MAX_ATTEMPTS", "GATE_EVERY", "GrokError", "GrokAuthError", "GrokTransientError",
           "GrokRequestError", "GrokAllowanceError"]
