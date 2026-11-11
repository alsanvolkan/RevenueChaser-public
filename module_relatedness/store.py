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


# The filing a profile was read from (build plan PR 20, G6): the company, the accession, the form and the day filed.
SOURCE_KEYS = ("cik", "accession", "form", "filed")


def cache_key(model, thinking, effort, prompt_version, ticker, input_hash, provider=None, source=None) -> str:
    """Structured (sorted-keys) cache key — stable across dict ordering.

    ``provider`` names a writer other than DeepSeek (``"grok"``, #388); a DeepSeek key carries none, so its bytes
    are what they were before Grok. ``source`` (G6) is the filing the prompt's text came from (``SOURCE_KEYS``);
    a key made without one carries none. Staleness never reads it (``is_stale``): the input hash already moves
    with the text.
    """
    key = {
        "model": model or "", "thinking": int(bool(thinking)), "effort": effort or "",
        "prompt_version": prompt_version, "ticker": (ticker or "").upper(),
        "input_hash": input_hash,
    }
    if provider and provider != "deepseek":
        key["provider"] = provider
    if source:
        key["source"] = {k: (None if source.get(k) is None else str(source[k])) for k in SOURCE_KEYS}
    return json.dumps(key, sort_keys=True, separators=(",", ":"))


def profile_source(cache_key_value) -> dict | None:
    """The filing a stored profile was read from (``cache_key``'s ``source``), or None for a profile made before
    G6 or from no filing."""
    src = (_key_mode(cache_key_value or "") or {}).get("source")
    return dict(src) if isinstance(src, dict) else None


def mode_stamp(writer: tuple[str, str] | None = None) -> dict:
    """The mode a current profile matches. ``writer`` is ``(provider, model)`` (#360; default DeepSeek and its
    ``MODEL``): a DeepSeek mode is the stamp it always was; another writer's names its provider and sends no
    thinking knob."""
    from RevenueChaser.core.providers import deepseek
    provider, model = writer or ("deepseek", deepseek.MODEL)
    if provider == "deepseek":
        # v3 (#400) sends no thinking: a DeepSeek stamp names neither knob, as every other writer's.
        return {"model": model, "thinking": False, "effort": "", "prompt_version": PROMPT_VERSION}
    return {"provider": provider, "model": model, "thinking": False, "effort": "", "prompt_version": PROMPT_VERSION}


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


# ``save_profile``'s default for ``profile_json`` / ``label_source``: carry the stored value (#400).
KEEP = object()
JSON_COLUMNS = ("profile_json", "label_source")


def _stored_json(tx, ticker: str) -> dict:
    """The stored ``profile_json`` / ``label_source`` of ``ticker`` (decoded), read inside ``tx``."""
    try:
        row = tx.execute("SELECT profile_json, label_source FROM relatedness_profiles WHERE ticker = ?",
                         [ticker]).fetchone()
    except Exception:  # noqa: BLE001 - no table yet: nothing to carry
        return {c: None for c in JSON_COLUMNS}
    out = {}
    for col, v in zip(JSON_COLUMNS, row or (None, None)):
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                v = None
        out[col] = v
    return out


def save_profile(ticker: str, cik, profile: dict, cache_key_value: str,
                 model: str, prompt_version: str = PROMPT_VERSION,
                 status: str = "ok", attempts: int = 0, provider: str = "deepseek",
                 profile_json=KEEP, label_source=KEEP) -> None:
    """Write one ticker's profile row and its attribute rows in one transaction.

    ``profile_json`` / ``label_source`` (schema 13, #401) are the v3 structured profile and where each field came
    from. Left out (``KEEP``), the stored values are carried: ``relatedness_profiles`` is an upsert that sets every
    column from the row, so a re-save that named neither column used to set both to NULL (#400). Pass ``None`` to
    clear them.
    """
    t = ticker.upper()
    rows = [{"ticker": t, "field": f,
             "value": json.dumps(profile.get(f) or [], ensure_ascii=False),
             "value_num": None} for f in config.PROFILE_FIELDS]

    # One transaction: a profile row is never visible without its attributes.
    # relatedness_attributes is "replace" grouped by ticker, so the write
    # clears the ticker's previous fields itself.
    def _write(tx):
        given = {"profile_json": profile_json, "label_source": label_source}
        if any(v is KEEP for v in given.values()):
            kept = _stored_json(tx, t)
            given = {c: (kept[c] if v is KEEP else v) for c, v in given.items()}
        tx.write_rows("relatedness_profiles", [{
            "ticker": t, "cik": str(cik or ""), "model": model,
            "prompt_version": prompt_version, "fetched_at": time.time(),
            "cache_key": cache_key_value, "status": status,
            "attempts": attempts, "provider": provider, **given}])
        tx.write_rows("relatedness_attributes", rows)

    rawstore.atomic(_write, db="relatedness")


