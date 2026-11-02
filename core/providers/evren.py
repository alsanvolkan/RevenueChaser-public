"""EVREN chat client (#360): an OpenAI-compatible endpoint with no fee, the default Relatedness profile writer.

Access order: **cache -> key -> HTTP**. There is no off-peak gate (EVREN does not bill by time of day) and no
thinking knob. The key is ``EVREN_API_KEY`` (``core.credentials``), read from the environment at call time; the
dashboard and the relatedness unit get it from ``/etc/life/secrets.env``. Base URL (``RC_EVREN_BASE``) and model
(``RC_EVREN_MODEL``, or the board's ``WRITER_MODEL``, ``module_relatedness.writers``) are overridable so the
end-to-end harness can point them at a mock.

Retries follow ``core.providers.deepseek`` (D2): one retry, only when the request is known not to have run (HTTP 429
and the errors raised before it left the machine); a 5xx is final. The error classes are DeepSeek's subclasses, so
``module_relatedness.engine`` stops a run on them exactly as it does for DeepSeek (``auth`` / ``transient``).
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from typing import Callable

import httpx

from .. import rawstore
from ..credentials import key_for
from . import deepseek

logger = logging.getLogger(__name__)

PROVIDER = "evren"
DEFAULT_BASE = "https://evren-llmapi.ssyz.org.tr/v1"
MODEL = os.getenv("RC_EVREN_MODEL") or "deepseek-v4.1-flash"
COST_USD_PER_CALL = 0.0
MAX_ATTEMPTS = 2
_PRE_SEND_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_calls = 0
_calls_lock = threading.Lock()


class EvrenError(deepseek.DeepSeekError):
    """An EVREN reply the caller cannot use (bad status, no content)."""


class EvrenAuthError(EvrenError, deepseek.DeepSeekAuthError):
    """The key is missing or rejected (401 / 403): the next run cannot fix it."""


class EvrenTransientError(EvrenError, deepseek.DeepSeekTransientError):
    """A 429 after the retry, a 5xx, or a network failure."""


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _new_client() -> httpx.Client:
    return httpx.Client(timeout=120)


def calls() -> int:
    """HTTP requests sent since the process started (the CLI's per-run count; a cache hit is not one)."""
    return _calls


def _count() -> None:
    global _calls
    with _calls_lock:
        _calls += 1


def base_url() -> str:
    """The API base URL, read from ``RC_EVREN_BASE`` at call time (no trailing slash)."""
    return (os.getenv("RC_EVREN_BASE") or "").strip().rstrip("/") or DEFAULT_BASE


def _cache_key(system: str, user: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(f"{PROVIDER}\x00{model}\x00{system}\x00{user}".encode("utf-8"))
    return h.hexdigest()[:32]


def _cache_put(key: str, content: str, meta: dict) -> None:
    """Durable cache write (``llm_cache``), the reply naming its provider. Never raises."""
    try:
        rawstore.ensure_table("llm_cache")
        rawstore.write_rows("llm_cache", [{
            "cache_key": key, "ticker": (meta.get("ticker") or "").upper(), "cik": str(meta.get("cik") or ""),
            "model": meta.get("model") or MODEL, "thinking": 0, "effort": "", "prompt_version": meta.get("prompt_version") or "",
            "input_hash": key,
            "response": {"content": content, "usage": meta.get("usage") or {}, "provider": PROVIDER},
            "fetched_at": time.time(), "status": "ok",
        }])
    except Exception as exc:  # noqa: BLE001 - a cache write must not fail the call
        logger.warning("llm_cache write failed: %s", exc)


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "[key]") if key else text


def complete(system: str, user: str, *, thinking: bool = False,
             reasoning_effort: str | None = None,
             temperature: float = 0.0, max_tokens: int = 1500,
             json_mode: bool = True, use_cache: bool = True,
             client: httpx.Client | None = None, now=None,
             ticker: str | None = None, cik=None,
             prompt_version: str | None = None,
             validate: Callable[[str], bool] | None = None,
             model: str | None = None) -> str:
    """One chat completion: cache, then the key, then HTTP. ``thinking`` / ``reasoning_effort`` / ``now`` are
    accepted for the writer signature (``module_relatedness.profiles``) and not sent. ``model`` is the board's
    (#360), else ``MODEL``; it is a parameter, never a module-level swap."""
    model = model or MODEL
    key = _cache_key(system, user, model)
    if use_cache:
        hit = deepseek._cache_get(key)
        if hit is not None:
            return hit

    api_key = key_for("evren")
    if not api_key:
        raise EvrenAuthError("EVREN API key is missing. Set EVREN_API_KEY in /etc/life/secrets.env "
                             "(see core/credentials.py).")

    body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens, "temperature": temperature, "stream": False}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    own = client is None
    if own:
        client = _new_client()
    url = f"{base_url()}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            _count()
            try:
                r = client.post(url, json=body, headers=headers)
            except _PRE_SEND_ERRORS as exc:
                if attempt < MAX_ATTEMPTS:
                    _sleep(deepseek._retry_delay(None, attempt))
                    continue
                raise EvrenTransientError(f"EVREN request failed: {_scrub(str(exc), api_key)}") from exc
            except httpx.HTTPError as exc:      # the request may have run: never replayed
                raise EvrenTransientError(f"EVREN request failed: {_scrub(str(exc), api_key)}") from exc
            text = _scrub((r.text or "")[:300], api_key)
            if r.status_code == 429:
                if attempt < MAX_ATTEMPTS:
                    _sleep(deepseek._retry_delay(r, attempt))
                    continue
                raise EvrenTransientError(f"EVREN HTTP 429: {text}")
            if 500 <= r.status_code < 600:
                raise EvrenTransientError(f"EVREN HTTP {r.status_code}: {text}")
            if r.status_code in (401, 403):
                raise EvrenAuthError(f"EVREN rejected the API key (HTTP {r.status_code}). "
                                     f"Renew EVREN_API_KEY in /etc/life/secrets.env and re-run.")
            if r.status_code >= 400:
                raise EvrenError(f"EVREN HTTP {r.status_code}: {text}")
            return _finish(r, key, use_cache, validate, ticker, cik, prompt_version, model, api_key)
        raise EvrenError("EVREN request failed")  # pragma: no cover - the loop always returns or raises
    finally:
        if own:
            client.close()


def _finish(r, key: str, use_cache: bool, validate, ticker, cik, prompt_version, model: str, api_key: str) -> str:
    try:
        data = r.json()
    except ValueError as exc:
        raise EvrenError(f"EVREN returned a reply that is not JSON: {_scrub((r.text or '')[:300], api_key)}") from exc
    choices = data.get("choices") if isinstance(data, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not content:
        raise EvrenError(f"EVREN returned no content: {_scrub(str(data)[:300], api_key)}")
    cacheable = use_cache
    if cacheable and validate is not None:
        try:
            cacheable = bool(validate(content))
        except Exception as exc:  # noqa: BLE001 - a caller's bug must not lose the answer
            logger.warning("evren validate hook raised %s: %s; not caching", type(exc).__name__, exc)
            cacheable = False
    if cacheable:
        _cache_put(key, content, {"usage": data.get("usage"), "ticker": ticker, "cik": cik,
                                  "prompt_version": prompt_version, "model": model})
    return content


__all__ = ["complete", "calls", "EvrenError", "EvrenAuthError", "EvrenTransientError", "MODEL", "MAX_ATTEMPTS",
           "COST_USD_PER_CALL", "base_url"]
