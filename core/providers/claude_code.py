"""Claude subscription chat client for the Relatedness profile writer (avo, 11 Nov 2026): headless Claude Code.

avo: "Bu isi deepseek yerine Claude den bir model yapsa? ... claude subscription ile kullaniliyor ... sadece ilk tur
claude ile sonra deepseek e donebiliriz". So this client spends the **Claude subscription** (a Claude Code login on
the box) and **never an API key**: each call runs ``claude -p`` (print mode, no tools, no MCP, no settings, no saved
session) with ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` / ``ANTHROPIC_BASE_URL`` removed from its environment,
so the CLI can only use its OAuth login. ``RC_CLAUDE_CONFIG_DIR`` picks the login (account A or B) and is handed to
the CLI as ``CLAUDE_CONFIG_DIR``; the allowance gate reads the same login.

It mirrors ``grok.complete`` (signature, cache, allowance gate), and its errors subclass Grok's, so
``module_relatedness.engine`` and ``compare`` stop a run on them exactly as on Grok's (``allowance`` / ``auth`` /
``request`` / ``transient``), as EVREN's subclass DeepSeek's. Access order: **cache -> allowance -> CLI**. The gate is
``subscription_usage.fetch_claude`` before the first call and after every ``GATE_EVERY`` calls; the run stops at
``subscription_usage.claude_weekly_cap()`` of the weekly window (``RC_CLAUDE_CAP``, 80% by default) or
``DEFAULT_MAX_SESSION`` of the five-hour one, or when the usage cannot be read.

``thinking``, ``reasoning_effort`` and ``temperature`` are DeepSeek knobs: accepted, not sent. ``max_tokens`` reaches
the CLI as ``CLAUDE_CODE_MAX_OUTPUT_TOKENS``; ``json_mode`` is the prompt's job (``profile_v3.parse`` strips a fence).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
from typing import Callable

from .. import rawstore, subscription_usage
from . import grok

logger = logging.getLogger(__name__)

PROVIDER = "claude"
MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5")
MODEL = os.getenv("RC_CLAUDE_MODEL") or MODELS[0]
COST_USD_PER_CALL = 0.0      # a subscription allowance, not a per-token bill
GATE_EVERY = 25              # calls between two allowance readings
TIMEOUT_S = 600
# Never handed to the CLI: any of them would bill an API key instead of the subscription (avo: no API keys for build).
_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


class ClaudeError(grok.GrokError):
    """A reply that cannot be used (no result, not JSON)."""


class ClaudeAuthError(ClaudeError, grok.GrokAuthError):
    """The Claude Code login is missing, expired or was rejected. Stop; do not retry."""


class ClaudeAllowanceError(ClaudeError, grok.GrokAllowanceError):
    """The subscription window is at its ceiling (or could not be read): stop and resume after ``next_reset``."""


class ClaudeTransientError(ClaudeError, grok.GrokTransientError):
    """A timeout, a crash or an overloaded reply: the service, not the request."""


class ClaudeRequestError(ClaudeError, grok.GrokRequestError):
    """No ``claude`` binary, or a model it refuses: every call would fail alike, so the run stops."""


_state_lock = threading.Lock()
_calls = 0
_gate = {"since": None}

# Patch point for tests.
_run = subprocess.run


def calls() -> int:
    """CLI calls made by this process. Each costs 0 USD."""
    return _calls


def reset_gate() -> None:
    """Make the next call read the allowance first (a run calls this as it starts)."""
    with _state_lock:
        _gate["since"] = None


def _cache_key(system: str, user: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(f"{PROVIDER}\x00{model}\x00{system}\x00{user}".encode("utf-8"))
    return h.hexdigest()[:32]


def _check_allowance(now) -> None:
    with _state_lock:
        since = _gate["since"]
        if since is not None and since < GATE_EVERY:
            _gate["since"] = since + 1
            return
        try:
            windows = subscription_usage.fetch_claude(now)
        except subscription_usage.SubscriptionUsageError as exc:
            cause = exc.__cause__
            rejected = isinstance(cause, urllib.error.HTTPError) and cause.code in (401, 403)
            if rejected or not isinstance(cause, (urllib.error.URLError, TimeoutError)):
                raise ClaudeAuthError(str(exc)) from exc       # no login file, an expired or a rejected login
            raise ClaudeAllowanceError(f"Claude usage could not be read ({exc}); stopped rather than spend "
                                       "blind.") from exc
        except Exception as exc:  # noqa: BLE001 - an odd usage shape: stop the run too
            raise ClaudeAllowanceError(f"Claude usage could not be read ({type(exc).__name__}); stopped rather "
                                       "than spend blind.") from exc
        cap = subscription_usage.claude_weekly_cap()
        if not subscription_usage.is_within_budget(windows, max_week=cap):
            raise ClaudeAllowanceError(subscription_usage.describe("Claude", windows, max_week=cap, now=now),
                                       subscription_usage.next_available(windows, max_week=cap, now=now))
        _gate["since"] = 1


def preflight(now=None) -> None:
    """The login and the allowance, read once before a run writes anything."""
    reset_gate()
    try:
        _check_allowance(now)
    finally:
        reset_gate()


def command(system: str, model: str) -> list[str]:
    """The CLI call: print mode, JSON result, no tools, no MCP servers, no settings files, no saved session."""
    return [os.getenv("RC_CLAUDE_BIN") or "claude", "-p", "--model", model, "--output-format", "json",
            "--system-prompt", system, "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--no-session-persistence"]


def child_env(max_tokens: int) -> dict:
    """The CLI's environment: this one without any Anthropic key, with the chosen login and the output ceiling."""
    env = {k: v for k, v in os.environ.items() if k not in _KEY_VARS}
    conf = subscription_usage.claude_config_dir()
    if conf is not None:
        env["CLAUDE_CONFIG_DIR"] = str(conf)
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(int(max_tokens))
    return env


