"""Fetch and slice the latest annual filing (10-K / 20-F) per issuer from SEC EDGAR.

One submissions call picks the filing; one archive call gets the body. The body
document is resolved from ``primaryDocument`` — the submissions ``primaryDocument``
is usually the real 10-K/20-F, but can be an XSLT/``.txt`` wrapper, so we accept
whatever it points at and let the splitter's ``quality`` flag catch a bad slice.
"""
from __future__ import annotations

import logging
import time

from RevenueChaser.core import edgar_docs, edgar_store, rawstore
from RevenueChaser.core.providers import sec_edgar

from . import splitter

logger = logging.getLogger(__name__)

ARCHIVES = sec_edgar.SEC_WWW_BASE + "/Archives/edgar/data"


def submissions_url(cik) -> str:
    return f"{sec_edgar.SEC_DATA_BASE}/submissions/CIK{int(cik):010d}.json"


def document_url(cik, accession: str, primary_doc: str) -> str:
    acc = str(accession).replace("-", "")
    return f"{ARCHIVES}/{int(cik)}/{acc}/{primary_doc}"


# Annual forms in tie order (same filing date: 10-K first). A 40-F is recognised
# only so it can be recorded as a named miss: its business text sits in the AIF
# exhibit, which is not sliced yet (#184). Amendments (10-K/A) never count.
ANNUAL_FORMS = ("10-K", "20-F", "40-F")
FORM_40F = "40-F"


def pick_filing(payload: dict) -> dict | None:
    """Newest annual filing by filing date (10-K / 20-F / 40-F; a 10-K/A never counts).

    A re-domiciled issuer keeps its old 10-K in ``filings.recent``, so "any 10-K
    first" picked a 2019 10-K over a 2026 20-F (#116 F4).
    """
    recent = (payload or {}).get("filings", {}).get("recent", {})
    forms = recent.get("form") or []
    best: dict | None = None
    for i, form in enumerate(forms):
        f = str(form or "")
        if f not in ANNUAL_FORMS:
            continue
        try:
            cand = {
                "form": f,
                "filed": str(recent["filingDate"][i]),
                "accession": str(recent["accessionNumber"][i]),
                "primary_doc": str(recent["primaryDocument"][i]),
            }
        except (IndexError, KeyError, TypeError):
            continue
        rank = (cand["filed"], -ANNUAL_FORMS.index(f))
        if best is None or rank > (best["filed"], -ANNUAL_FORMS.index(best["form"])):
            best = cand
    return best


def slice_document(ticker: str, cik, filing: dict, html: str, fetched_at: float | None = None) -> dict:
    """Slice one filing's raw HTML into a ``chain_filings`` row (no network).

    ``fetched_at`` is when the document was downloaded (K6, #383 R1); without it, the time of this slice."""
    url = document_url(cik, filing["accession"], filing["primary_doc"])
    item, quality = splitter.slice_item(splitter.strip_html(html), filing["form"])
    return {
        "ticker": ticker.upper(),
        "cik": str(cik),
        "form": filing["form"],
        "filed": filing["filed"],
        "accession": filing["accession"],
        "primary_doc": url,
        "item_text": item or "",
        "quality": quality,
        "fetched_at": time.time() if fetched_at is None else float(fetched_at),
    }


def fetch_document(ticker: str, cik, filing: dict, client=None) -> tuple[str, dict]:
    """Fetch + slice one already-chosen filing; return ``(raw_html, row)``."""
    url = document_url(cik, filing["accession"], filing["primary_doc"])
    html = sec_edgar.get_text(url, client)
    return html, slice_document(ticker, cik, filing, html, fetched_at=time.time())


def document_slice(ticker: str, cik, filing: dict, client=None, run_at: float | None = None) -> dict:
    """The ``chain_filings`` row of one chosen filing, through the one document path (#299, D16).

    A filing whose raw doc is already stored is re-sliced from it: no download, no second write. Otherwise the doc
    is downloaded and stored, the HTML as an ``edgar_docs`` file and its metadata as an ``edgar_documents`` row
    (append: the first write of an accession wins). Both the daily sync and the Value Chain Fetch take this path.

    The row's ``fetched_at`` (K6, #383 R1) is the document's download: a re-slice carries the stored document's
    ``edgar_documents.fetched_at``, or the time of the slice when the file has no metadata row; a download carries
    the stamp its ``edgar_documents`` row gets.
    """
    acc = filing["accession"]
    if edgar_docs.exists(cik, acc):
        doc = rawstore.get("edgar_documents", acc) or {}
        return slice_document(ticker, cik, filing, edgar_docs.read_html(cik, acc) or "",
                              fetched_at=doc.get("fetched_at"))
    html, row = fetch_document(ticker, cik, filing, client)
    doc = edgar_store.document_row(
        acc, cik, ticker, filing["form"], filing["filed"], html, doc_url=row["primary_doc"], run_at=run_at)
    edgar_store.save_document(doc)
    if doc.get("fetched_at") is not None:
        row["fetched_at"] = doc["fetched_at"]
    return row


def fetch_filing(ticker: str, cik, client=None) -> dict:
    """Fetch, strip and slice one issuer's business description.

    Returns the ``chain_filings`` row, or a reason instead of a row:
    ``{"ticker", "miss": "no_filing" | "form_40f"}`` when the issuer has no usable
    annual filing, ``{"ticker", "error": ...}`` on a network failure. Only a miss
    may be recorded; an error is retried on the next run.
    """
    try:
        payload = sec_edgar.get_json(submissions_url(cik), client)
    except Exception as exc:  # noqa: BLE001 - per-ticker network failure
        logger.warning("value_chain %s: submissions failed: %s", ticker, exc)
        return {"ticker": ticker.upper(), "error": str(exc)}
    filing = pick_filing(payload)
    if not filing:
        return {"ticker": ticker.upper(), "miss": "no_filing"}
    if filing["form"] == FORM_40F:
        return {"ticker": ticker.upper(), "miss": "form_40f"}
    try:
        row = document_slice(ticker, cik, filing, client)
    except Exception as exc:  # noqa: BLE001 - per-ticker network failure
        logger.warning("value_chain %s: fetch failed: %s", ticker, exc)
        return {"ticker": ticker.upper(), "error": str(exc)}
    return row