def load_profiles(tickers: list[str] | None = None) -> dict[str, dict]:
    """``{ticker: {field: [str, ...]}}`` from the durable tables: every stored profile when ``tickers`` is None, none
    when it is empty (an empty list once read every profile, #642 PR 3)."""
    if tickers is not None and not tickers:
        return {}
    want = {t.upper() for t in tickers} if tickers is not None else None
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


def load_profile_json(tickers: list[str] | None = None) -> dict[str, dict]:
    """``{ticker: v3 profile}`` for the stored rows that carry one (#669; a v2 row has none)."""
    want = {t.upper() for t in tickers} if tickers else None
    out = {}
    for r in rawstore.get_all("relatedness_profiles", ["ticker", "profile_json"]):
        pj = r.get("profile_json")
        if isinstance(pj, str):
            try:
                pj = json.loads(pj)
            except (json.JSONDecodeError, TypeError):
                pj = None
        if isinstance(pj, dict) and (want is None or r["ticker"] in want):
            out[r["ticker"]] = pj
    return out


def load_vectors(model: str, tickers: list[str]) -> dict[str, bytes]:
    """``{ticker: vector blob}`` stored for ``model`` (``embeddings`` reads the blob's layout)."""
    want = set(tickers)
    return {r["ticker"]: bytes(r["vector"]) for r in rawstore.get_all("relatedness_embeddings")
            if r["model"] == model and r["ticker"] in want and r.get("vector") is not None}


def save_vectors(model: str, blobs: dict[str, bytes], dim: int) -> None:
    """Upsert one vector row per ticker (key ``(ticker, model)``): a re-embedded company replaces its row."""
    if blobs:
        rawstore.write_rows("relatedness_embeddings",
                            [{"ticker": t, "model": model, "dim": dim, "vector": b} for t, b in blobs.items()])


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
    """True when a stored profile was made under another prompt version than ``mode``'s (or its key is unreadable).

    Mixed writers (avo, 11 Nov 2026: "sadece ilk tur claude ile sonra deepseek e donebiliriz"): a profile stays
    current whoever wrote it, so a first round by Claude is not redone when the board's writer goes back to DeepSeek.
    This replaces #360's "one writer per board", under which another provider or model made a profile stale. A new
    filing text or company name still redoes a profile through its input hash (#388 D5), with the board's writer.
    """
    k = _key_mode(meta.get("cache_key") or "")
    return k is None or k.get("prompt_version") != mode.get("prompt_version")


def key_writer(cache_key_value) -> tuple[str, str] | None:
    """``(provider, model)`` a stored profile's key names (no provider = DeepSeek), or None for an unreadable key."""
    k = _key_mode(cache_key_value or "")
    return None if k is None else (k.get("provider") or "deepseek", k.get("model") or "")


def other_writer_tickers(tickers: list[str] | None, writer: tuple[str, str]) -> set[str]:
    """Tickers whose usable profile another writer made. Counted for the board (``other_writer``); since mixed
    writers (11 Nov 2026) they stay current and in the peer matrix."""
    return {t for t, meta in profile_meta(tickers).items()
            if meta["status"] == "ok" and key_writer(meta.get("cache_key")) not in (None, tuple(writer))}


def profile_writers(tickers: list[str] | None = None) -> dict[str, str]:
    """``{ticker: "provider/model"}`` of each usable stored profile (the board names the writer per profile)."""
    out = {}
    for t, meta in profile_meta(tickers).items():
        w = key_writer(meta.get("cache_key")) if meta["status"] == "ok" else None
        if w is not None:
            out[t] = f"{w[0]}/{w[1]}"
    return out


def stale_tickers(tickers: list[str] | None = None, writer: tuple[str, str] | None = None) -> set[str]:
    """Tickers whose non-empty profile predates the current mode of ``writer`` (kept, badged)."""
    mode = mode_stamp(writer)
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
        # Fetch: retiring every pair it did not score would delete every stored score (#284).
        logger.warning("relatedness_scores: the run produced no peer scores; kept the %d stored rows",
                       rawstore.count("relatedness_scores"))
        return 0
    retired = rawstore.sync_rows("relatedness_scores", rows)   # incremental, plan §10
    logger.info("relatedness_scores: %d pairs written, %d retired", len(rows), retired)
    return len(rows)
