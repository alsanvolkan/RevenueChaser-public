"""SEC adapters: the ticker/exchange map and per-CIK filing summaries.

Both go through :mod:`core.providers.sec_edgar` so they share its User-Agent, the
process-wide <= 10 req/s throttle (``www.sec.gov`` and ``data.sec.gov`` share the limit),
the 429/5xx backoff, and the ``RC_SEC_WWW_BASE`` / ``RC_SEC_DATA_BASE`` overrides.

``exchange_map`` is refreshed every run (it is what keeps new listings and delistings
current). ``submissions`` reduces each CIK's filing index to what the type rules need: the
set of form types, and whether the company closed out as a shell (8-K item 5.06).

Ported from the Identity R1 research script (issue #84).
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx

from ...providers import sec_edgar
from . import SourceResult, fail, norm_ticker, now_iso, redact

EXCHANGE_SOURCE = "sec_exchange"
FUND_SOURCE = "sec_mf"
SUBMISSIONS_SOURCE = "sec_submissions"

# Form prefixes that mark a registered fund; an issuer showing these needs its older filing
# pages checked for N-54A (the BDC election) before it can be classified.
FUNDISH = ("N-2", "N-CSR", "NPORT", "N-CEN", "497", "486", "N-54", "N-23C", "24F-2", "N-PX",
           "40-17", "N-8F")
PERIODIC = {"10-K", "10-Q", "10-K405", "10-KT", "20-F", "40-F", "6-K", "N-CSR", "N-CSRS", "NPORT-P"}
_SHELL_ITEM = "5.06"     # 8-K item 5.06: change in shell company status


def exchange_map(attempts: int | None = None, headers_out: dict | None = None) -> SourceResult:
    """``company_tickers_exchange.json``: one row per listed ticker (ticker, cik, name, exchange).

    An empty or malformed body is an error, never an empty ok: the SEC map is never empty,
    so an empty one means the fetch failed quietly. ``attempts`` caps the requests (see
    ``sec_edgar._request``); the nightly SEC-file step passes 1 (#301). ``headers_out`` receives the response
    headers (the step reports the file's ``Last-Modified``).
    """
    try:
        body = sec_edgar.get_json(f"{sec_edgar.SEC_WWW_BASE}/files/company_tickers_exchange.json",
                                  attempts=attempts, headers_out=headers_out)
        fields, data = body["fields"], body["data"]
        idx = {name: fields.index(name) for name in ("cik", "name", "ticker", "exchange")}
        rows: dict[str, dict] = {}
        for rec in data:
            t = norm_ticker(rec[idx["ticker"]])
            if t and t not in rows:
                rows[t] = {"ticker": t, "cik": str(int(rec[idx["cik"]])),
                           "name": rec[idx["name"]], "exchange": rec[idx["exchange"]]}
        if not rows:
            return fail(EXCHANGE_SOURCE, "SEC ticker map came back empty")
        return SourceResult(EXCHANGE_SOURCE, "ok", list(rows.values()), None, now_iso())
    except Exception as exc:  # noqa: BLE001 - adapters never raise
        return fail(EXCHANGE_SOURCE, exc)


def fund_map(attempts: int | None = None, headers_out: dict | None = None) -> SourceResult:
    """``company_tickers_mf.json`` (#301, D8): one row per fund share class (class_id, cik, series_id, symbol).

    Rows are keyed by classId; a row without one is dropped and a repeated one keeps its first row. An empty
    symbol is ``None``. An empty or malformed body is an error, as for ``exchange_map``; ``headers_out`` is too.
    """
    try:
        body = sec_edgar.get_json(f"{sec_edgar.SEC_WWW_BASE}/files/company_tickers_mf.json", attempts=attempts,
                                  headers_out=headers_out)
        fields, data = body["fields"], body["data"]
        idx = {name: fields.index(name) for name in ("cik", "seriesId", "classId", "symbol")}
        rows: dict[str, dict] = {}
        for rec in data:
            class_id = str(rec[idx["classId"]] or "").strip()
            if not class_id or class_id in rows:
                continue
            cik, series, symbol = rec[idx["cik"]], rec[idx["seriesId"]], rec[idx["symbol"]]
            rows[class_id] = {"class_id": class_id, "cik": str(int(cik)) if cik not in (None, "") else None,
                              "series_id": str(series).strip() or None if series is not None else None,
                              "symbol": str(symbol).strip() or None if symbol is not None else None}
        if not rows:
            return fail(FUND_SOURCE, "SEC fund file came back empty")
        return SourceResult(FUND_SOURCE, "ok", list(rows.values()), None, now_iso())
    except Exception as exc:  # noqa: BLE001 - adapters never raise
        return fail(FUND_SOURCE, exc)


def _block(block: dict) -> tuple[set[str], set[str]]:
    """(form set, 8-K item set) of one filings block (``recent`` or an older page)."""
    forms = block.get("form") or []
    items = block.get("items") or []
    eight_k = {i.strip() for f, it in zip(forms, items)
               if str(f).startswith("8-K") and it for i in str(it).split(",") if i.strip()}
    return {str(f) for f in forms}, eight_k


def _status_code(exc: Exception) -> int | None:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None)


def _one(cik: int, ticker: str | None) -> dict:
    """One CIK's summary row. Never raises: a failure is the row's ``status``."""
    row: dict = {"cik": str(cik), "status": "ok"}
    if ticker:
        row["ticker"] = ticker
    try:
        data = sec_edgar.get_json(f"{sec_edgar.SEC_DATA_BASE}/submissions/CIK{cik:010d}.json")
        filings = data.get("filings") or {}
        forms, items = _block(filings.get("recent") or {})
        files = [f["name"] for f in filings.get("files") or [] if f.get("name")]
        fundish = any(f.startswith(FUNDISH) for f in forms)
        older = False
        if files and ((fundish and "N-54A" not in forms) or not (forms & PERIODIC)):
            older = True
            for name in files:
                page_forms, page_items = _block(sec_edgar.get_json(
                    f"{sec_edgar.SEC_DATA_BASE}/submissions/{name}"))
                forms |= page_forms
                items |= page_items
        row.update(name=data.get("name"), sic=str(data.get("sic") or ""),
                   sic_description=data.get("sicDescription") or "",
                   entity_type=data.get("entityType"), tickers=data.get("tickers") or [],
                   exchanges=data.get("exchanges") or [], forms=sorted(forms),
                   items_8k=sorted(items), shell_closed=_SHELL_ITEM in items,
                   older_fetched=older)
    except httpx.HTTPStatusError as exc:
        if _status_code(exc) == 404:
            row["status"] = "not_found"
        else:
            row.update(status="error", error=redact(f"HTTP {_status_code(exc)}"))
    except Exception as exc:  # noqa: BLE001
        row.update(status="error", error=redact(f"{type(exc).__name__}: {exc}"[:200]))
    return row


def submissions(ciks, ticker_by_cik: dict | None = None, workers: int = 4) -> SourceResult:
    """Trimmed filing summary per CIK: name, sic, entity type, tickers, exchanges, form set, shell flag.

    ``ciks`` are ints or digit strings (de-duplicated). ``ticker_by_cik`` optionally stamps a
    ``ticker`` on each row so the cache can key it. Old filing pages are fetched only when the
    ``recent`` page cannot decide the fund question (port of R1's rule).

    A CIK the SEC does not know is a ``not_found`` row, a failed CIK an ``error`` row (never
    cached). The result is ``error`` only when every CIK failed; partial failure stays ``ok``
    with the count in ``error``.
    """
    unique = sorted({int(c) for c in ciks})
    tmap = {int(k): v for k, v in (ticker_by_cik or {}).items()}
    if not unique:
        return SourceResult(SUBMISSIONS_SOURCE, "ok", [], None, now_iso())
    out: dict[int, dict] = {}
    lock = threading.Lock()

    def work(cik: int):
        row = _one(cik, tmap.get(cik))
        with lock:
            out[cik] = row

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(unique)))) as pool:
        list(pool.map(work, unique))
    rows = [out[c] for c in unique]
    failed = sum(1 for r in rows if r["status"] == "error")
    note = f"{failed} of {len(rows)} CIKs failed" if failed else None
    status = "error" if failed == len(rows) else "ok"
    return SourceResult(SUBMISSIONS_SOURCE, status, rows, note, now_iso())
