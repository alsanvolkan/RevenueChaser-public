"""Alpaca market data (``data.alpaca.markets``): the key headers, the pacer and the paged GET the prices store uses.

Two credentials, read by name through ``core/credentials.py`` and sent as headers, never in a URL and never logged:
``ALPACA_API_KEY_ID`` as ``APCA-API-KEY-ID`` and ``ALPACA_API_SECRET_KEY`` as ``APCA-API-SECRET-KEY``. Without both a
run fails before it sends anything.

Endpoints (Alpaca's API reference, read 2026-10-28; **not run against the real source from here**: a cloud thread has
no key, and the tests answer at the HTTP boundary):

- ``GET /v2/stocks/bars``: ``symbols`` (comma list), ``timeframe=1Day``, ``start`` / ``end`` (RFC-3339),
  ``adjustment`` (``raw``, ``split``, ``dividend``, ``spin-off``, ``all``), ``feed`` (``sip`` default), ``limit``
  (1-10,000 data points in total, not per symbol), ``page_token``, ``sort``. Answer ``{"bars": {"<SYM>": [{"t",
  "o", "h", "l", "c", "v", "n", "vw"}]}, "next_page_token"}``.
- ``GET /v1/corporate-actions``: ``symbols``, ``types``, ``start`` / ``end`` (``YYYY-MM-DD``, inclusive, on
  ``process_date``), ``limit`` (1-1,000), ``page_token``. Answer ``{"corporate_actions": {"forward_splits": [...],
  "reverse_splits": [...], "cash_dividends": [...], ...}, "next_page_token"}``.

The free plan allows 200 requests a minute (``PER_MINUTE`` stays under it) and no SIP data of the last 15 minutes, so
a query's ``end`` is never later than 16 minutes ago. ``RC_ALPACA_BASE`` redirects the host (read at call time).
"""
from __future__ import annotations

import os
from typing import Any

from ....core import credentials
from .. import base

PROVIDER = "alpaca"
DEFAULT_BASE = "https://data.alpaca.markets"
PER_MINUTE = 190
# A paged answer that never ends is cut here rather than followed forever: 100 pages of 10,000 bars is a million
# bars, far past one chunk of 100 symbols over a few sessions.
MAX_PAGES = 100


def base_url() -> str:
    return (os.getenv("RC_ALPACA_BASE") or DEFAULT_BASE).strip().rstrip("/")


def headers() -> dict[str, str]:
    """The two key headers; ``SourceError`` naming the missing variable(s), never a value, when one is unset."""
    key_id, secret = credentials.key_for("alpaca"), credentials.key_for("alpaca_secret")
    missing = [credentials.get(s).env_var for s, v in (("alpaca", key_id), ("alpaca_secret", secret)) if not v]
    if missing:
        raise base.SourceError(f"no Alpaca key: set {' and '.join(missing)} (core/credentials.py)")
    return {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}


def pacer(ctx: base.RunContext) -> base.Pacer:
    return base.Pacer(PER_MINUTE, clock=ctx.clock, sleep=ctx.sleep)


def get_pages(ctx: base.RunContext, path: str, params: dict, pace: base.Pacer, key: str) -> list[Any]:
    """Every page of ``path`` (``next_page_token`` followed), each page's ``key`` value in order. Every page is a
    call against the day cap, counted by ``base.http_get``. ``SourceError`` for an answer that is not a JSON object
    holding ``key``, or for ``MAX_PAGES`` pages without an end."""
    auth = headers()
    out, token = [], None
    for _ in range(MAX_PAGES):
        pace.wait()
        r = base.http_get(ctx, PROVIDER, base_url() + path,
                          params={**params, **({"page_token": token} if token else {})}, headers=auth, keyed=True)
        try:
            body = r.json()
        except ValueError as e:
            raise base.SourceError(f"alpaca {path}: the answer is not JSON") from e
        if not isinstance(body, dict) or key not in body:
            raise base.SourceError(f"alpaca {path}: no {key!r} in the answer")
        out.append(body.get(key))
        token = body.get("next_page_token")
        if not token:
            return out
    raise base.SourceError(f"alpaca {path}: {MAX_PAGES} pages without an end; not stored")
