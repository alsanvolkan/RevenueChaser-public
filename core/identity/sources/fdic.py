"""FDIC BankFind adapter: match non-SEC bank names to FDIC institutions.

For each ``(ticker, name)`` it tries several query variants (R1 found that one exact query
misses many banks): the stripped name, the holding-company field, the name with spaces removed
("Towne Bank" is "TowneBank" at the FDIC), a token wildcard, and the full unstripped name.
Candidates are accepted on name similarity >= 0.6 (:mod:`core.identity.names`); ties go to the
bigger bank. Base URL: ``RC_FDIC_BASE`` (default ``https://api.fdic.gov``).

Ported from the Identity R1 research script (issue #84).
"""
from __future__ import annotations

import re
import time
from typing import Callable, Mapping

import httpx

from .. import names
from . import SourceResult, base_url, now_iso, redact

SOURCE = "fdic"
MIN_SCORE = 0.6
PACE_S = 0.3
_FIELDS = "NAME,CERT,NAMEHCR,RSSDHCR,CITY,STALP,ASSET,BKCLASS,SPECGRPN"
_STRIP = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|holdings?|n\.?a\.?|ltd|the|"
                    r"class [a-c]|common stock|financial|bancorp|bancshares|bancorporation|group)\b\.?",
                    re.I)


def _queries(name: str) -> list[str]:
    core = " ".join(re.sub(r"[,.]", " ", _STRIP.sub(" ", name or "")).split())
    queries: list[str] = []
    if core:
        queries += [f'NAME:"{core}" AND ACTIVE:1', f'NAMEHCR:"{core.upper()}*" AND ACTIVE:1']
        toks = [t for t in (re.sub(r"[^A-Za-z0-9]", "", x) for x in core.split()) if t]
        if len(toks) > 1:
            queries.append(f'NAME:"{"".join(toks)}" AND ACTIVE:1')
        if toks:
            queries.append("NAME:" + "*".join(toks) + "* AND ACTIVE:1")
    full = " ".join(re.sub(r"[,.]", " ", name or "").split())
    if full and full != core:
        queries.append(f'NAME:"{full}" AND ACTIVE:1')
    return queries


def _lookup(ticker: str, name: str, url: str, sleep: Callable[[float], None]) -> dict:
    hits: list[dict] = []
    errors: list[str] = []
    for q in _queries(name):
        sleep(PACE_S)
        try:
            r = httpx.get(url, params={"filters": q, "fields": _FIELDS, "limit": 25}, timeout=60)
            if r.status_code == 200:
                hits += [d["data"] for d in r.json().get("data", []) if d.get("data")]
            else:
                errors.append(f"HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
    best = None
    for h in hits:
        score = max(names.name_score(name, h.get("NAME")) or 0, names.name_score(name, h.get("NAMEHCR")) or 0)
        if score >= MIN_SCORE and (best is None or (score, h.get("ASSET") or 0) > (best[0], best[1].get("ASSET") or 0)):
            best = (score, h)
    if best:
        score, h = best
        return {"ticker": ticker, "status": "ok", "cert": h.get("CERT"), "name": h.get("NAME"),
                "holding_company": h.get("NAMEHCR") or "", "score": score,
                "city": h.get("CITY"), "state": h.get("STALP"), "assets_k": h.get("ASSET")}
    if errors and not hits:
        return {"ticker": ticker, "status": "error", "error": redact(errors[0])[:160]}
    return {"ticker": ticker, "status": "no_match", "cert": None, "name": None, "holding_company": None,
            "score": None}


def match_banks(names_by_ticker: Mapping[str, str],
                sleep: Callable[[float], None] = time.sleep) -> SourceResult:
    """One row per ticker: ``cert``, ``name``, ``holding_company``, ``score`` and a ``status``.

    Row status ``ok`` (an institution matched), ``no_match``, or ``error`` (every query failed;
    never cached). The result is ``error`` only when every ticker errored.
    """
    if not names_by_ticker:
        return SourceResult(SOURCE, "ok", [], None, now_iso())
    url = f"{base_url('FDIC', 'https://api.fdic.gov')}/banks/institutions"
    rows = [_lookup(t, n, url, sleep) for t, n in names_by_ticker.items()]
    failed = sum(1 for r in rows if r["status"] == "error")
    if failed == len(rows):
        return SourceResult(SOURCE, "error", rows, f"{failed} of {len(rows)} lookups failed: {rows[0]['error']}",
                            now_iso())
    note = f"{failed} of {len(rows)} lookups failed" if failed else None
    return SourceResult(SOURCE, "ok", rows, note, now_iso())
