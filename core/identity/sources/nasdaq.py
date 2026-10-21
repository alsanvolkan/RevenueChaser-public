"""Nasdaq Trader symbol directory: the ETF flag and exchange per listed ticker.

Two pipe-delimited files, ``nasdaqlisted.txt`` and ``otherlisted.txt``. Base URL:
``RC_NASDAQTRADER_BASE`` (default ``https://www.nasdaqtrader.com/dynamic/SymDir``). With the
default, the ftp host is tried when www fails (R1 needed that); an explicit override is used
alone. Test issues and the trailer line are dropped; tickers are normalised (``BRK.B`` ->
``BRK-B``).

Ported from the Identity R1 research script (issue #84).
"""
from __future__ import annotations

import csv
import io

import httpx

from . import SourceResult, base_url, fail, norm_ticker, now_iso

SOURCE = "nasdaq"
_DEFAULT = "https://www.nasdaqtrader.com/dynamic/SymDir"
_FALLBACK = "https://ftp.nasdaqtrader.com/dynamic/SymDir"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _fetch(filename: str) -> str:
    bases = [base_url("NASDAQTRADER", _DEFAULT)]
    if bases[0] == _DEFAULT:
        bases.append(_FALLBACK)
    last = "no host answered"
    for base in bases:
        try:
            r = httpx.get(f"{base}/{filename}", headers=_HEADERS, timeout=60)
        except httpx.HTTPError as exc:
            last = type(exc).__name__
            continue
        if r.status_code == 200 and "|" in r.text[:300]:
            return r.text
        last = f"HTTP {r.status_code}" if r.status_code != 200 else "not a symbol directory"
    raise RuntimeError(f"Nasdaq Trader {filename}: {last}")


def _parse(text: str, symbol_col: str, exchange_col: str | None) -> list[dict]:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("File Creation Time")]
    rows = []
    for rec in csv.DictReader(io.StringIO("\n".join(lines)), delimiter="|"):
        if (rec.get("Test Issue") or "").strip() == "Y":
            continue
        ticker = norm_ticker(rec.get(symbol_col))
        if not ticker:
            continue
        rows.append({"ticker": ticker, "security_name": (rec.get("Security Name") or "").strip(),
                     "is_etf": (rec.get("ETF") or "").strip() == "Y",
                     "exchange": (rec.get(exchange_col) or "").strip() if exchange_col else "Q"})
    return rows


def symbol_directory() -> SourceResult:
    """One row per listed ticker: ``ticker``, ``security_name``, ``is_etf``, ``exchange``.

    Either file failing makes the whole result an error: half a directory would silently
    flag half the ETFs as operating companies.
    """
    try:
        rows = (_parse(_fetch("nasdaqlisted.txt"), "Symbol", None)
                + _parse(_fetch("otherlisted.txt"), "ACT Symbol", "Exchange"))
        unique = {r["ticker"]: r for r in reversed(rows)}       # first listing wins
        if not unique:
            return fail(SOURCE, "symbol directory came back empty")
        return SourceResult(SOURCE, "ok", list(unique.values())[::-1], None, now_iso())
    except Exception as exc:  # noqa: BLE001 - adapters never raise
        return fail(SOURCE, exc)
