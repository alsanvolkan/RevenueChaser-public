"""OpenFIGI adapter: security type per ticker (``POST /v3/mapping``).

Works without a key (<= 10 jobs per request, 25 requests a minute) and faster with
``OPENFIGI_API_KEY`` (<= 100 jobs per request, 25 requests per 6 s). The key is read through
:mod:`core.credentials` and sent only in the ``X-OPENFIGI-APIKEY`` header. Base URL:
``RC_OPENFIGI_BASE`` (default ``https://api.openfigi.com``).

Share classes: OpenFIGI spells ``BRK-B`` as ``BRK/B``; the request uses the slash and the row
is mapped back to the app's spelling. Units and warrants have their own suffixes there: ``AAC-UN``
is ``AAC/U`` and ``BESS-WT`` is ``BESS/WS`` (measured on the 546 unmatched tickers, 2026-10-01:
units 29/30, warrants 10/10). Preferreds (``WFC-PA``, ``ZIONP``) matched under none of seven
spellings tried; :mod:`core.identity.types` types them from their Nasdaq names instead.

Ported from the Identity R1 research script (issue #84).
"""
from __future__ import annotations

import time
from typing import Callable, Iterable

import httpx

from ... import credentials
from . import SourceResult, base_url, fail, now_iso, redact

SOURCE = "openfigi"
BATCH_KEYED = 100
BATCH_KEYLESS = 10
_GAP_KEYED = 0.3        # 25 requests / 6 s
_GAP_KEYLESS = 2.5      # 25 requests / minute
_MAX_ATTEMPTS = 8
_LOW_REMAINING = 5


# our suffix -> OpenFIGI's; only the two measured (the universe has no other unit/warrant form)
_SUFFIXES = (("-UN", "/U"), ("-WT", "/WS"))


def figi_symbol(ticker: str) -> str:
    """The app's ticker in OpenFIGI's spelling."""
    for ours, theirs in _SUFFIXES:
        if ticker.endswith(ours):
            return ticker[: -len(ours)] + theirs
    return ticker.replace("-", "/")


def _pick(ticker: str, resp: dict) -> dict | None:
    """The mapping entry for ``ticker``: the US listing with that exact ticker, else the first."""
    data = (resp or {}).get("data") or []
    if not data:
        return None
    want = figi_symbol(ticker)
    for d in data:
        if d.get("ticker") == want and d.get("exchCode") == "US":
            return d
    return data[0]


def _wait_seconds(resp) -> int:
    try:
        return int(float(resp.headers.get("ratelimit-reset", "10") or 10)) + 1
    except (TypeError, ValueError):
        return 11


def security_types(tickers: Iterable[str], sleep: Callable[[float], None] = time.sleep) -> SourceResult:
    """One row per ticker: ``security_type``, ``security_type2``, ``market_sector``, ``name``, ``figi``, and
    (#267) ``composite_figi``, ``share_class_figi``, ``exch_code``, ``security_description``.

    A ticker OpenFIGI does not know is a ``no_match`` row. A failed request ends the run as an
    ``error`` result holding the rows collected so far.
    """
    todo = sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
    if not todo:
        return SourceResult(SOURCE, "ok", [], None, now_iso())
    key = credentials.key_for("openfigi")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-OPENFIGI-APIKEY"] = key
    batch = BATCH_KEYED if key else BATCH_KEYLESS
    gap = _GAP_KEYED if key else _GAP_KEYLESS
    url = f"{base_url('OPENFIGI', 'https://api.openfigi.com')}/v3/mapping"
    rows: list[dict] = []
    try:
        for n, i in enumerate(range(0, len(todo), batch)):
            chunk = todo[i:i + batch]
            if n:
                sleep(gap)
            jobs = [{"idType": "TICKER", "idValue": figi_symbol(t), "exchCode": "US"} for t in chunk]
            resp = None
            for _attempt in range(_MAX_ATTEMPTS):
                resp = httpx.post(url, json=jobs, headers=headers, timeout=90)
                if resp.status_code != 429:
                    break
                sleep(_wait_seconds(resp))
            if resp.status_code != 200:
                return _partial(rows, f"OpenFIGI HTTP {resp.status_code}")
            body = resp.json()
            if not isinstance(body, list) or len(body) != len(chunk):
                return _partial(rows, "OpenFIGI returned an unexpected body")
            for t, entry in zip(chunk, body):
                hit = _pick(t, entry)
                if hit is None:
                    rows.append({"ticker": t, "status": "no_match", "security_type": None,
                                 "security_type2": None, "market_sector": None, "name": None})
                else:
                    rows.append({"ticker": t, "status": "ok",
                                 "security_type": hit.get("securityType"),
                                 "security_type2": hit.get("securityType2"),
                                 "market_sector": hit.get("marketSector"),
                                 "name": hit.get("name"), "figi": hit.get("figi"),
                                 "composite_figi": hit.get("compositeFIGI"),
                                 "share_class_figi": hit.get("shareClassFIGI"),
                                 "exch_code": hit.get("exchCode"),
                                 "security_description": hit.get("securityDescription")})
            remaining = resp.headers.get("ratelimit-remaining")
            if remaining is not None and str(remaining).isdigit() and int(remaining) < _LOW_REMAINING:
                sleep(_wait_seconds(resp))
    except Exception as exc:  # noqa: BLE001 - adapters never raise
        result = fail(SOURCE, exc)
        result.rows = rows
        return result
    return SourceResult(SOURCE, "ok", rows, None, now_iso())


def _partial(rows: list[dict], message: str) -> SourceResult:
    return SourceResult(SOURCE, "error", rows, redact(message), now_iso())
