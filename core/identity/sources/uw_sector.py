"""Unusual Whales sector adapter: sector, industry, issue type and next earnings date per ticker.

``/screener/stocks`` ignores its ``page`` parameter (R1 verified that page 1 and 3 return the
same 500 rows), so the universe is pulled in market-cap bands and any band that comes back
full (500 rows) is split in half until it fits. The client is
:class:`core.providers.unusual_whales.UWClient` (base URL ``RC_UW_BASE``, retry and backoff
included). Without a key the source is ``skipped``.

Ported from the Identity R1 research script (issue #84). The sector is UW's own Morningstar name, stored as
UW gives it (#265); ``next_earnings_date`` (UW's date text) travels in the cache payload to the apply.
"""
from __future__ import annotations

import math
import time
from typing import Callable

from ... import credentials
from ...providers.unusual_whales import UWClient
from . import SourceResult, fail, now_iso, redact

SOURCE = "uw_sector"
LIMIT = 500
MAX_DEPTH = 12
MIN_WIDTH = 1e6
PACE_S = 0.6
EDGES = [0, 5e7, 1e8, 2e8, 3e8, 4e8, 6e8, 1e9, 1.5e9, 2e9, 3e9, 5e9, 8e9, 1.5e10, 3e10, 1e11, 1e14]


def _cap(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _date(value) -> str | None:
    """UW's date text (``2026-11-19``), else ``None`` for a blank or missing one."""
    text = str(value).strip() if value is not None else ""
    return text or None


def sectors(client=None, sleep: Callable[[float], None] = time.sleep) -> SourceResult:
    """One row per ticker: ``sector``, ``industry``, ``security_type`` (UW issue type), ``market_cap``,
    ``next_earnings_date``.

    ``client`` is injectable for tests. A failed request stops the run as ``error`` and keeps
    the rows gathered so far. A band that is still full after the maximum number of splits is
    named in ``error`` on an otherwise ``ok`` result, so truncation is never silent.
    """
    try:
        client = client or UWClient()
        if not client.active:
            return SourceResult(SOURCE, "skipped", [], credentials.missing_message("unusual_whales"), now_iso())
        rows: dict[str, dict] = {}
        state = {"stopped": None, "truncated": []}

        def band(lo: float, hi: float, depth: int = 0) -> None:
            if state["stopped"]:
                return
            sleep(PACE_S)
            res = client.get_result("/screener/stocks", {
                "min_marketcap": str(int(lo)), "max_marketcap": str(int(hi)), "limit": LIMIT})
            if not res.ok:
                detail = (res.error or {}).get("error") or "request failed"
                state["stopped"] = f"UW screener band {int(lo)}-{int(hi)}: {detail}"
                return
            data = res.data.get("data", []) if isinstance(res.data, dict) else (res.data or [])
            if len(data) >= LIMIT:
                if depth < MAX_DEPTH and hi - lo > MIN_WIDTH:
                    mid = (lo + hi) / 2 if lo <= 0 else math.sqrt(lo * hi)
                    band(lo, mid, depth + 1)
                    band(mid, hi, depth + 1)
                    return
                state["truncated"].append([int(lo), int(hi)])
            for d in data:
                t = d.get("ticker")
                if t and t not in rows:
                    rows[t] = {"ticker": t, "sector": d.get("sector"), "industry": d.get("industry_type"),
                               "security_type": d.get("issue_type"), "market_cap": _cap(d.get("marketcap")),
                               "next_earnings_date": _date(d.get("next_earnings_date"))}

        for lo, hi in zip(EDGES[:-1], EDGES[1:]):
            band(lo, hi)
        out = list(rows.values())
        if state["stopped"]:
            return SourceResult(SOURCE, "error", out, redact(state["stopped"]), now_iso())
        note = f"truncated bands (still full at {LIMIT} rows): {state['truncated']}" if state["truncated"] else None
        return SourceResult(SOURCE, "ok", out, note, now_iso())
    except Exception as exc:  # noqa: BLE001 - adapters never raise
        return fail(SOURCE, exc)
