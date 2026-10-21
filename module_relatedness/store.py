"""Durable Relatedness store (raw.duckdb) — Phase 6.

Replaces the ``.cache/*.json`` profile files. A profile is a fixed schema of
string lists; it is stored one row per field in ``relatedness_attributes``
(``value`` = JSON list) plus a metadata row in ``relatedness_profiles``. The raw
LLM response is kept in ``llm_cache`` under a structured cache key, and the
profile mode is stamped in ``run_registry``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time

from RevenueChaser.core import rawstore

from . import config

logger = logging.getLogger(__name__)

MODE_KEY = "profiles_mode"
PROMPT_VERSION = config.PROMPT_VERSION


def prompt_hash(system: str, user: str) -> str:
    """SHA-256 over the exact prompt bytes (system + separator + user)."""
    return hashlib.sha256((system + "\n\n" + user).encode("utf-8")).hexdigest()


# Writers other than the mode's own (DeepSeek) whose profile at the current prompt version is current for every run
# (#388, avo 2026-10-16 "grok kullan direk"): a Grok profile is not redone by the DeepSeek nightly, and back.
ALT_WRITERS = frozenset({"grok"})


def cache_key(model, thinking, effort, prompt_version, ticker, input_hash, provider=None) -> str:
    """Structured (sorted-keys) cache key — stable across dict ordering.

    ``provider`` names a writer other than DeepSeek (``"grok"``, #388); a DeepSeek key carries none, so its bytes
    are what they were before Grok.
    """
    key = {
        "model": model or "", "thinking": int(bool(thinking)), "effort": effort or "",
        "prompt_version": prompt_version, "ticker": (ticker or "").upper(),
        "input_hash": input_hash,
    }
    if provider and provider != "deepseek":
        key["provider"] = provider
    return json.dumps(key, sort_keys=True, separators=(",", ":"))


def mode_stamp() -> dict:
    from RevenueChaser.core.providers import deepseek
    return {"model": deepseek.MODEL, "thinking": bool(config.THINKING),
            "effort": config.REASONING_EFFORT, "prompt_version": PROMPT_VERSION}


def get_mode() -> dict | None:
    row = rawstore.get("run_registry", MODE_KEY)
    if not row:
        return None
    v = row["value"]
    if isinstance(v, dict):          # rawstore auto-decodes JSON strings on read
        return v
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return None


def set_mode(mode: dict) -> None:
    rawstore.write_rows("run_registry",
                        [{"key": MODE_KEY, "value": json.dumps(mode, sort_keys=True)}])


def save_profile(ticker: str, cik, profile: dict, cache_key_value: str,
                 model: str, prompt_version: str = PROMPT_VERSION,
                 status: str = "ok", attempts: int = 0) -> None:
    t = ticker.upper()
    rows = [{"ticker": t, "field": f,
             "value": json.dumps(profile.get(f) or [], ensure_ascii=False),
             "value_num": None} for f in config.PROFILE_FIELDS]

    # One transaction: a profile row is never visible without its attributes.
    # relatedness_attributes is "replace" grouped by ticker, so the write
    # clears the ticker's previous fields itself.
    def _write(tx):
        tx.write_rows("relatedness_profiles", [{
            "ticker": t, "cik": str(cik or ""), "model": model,
            "prompt_version": prompt_version, "fetched_at": time.time(),
            "cache_key": cache_key_value, "status": status,
            "attempts": attempts}])
        tx.write_rows("relatedness_attributes", rows)

    rawstore.atomic(_write)


def load_profiles(tickers: list[str] | None = None) -> dict[str, dict]:
    """``{ticker: {field: [str, ...]}}`` from the durable tables."""
    want = {t.upper() for t in tickers} if tickers else None
    attr: dict[str, dict] = {}
    for r in rawstore.get_all("relatedness_attributes"):
        t = r["ticker"]
        if want is not None and t not in want:
            continue
        v = r["value"]
        if isinstance(v, list):      # rawstore auto-decodes JSON strings on read
            vals = v
        elif isinstance(v, str) and v:
            try:
                vals = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                vals = []
        else:
            vals = []
        attr.setdefault(t, {})[r["field"]] = vals
    out: dict[str, dict] = {}
    for r in rawstore.get_all("relatedness_profiles"):
        t = r["ticker"]
        if want is not None and t not in want:
            continue
        out[t] = {f: (attr.get(t, {}).get(f) or []) for f in config.PROFILE_FIELDS}
    return out


def profile_meta(tickers: list[str] | None = None) -> dict[str, dict]:
    """``{ticker: {status, attempts, cache_key}}`` for stored profile rows."""
    want = {t.upper() for t in tickers} if tickers else None
    return {r["ticker"]: {"status": r.get("status") or "ok",
                          "attempts": int(r.get("attempts") or 0),
                          "cache_key": r.get("cache_key") or ""}
            for r in rawstore.get_all("relatedness_profiles")
            if want is None or r["ticker"] in want}


def _key_mode(cache_key_value) -> dict | None:
    if isinstance(cache_key_value, dict):      # rawstore auto-decodes JSON text
        return cache_key_value
    try:
        k = json.loads(cache_key_value)
    except (json.JSONDecodeError, TypeError):
        return None
    return k if isinstance(k, dict) else None


def input_hash(cache_key_value) -> str | None:
    """The ``input_hash`` a stored profile was made from (``prompt_hash`` of its exact prompt), or ``None``.

    ``build_profiles`` compares it with the hash of today's prompt: a new filing text or company name redoes the
    profile (#388 D5).
    """
    k = _key_mode(cache_key_value or "")
    return (k or {}).get("input_hash") or None


def is_stale(meta: dict, mode: dict) -> bool:
    """True when a stored profile was made under a different mode than ``mode``.

    The prompt version decides for every writer: a v1 profile is stale whoever wrote it and whoever runs. A DeepSeek
    profile (no ``provider`` in its key) must also match the mode's model, thinking and effort, as before. A profile
    of an alternative writer (``ALT_WRITERS``: Grok, #388) at the current prompt version is current: its model is
    Grok's (and ``RC_GROK_MODEL`` may differ between the shell that ran Grok and the scheduled DeepSeek task), so a
    model check would make the nightly DeepSeek run redo every Grok profile.
    """
    k = _key_mode(meta.get("cache_key") or "")
    if k is None:
        return True
    if k.get("prompt_version") != mode.get("prompt_version"):
        return True
    if k.get("provider") in ALT_WRITERS:
        return False
    return (k.get("model") != (mode.get("model") or "")
            or bool(k.get("thinking")) != bool(mode.get("thinking"))
            or k.get("effort") != (mode.get("effort") or "")
            or k.get("prompt_version") != mode.get("prompt_version"))


def stale_tickers(tickers: list[str] | None = None) -> set[str]:
    """Tickers whose non-empty profile predates the current mode (kept, badged)."""
    mode = mode_stamp()
    out = set()
    for t, meta in profile_meta(tickers).items():
        if meta["status"] == "ok" and is_stale(meta, mode):
            out.add(t)
    return out


def score_rows(peers: dict[str, list[dict]]) -> list[dict]:
    """The ``relatedness_scores`` rows of ``{ticker: [{peer, score}, ...]}`` (its result history,
    ``relatedness_scores_history``, is their mirror)."""
    return [{"ticker": t, "peer": p["peer"], "score": p["score"]}
            for t, scored in peers.items() for p in scored]


def save_scores(peers: dict[str, list[dict]]) -> int:
    """Persist ``{ticker: [{peer, score}, ...]}`` to ``relatedness_scores``."""
    rows = score_rows(peers)
    if not rows:
        # A run that scored nothing (cold cache, peak-hours stop) must not wipe the last good
        # Fetch: replace_all would delete every stored score and write none (#284).
        logger.warning("relatedness_scores: the run produced no peer scores; kept the %d stored rows",
                       rawstore.count("relatedness_scores"))
        return 0
    rawstore.replace_all("relatedness_scores", rows)
    return len(rows)
