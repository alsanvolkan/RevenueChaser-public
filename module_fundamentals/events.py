"""Corporate-event flag (#386 F5): the 8-K items that break the comparability of a filer's fundamentals.

``sec_submissions`` (edgar.duckdb) holds every filing of every universe CIK, written by the daily ``edgar_sync`` job
(``core/sec_submissions.record``). This reader turns the 8-Ks of the last ``config.CORPORATE_EVENT_WINDOW_DAYS``
into one flag per ticker: the item codes of ``config.CORPORATE_EVENT_ITEMS`` a filer reported, and the date of its
newest such filing.

**Display only.** Nothing here is scored, ranked, bucketed or frozen: the flag is not in ``freeze._HASHED`` and
changes no ``ledger.model_version``.

**Unknown is not false.** A ticker with no CIK, no filings or no qualifying item is absent from the result, never a
positive "no event" answer; a board that cannot see itself in the store says nothing.

Two read-only store reads, no write and no network call; a missing table read answers empty, like an empty store.
"""
from __future__ import annotations

from datetime import date, timedelta

from RevenueChaser.core import rawstore

from . import config


def _today():
    """The module's one calendar seam: the pipeline's ``_today``, which a test pins for the whole board."""
    from . import pipeline
    return pipeline._today()


def _norm_cik(v) -> str | None:
    """``sec_submissions`` stores CIKs as text without leading zeros (``core.sec_submissions.norm_cik``); a
    non-numeric one is no CIK. ``'0001234'`` and ``'1234'`` both become ``'1234'``."""
    s = "" if v is None else str(v).strip()
    if not s.isdigit():
        return None
    return s.lstrip("0") or None


def _items(raw) -> set[str]:
    """The item codes of a filing's ``items`` text column: ``'2.02,9.01'`` -> ``{'2.02', '9.01'}``."""
    return {part.strip() for part in str(raw or "").split(",") if part.strip()}


def corporate_events(tickers, *, today=None) -> dict[str, dict]:
    """``{ticker: {"items": [codes, sorted], "last_filing_date": "YYYY-MM-DD"}}`` for tickers that have one.

    A ticker with no CIK, no row, or no qualifying filing is simply absent from the result.
    """
    today = _today() if today is None else today
    if not isinstance(today, date):
        today = date.fromisoformat(str(today)[:10])
    wanted = list(dict.fromkeys(tickers or []))
    if not wanted:
        return {}

    cik_of = {}
    for row in rawstore.query("SELECT ticker, cik FROM security_master WHERE cik IS NOT NULL AND cik <> ''",
                              db="raw"):
        cik = _norm_cik(row.get("cik"))
        if cik and row.get("ticker"):
            cik_of[row["ticker"]] = cik
    by_cik = {}
    for ticker in wanted:
        cik = cik_of.get(ticker)
        if cik:
            by_cik.setdefault(cik, []).append(ticker)
    if not by_cik:
        return {}

    ciks = list(by_cik)
    since = today - timedelta(days=config.CORPORATE_EVENT_WINDOW_DAYS)
    rows = rawstore.query(
        "SELECT cik, items, filing_date FROM sec_submissions "
        f"WHERE form = '8-K' AND filing_date >= ? AND cik IN ({', '.join(['?'] * len(ciks))})",
        [since, *ciks], db="edgar")

    wanted_items = set(config.CORPORATE_EVENT_ITEMS)
    found: dict[str, dict] = {}
    for row in rows:
        hit = _items(row.get("items")) & wanted_items
        filed = str(row.get("filing_date") or "")[:10]
        if not hit or not filed:
            continue
        for ticker in by_cik.get(_norm_cik(row.get("cik")), ()):
            ev = found.setdefault(ticker, {"items": set(), "last_filing_date": ""})
            ev["items"] |= hit
            ev["last_filing_date"] = max(ev["last_filing_date"], filed)
    return {t: {"items": sorted(ev["items"]), "last_filing_date": ev["last_filing_date"]}
            for t, ev in found.items()}
