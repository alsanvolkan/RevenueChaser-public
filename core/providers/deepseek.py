"""DeepSeek chat client with an off-peak gate and a local result cache.

Access order is deliberate: **cache -> off-peak gate -> HTTP**. A cached answer
is served even at peak (it costs nothing); a cache miss at peak raises
``PeakHoursError`` without making a request, so the caller stops and resumes on
the next off-peak run instead of paying the peak rate.

The gate is ``core.offpeak``; it never sleeps. The Hermes usage card can lift
it for the current peak window only (``peak_override_active``). The key is ``DEEPSEEK_API_KEY``
(see ``core.credentials``); base URL and model are env-overridable so the
end-to-end harness can point them at a mock. The base URL is read when a call
is made (``base_url()``), so a test or a restarted job sees a changed value.

Retries (D2): at most ``MAX_ATTEMPTS`` calls. A call is repeated only when the
request is known not to have run: HTTP 429 and the errors raised before the
request left the machine (connect error, connect timeout, pool timeout). A 5xx
or a failure after the send may already be billed, so those end the call at
once as ``DeepSeekTransientError``. The wait between attempts is jittered and
never above ``_MAX_DELAY`` seconds.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import time
from pathlib import Path
from typing import Callable

import httpx

from .. import offpeak, rawstore
from ..credentials import key_for

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.deepseek.com"
MODEL = os.getenv("RC_DEEPSEEK_MODEL") or "deepseek-flash"
MAX_ATTEMPTS = 2        # one call and at most one retry (a retried 429 is not billed)
_RETRY_BASE = 2.0       # seconds; the first retry waits uniform(0, _RETRY_BASE)
_MAX_DELAY = 30.0       # no wait is ever longer than this, whatever the server asks
_RETRY_AFTER_JITTER = 0.25
# Raised before the request left the machine: sending it again cannot bill twice.
_PRE_SEND_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# Patch points for tests (a test that cares about the delays patches these).
_sleep = time.sleep
_random = random.random


class DeepSeekError(RuntimeError):
    """A non-recoverable DeepSeek call failure (missing key, bad response)."""


class DeepSeekAuthError(DeepSeekError):
    """The API key was rejected (401/403). Callers should stop, not retry."""


class DeepSeekTransientError(DeepSeekError):
    """429 / 5xx / timeout / connection failure: the service, not the request."""


class PeakHoursError(RuntimeError):
    """Raised instead of calling the API during peak billing hours."""

    def __init__(self, next_off_peak):
        self.next_off_peak = next_off_peak
        super().__init__(
            f"DeepSeek peak hours — call skipped. Next off-peak: "
            f"{next_off_peak.strftime('%Y-%m-%d %H:%M')} UTC."
        )


def base_url() -> str:
    """The API base URL, read from ``RC_DEEPSEEK_BASE`` at call time (no trailing slash)."""
    return (os.getenv("RC_DEEPSEEK_BASE") or "").strip().rstrip("/") or DEFAULT_BASE


def off_peak_only() -> bool:
    """Whether the off-peak gate is active (default on)."""
    return (os.getenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "1").strip() or "1") != "0"


def gate_file() -> Path:
    """The peak-override file the Hermes usage card writes.

    ``DEEPSEEK_GATE_FILE`` wins; otherwise ``<HERMES_HOME>/deepseek_gate.json``,
    defaulting to ``%LOCALAPPDATA%/hermes`` (Windows) or ``~/.hermes``.
    """
    env = os.getenv("DEEPSEEK_GATE_FILE")
    if env:
        return Path(env)
    home = os.getenv("HERMES_HOME")
    if not home:
        local = os.getenv("LOCALAPPDATA")
        home = str(Path(local) / "hermes") if local else str(Path.home() / ".hermes")
    return Path(home) / "deepseek_gate.json"


def peak_override_active(now=None) -> bool:
    """True while the user has allowed peak-hour calls from the usage card.

    The card writes ``{"allowPeak": true, "until": <epoch s>}`` with ``until`` =
    the end of the current peak window, so the override lapses by itself. A
    missing, unreadable or expired file means "not allowed" (fail closed).
    """
    try:
        data = json.loads(gate_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not data.get("allowPeak"):
        return False
    ts = offpeak._as_utc(now).timestamp() if now is not None else time.time()
    try:
        return float(data.get("until") or 0) > ts
    except (TypeError, ValueError):
        return False


def peak_blocked(now=None) -> bool:
    """The one gate every caller uses: off-peak-only mode, at peak, no override."""
    return off_peak_only() and not offpeak.is_off_peak(now) and not peak_override_active(now)


def _cache_key(system: str, user: str, thinking: bool, model: str,
               effort: str | None = None) -> str:
    h = hashlib.sha256()
    h.update(f"{model}\x00{int(thinking)}\x00{effort or ''}\x00{system}\x00{user}".encode("utf-8"))
    return h.hexdigest()[:32]


def _cache_get(key: str) -> str | None:
    """Durable cache read (``llm_cache`` table). Never raises."""
    try:
        row = rawstore.get("llm_cache", key)
    except Exception:  # noqa: BLE001 - table may not exist yet
        return None
    if not row:
        return None
    resp = row.get("response")
    if isinstance(resp, dict):
        return resp.get("content")
    if isinstance(resp, str):
        try:
            return json.loads(resp).get("content")
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _cache_put(key: str, content: str, meta: dict) -> None:
    """Durable cache write (``llm_cache`` table)."""
    try:
        rawstore.ensure_table("llm_cache")
        rawstore.write_rows("llm_cache", [{
            "cache_key": key,
            "ticker": (meta.get("ticker") or "").upper(),
            "cik": str(meta.get("cik") or ""),
            "model": meta.get("model") or MODEL,
            "thinking": int(bool(meta.get("thinking"))),
            "effort": meta.get("effort") or "",
            "prompt_version": meta.get("prompt_version") or "",
            "input_hash": meta.get("input_hash") or key,
            "response": {"content": content, "usage": meta.get("usage") or {}},
            "fetched_at": time.time(),
            "status": meta.get("status") or "ok",
        }])
    except Exception as exc:  # noqa: BLE001 - a cache write must not fail the call
        logger.warning("llm_cache write failed: %s", exc)


def _retry_delay(resp, attempt: int) -> float:
    """Seconds to wait before retry number ``attempt`` (1-based), never above ``_MAX_DELAY``.

    A usable ``Retry-After`` (a finite, non-negative number of seconds) gets up
    to 25% jitter on top and is then capped. Anything else (no header, an HTTP
    date, ``nan``, a negative number) falls back to full-jitter backoff,
    ``uniform(0, _RETRY_BASE * 2**(attempt-1))``. The cap comes last, so the
    jitter cannot push a wait past it.
    """
    raw = (resp.headers.get("Retry-After") or "").strip() if resp is not None else ""
    if raw:
        try:
            wait = float(raw)
        except ValueError:
            wait = math.nan
        if math.isfinite(wait) and wait >= 0:
            return min(_MAX_DELAY, wait * (1 + _RETRY_AFTER_JITTER * _random()))
    return min(_MAX_DELAY, _random() * _RETRY_BASE * 2.0 ** min(max(attempt - 1, 0), 16))


def complete(system: str, user: str, *, thinking: bool = False,
             reasoning_effort: str | None = None,
             temperature: float = 0.0, max_tokens: int = 1500,
             json_mode: bool = True, use_cache: bool = True,
             client: httpx.Client | None = None, now=None,
             ticker: str | None = None, cik=None,
             prompt_version: str | None = None,
             validate: Callable[[str], bool] | None = None) -> str:
    """One chat completion. Cache first, then the off-peak gate, then HTTP.

    In thinking mode ``temperature`` is omitted (the API ignores it) and
    ``reasoning_effort`` (``low`` / ``high`` / ``max``) is forwarded; reasoning
    tokens count against ``max_tokens``, so callers should budget generously.

    ``validate`` lets the caller veto an answer it cannot use (for example a
    reply that is not the JSON it asked for). When it returns False, or raises,
    the content is still returned but not cached, so the next call asks again
    instead of replaying the bad answer from ``llm_cache``.
    """
    effort = (reasoning_effort or "").lower() or None
    key = _cache_key(system, user, thinking, MODEL, effort)
    if use_cache:
        hit = _cache_get(key)
        if hit is not None:
            return hit

    if peak_blocked(now):
        raise PeakHoursError(offpeak.next_off_peak(now))

    api_key = key_for("deepseek")
    if not api_key:
        raise DeepSeekAuthError(
            "DeepSeek API key is missing. Set DEEPSEEK_API_KEY (see core/credentials.py).")

    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "stream": False,
        "thinking": {"type": "enabled" if thinking else "disabled"},
    }
    if thinking:
        if effort:
            body["reasoning_effort"] = effort
    else:
        body["temperature"] = temperature
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    own = client is None
    if own:
        client = httpx.Client(timeout=120)
    url = f"{base_url()}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                r = client.post(url, json=body, headers=headers)
            except _PRE_SEND_ERRORS as exc:
                if attempt < MAX_ATTEMPTS:
                    _sleep(_retry_delay(None, attempt))
                    continue
                raise DeepSeekTransientError(f"DeepSeek request failed: {exc}") from exc
            except httpx.HTTPError as exc:   # the request may have run: never replayed
                raise DeepSeekTransientError(f"DeepSeek request failed: {exc}") from exc
            if r.status_code == 429:
                if attempt < MAX_ATTEMPTS:
                    _sleep(_retry_delay(r, attempt))
                    continue
                raise DeepSeekTransientError(f"DeepSeek HTTP 429: {r.text[:300]}")
            if 500 <= r.status_code < 600:   # may already be billed: final
                raise DeepSeekTransientError(f"DeepSeek HTTP {r.status_code}: {r.text[:300]}")
            if r.status_code in (401, 403):
                raise DeepSeekAuthError(
                    f"DeepSeek rejected the API key (HTTP {r.status_code}). "
                    f"Renew DEEPSEEK_API_KEY and re-run.")
            if r.status_code >= 400:
                raise DeepSeekError(f"DeepSeek HTTP {r.status_code}: {r.text[:300]}")
            return _finish(r, key, use_cache, validate, effort, thinking, ticker, cik,
                           prompt_version)
        raise DeepSeekError("DeepSeek request failed")  # pragma: no cover - loop always returns or raises
    finally:
        if own:
            client.close()


def _finish(r, key: str, use_cache: bool, validate, effort, thinking: bool, ticker, cik,
            prompt_version) -> str:
    """Turn a 2xx reply into the message content (and cache it when the caller accepts it)."""
    try:
        data = r.json()
    except ValueError as exc:
        raise DeepSeekError(f"DeepSeek returned a reply that is not JSON: {r.text[:300]}") from exc
    if not isinstance(data, dict):
        raise DeepSeekError(f"DeepSeek returned an unexpected reply: {str(data)[:300]}")
    choices = data.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not content:
        raise DeepSeekError(f"DeepSeek returned no content: {str(data)[:300]}")
    cacheable = use_cache
    if cacheable and validate is not None:
        try:
            cacheable = bool(validate(content))
        except Exception as exc:  # noqa: BLE001 - a caller's bug must not lose the answer
            logger.warning("deepseek validate hook raised %s: %s; not caching", type(exc).__name__, exc)
            cacheable = False
    if cacheable:
        _cache_put(key, content, {"model": MODEL, "thinking": thinking,
                                  "effort": effort, "usage": data.get("usage"),
                                  "ticker": ticker, "cik": cik,
                                  "prompt_version": prompt_version,
                                  "input_hash": key})
    return content


__all__ = ["complete", "PeakHoursError", "DeepSeekError", "DeepSeekAuthError",
           "DeepSeekTransientError", "off_peak_only", "peak_override_active", "peak_blocked", "gate_file",
           "MODEL", "MAX_ATTEMPTS", "base_url"]
