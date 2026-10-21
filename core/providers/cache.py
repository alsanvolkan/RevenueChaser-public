"""JSON cache + TTL — per-ticker raw payloads in .store/raw.duckdb (ticker_payload_cache).

Per-ticker raw payloads live in the single DuckDB raw store, keyed by ticker,
with a daily TTL and rate-limit protection. ``load(ignore_ttl=True)`` (the
``no_fetch`` model-only mode) accepts stale cache without touching the network.

A payload with neither ``annual`` nor ``info`` is what ``yfinance.get_financials`` returns
when Yahoo fails, and one with ``info`` but no ``annual`` cannot replace a stored ``annual``:
``save`` refuses both, so a bad day never overwrites good data and never starts a fresh TTL. The one exception is a
flagged ``info_only`` payload (``yfinance.get_info``; an empty one is refused above): over a stored full row it refreshes
the row's ``.info`` part and ``_info_fetched_at`` and keeps the years and their ``_fetched_at``.
``get_or_fetch`` then returns the previous payload (any age) flagged ``_stale``. The flag is
a return-value marker only; it is never written to the store: ``save`` refuses a payload that
carries it, so a stale payload saved back cannot restart the TTL.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from .. import rawstore
from ..config import CACHE_TTL_DAYS

_TTL_SECONDS = CACHE_TTL_DAYS * 24 * 3600


def _now_ts() -> float:
    return time.time()


def _usable(row: dict | None, ignore_ttl: bool, allow_info_only: bool) -> dict | None:
    """The payload of a stored row when ``load`` would hand it out, else None."""
    if not row:
        return None
    data = row["payload"]
    if not isinstance(data, dict):
        return None
    if data.get("info_only") and not allow_info_only:
        return None
    fetched_at = data.get("_fetched_at", 0) or row.get("fetched_at") or 0
    if allow_info_only:  # AG reads the ``.info`` part, which a later info-only fetch may have refreshed
        fetched_at = max(fetched_at, data.get("_info_fetched_at", 0) or 0)
    if not ignore_ttl and _now_ts() - fetched_at > _TTL_SECONDS:
        return None
    return data


def load(ticker: str, ignore_ttl: bool = False, allow_info_only: bool = False) -> dict | None:
    """TTL içinde geçerli cache varsa döndürür, yoksa None.

    ``ignore_ttl=True`` — model-only (no-fetch) modda eskimiş cache'i de kabul eder.
    An ``info_only`` payload (``yfinance.get_info``, no statements) is a hit only for a caller that
    passes ``allow_info_only=True``; everyone else sees a miss and fetches the full payload (#150 U13).
    """
    return _usable(rawstore.get("ticker_payload_cache", ticker.upper()), ignore_ttl, allow_info_only)


def load_many(tickers, ignore_ttl: bool = False, allow_info_only: bool = False) -> dict[str, dict]:
    """``{ticker: payload}`` for the tickers ``load`` would hit, from one store query (#150 U13)."""
    rows = rawstore.get_many("ticker_payload_cache", [t.upper() for t in tickers])
    out = {}
    for ticker, row in rows.items():
        data = _usable(row, ignore_ttl, allow_info_only)
        if data is not None:
            out[ticker] = data
    return out


def _is_empty(data: dict | None) -> bool:
    """No annual financials and no info: nothing a board could show."""
    data = data or {}
    return not data.get("annual") and not data.get("info")


def _would_discard_annual(ticker: str, data: dict) -> bool:
    """True when ``data`` has no ``annual`` but the stored payload for ``ticker`` does."""
    if data.get("annual"):
        return False
    row = rawstore.get("ticker_payload_cache", ticker.upper())
    stored = row.get("payload") if row else None
    return isinstance(stored, dict) and bool(stored.get("annual"))


_STATEMENT_KEYS = {"annual", "info_only", "_fetched_at", "_fetched_at_iso", "_info_fetched_at"}


def _merge_info(stored: dict, data: dict) -> dict:
    """The stored full payload with the ``.info`` part of an info-only fetch laid over it (#150 review M1).

    The statements and their ``_fetched_at`` stay as they are (Financials' TTL logic does not change); the
    info fetch gets its own ``_info_fetched_at``, which only a reader that asks for info-only rows counts.
    """
    merged = {**stored, **{k: v for k, v in data.items() if k not in _STATEMENT_KEYS}}
    merged["_info_fetched_at"] = _now_ts()
    return merged


def _stored_full(ticker: str) -> dict | None:
    row = rawstore.get("ticker_payload_cache", ticker.upper())
    stored = row.get("payload") if row else None
    return stored if isinstance(stored, dict) and stored.get("annual") else None


def save(ticker: str, data: dict) -> bool:
    """Cache'e yazar; fetched-at damgası ekler. ``False`` = yazılmadı.

    Not written: an empty payload (no ``annual`` and no ``info``), a payload without
    ``annual`` over a stored one that has it (an info-only fetch must not wipe the years), and a
    payload carrying ``_stale`` (``get_or_fetch`` returns an old payload with that marker; saving it
    back would stamp it with a new ``_fetched_at`` and restart the TTL on data that is not fresh).
    """
    if data.get("_stale") or _is_empty(data):
        return False
    if data.get("info_only") and (stored := _stored_full(ticker)) is not None:
        # An info-only fetch over a stored full row refreshes the ``.info`` part and keeps the years (M1).
        rawstore.write_rows("ticker_payload_cache", [{
            "ticker": ticker.upper(), "payload": _merge_info(stored, data),
            "fetched_at": stored.get("_fetched_at") or 0, "fetched_at_iso": stored.get("_fetched_at_iso") or ""}])
        return True
    if _would_discard_annual(ticker, data):
        return False
    payload = dict(data)
    payload["_fetched_at"] = _now_ts()
    payload["_fetched_at_iso"] = datetime.now(timezone.utc).isoformat()
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": ticker.upper(),
        "payload": payload,
        "fetched_at": payload["_fetched_at"],
        "fetched_at_iso": payload["_fetched_at_iso"],
    }])
    return True


def get_or_fetch(ticker: str, fetcher: Callable[[str], dict], use_cache: bool = True) -> dict:
    """Cache'ten okur, TTL dolmuşsa veya yoksa ``fetcher`` ile çeker ve kaydeder.

    When the fetch comes back unusable (empty, or without the ``annual`` already stored) the
    previous payload of any age is returned with ``_stale: True``; with nothing stored the
    fetched payload is returned as it is, unsaved.
    """
    if use_cache:
        cached = load(ticker)
        if cached is not None:
            return cached
    data = fetcher(ticker)
    if data and save(ticker, data):
        return data
    previous = load(ticker, ignore_ttl=True)
    if previous is not None and not _is_empty(previous):
        return {**previous, "_stale": True}
    return data