def _classify(text: str) -> type[ClaudeError]:
    low = text.lower()
    if "limit" in low:
        return ClaudeAllowanceError
    if "login" in low or "oauth" in low or "authenticat" in low or "api key" in low:
        return ClaudeAuthError
    if "model" in low and ("not found" in low or "invalid" in low or "not available" in low):
        return ClaudeRequestError
    return ClaudeTransientError


def complete(system: str, user: str, *, thinking: bool = False,
             reasoning_effort: str | None = None,
             temperature: float = 0.0, max_tokens: int = 1500,
             json_mode: bool = True, use_cache: bool = True,
             client=None, now=None,
             ticker: str | None = None, cik=None,
             prompt_version: str | None = None,
             validate: Callable[[str], bool] | None = None,
             model: str | None = None) -> str:
    """One answer from headless Claude Code on the subscription: cache, then the allowance gate, then the CLI.

    The signature is ``deepseek.complete``'s; ``client`` and the DeepSeek knobs are accepted and unused."""
    global _calls
    del thinking, reasoning_effort, temperature, json_mode, client
    model = model or MODEL
    key = _cache_key(system, user, model)
    if use_cache:
        hit = grok._cache_get(key)
        if hit is not None:
            return hit
    _check_allowance(now)
    with _state_lock:
        _calls += 1
    try:
        proc = _run(command(system, model), input=user, capture_output=True, text=True, timeout=TIMEOUT_S,
                    env=child_env(max_tokens), cwd=tempfile.gettempdir())
    except FileNotFoundError as exc:
        raise ClaudeRequestError(f"the claude CLI is not installed for this user ({exc.filename})") from None
    except subprocess.TimeoutExpired:
        raise ClaudeTransientError(f"claude -p gave no answer in {TIMEOUT_S} s") from None
    try:
        data = json.loads(proc.stdout or "")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        text = (proc.stderr or proc.stdout or "").strip()[:300]
        raise _classify(text)(f"claude -p exit {proc.returncode}: {text}")
    content = data.get("result")
    if data.get("is_error") or proc.returncode != 0:
        text = str(content or data.get("subtype") or "")[:300]
        raise _classify(text)(f"claude -p ({model}) failed: {text}")
    if not isinstance(content, str) or not content.strip():
        raise ClaudeError(f"claude -p ({model}) returned no result")
    cacheable = use_cache
    if cacheable and validate is not None:
        try:
            cacheable = bool(validate(content))
        except Exception as exc:  # noqa: BLE001 - a caller's bug must not lose the answer
            logger.warning("claude validate hook raised %s; not caching", type(exc).__name__)
            cacheable = False
    if cacheable:
        _cache_put(key, content, model, data.get("usage"), ticker, cik, prompt_version)
    return content


def _cache_put(key, content, model, usage, ticker, cik, prompt_version) -> None:
    try:
        rawstore.ensure_table("llm_cache")
        rawstore.write_rows("llm_cache", [{
            "cache_key": key, "ticker": (ticker or "").upper(), "cik": str(cik or ""), "model": model,
            "thinking": 0, "effort": "", "prompt_version": prompt_version or "", "input_hash": key,
            "response": {"content": content, "usage": usage or {}, "provider": PROVIDER},
            "fetched_at": time.time(), "status": "ok",
        }])
    except Exception as exc:  # noqa: BLE001 - a cache write must not fail the call
        logger.warning("llm_cache write failed: %s", exc)


__all__ = ["complete", "preflight", "command", "child_env", "calls", "reset_gate", "MODEL", "MODELS", "PROVIDER",
           "COST_USD_PER_CALL", "GATE_EVERY", "ClaudeError", "ClaudeAuthError", "ClaudeAllowanceError",
           "ClaudeTransientError", "ClaudeRequestError"]
