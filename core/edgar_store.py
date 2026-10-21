"""EDGAR document **metadata** store (``.store/edgar.duckdb``).

``edgar_documents`` holds metadata only — the raw 10-K/20-F HTML lives as gzip
files under ``edgar_docs/`` (``core/edgar_docs.py``), keyed by CIK + accession.

Writes/reads go through ``rawstore``, which routes the ``edgar_documents`` table
to ``edgar.duckdb`` (MANIFEST ``"db": "edgar"``):

- ``document_row(...)`` writes the HTML file and returns the metadata row.
- ``save_document(row)`` appends that metadata; the first write of an accession
  wins, a later one is a no-op (#299, D16: a filed document does not change).
- ``get_document(accession)`` returns metadata + the decompressed ``content``.

See docs/classification_design.md Phase 4 and docs/data_architecture.md.
"""
from __future__ import annotations

import gzip
import time
from pathlib import Path

import duckdb

from . import edgar_docs, rawstore


def edgar_db_path() -> Path:
    """The EDGAR store file (follows ``rawstore.DB_PATH`` redirects)."""
    return rawstore.edgar_db_path()


def connect(path: Path | None = None, read_only: bool = False):
    """Open ``edgar.duckdb`` directly (defaults to the configured store file)."""
    p = Path(path or edgar_db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(p), read_only=read_only)


def ensure_schema(path: Path | None = None) -> None:
    """Create ``edgar_documents`` (via rawstore for the default store)."""
    if path is None:
        rawstore.ensure_table("edgar_documents")
        return
    conn = duckdb.connect(str(Path(path)))
    try:
        conn.execute(_DDL)
    finally:
        conn.close()


def compress(text: str | bytes) -> bytes:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return gzip.compress(raw, compresslevel=6)


def decompress(blob: bytes) -> str:
    return gzip.decompress(blob).decode("utf-8", errors="replace")


# The rule-A ``source`` of a stored document (D16): the SEC Archives.
SOURCE = "sec_archives"


def document_row(accession: str, cik, ticker: str, form: str, filed: str,
                 html: str, doc_url: str = "", fiscal_year: str = "",
                 tickers: list[str] | None = None, run_at: float | None = None) -> dict:
    """Write the HTML file (``edgar_docs``) and return the metadata row.

    ``run_at`` is the writing run's time (default: now, the ``fetched_at``)."""
    info = edgar_docs.write_html(cik, accession, html)
    tk = tickers if tickers is not None else ([ticker] if ticker else [])
    fetched_at = time.time()
    return {
        "accession": accession,
        "cik": str(cik),
        "tickers": ",".join(t.upper() for t in tk),
        "form": form,
        "filed": filed,
        "fiscal_year": fiscal_year,
        "doc_url": doc_url,
        "path": info["path"],
        "sha256": info["sha256"],
        "bytes_raw": info["bytes_raw"],
        "bytes_gzip": info["bytes_gzip"],
        "fetched_at": fetched_at,
        "source": SOURCE,
        "run_at": fetched_at if run_at is None else float(run_at),
    }


def save_document(row: dict) -> None:
    """Append one document's metadata; a stored accession is never rewritten (first write wins)."""
    rawstore.write_rows("edgar_documents", [row])


def get_document(accession: str) -> dict | None:
    """Metadata row with ``content`` decompressed from the ``edgar_docs`` file."""
    out = rawstore.get("edgar_documents", accession)
    if out is None:
        return None
    out["content"] = edgar_docs.read_html(out.get("cik"), accession) or ""
    return out


def count() -> int:
    return rawstore.count("edgar_documents")


_DDL = """
CREATE TABLE IF NOT EXISTS edgar_documents (
    accession     TEXT PRIMARY KEY,
    cik           TEXT,
    tickers       TEXT,
    form          TEXT,
    filed         TEXT,
    fiscal_year   TEXT,
    doc_url       TEXT,
    path          TEXT,
    sha256        TEXT,
    bytes_raw     BIGINT,
    bytes_gzip    BIGINT,
    fetched_at    DOUBLE,
    source        TEXT,
    run_at        DOUBLE
)
"""


__all__ = ["edgar_db_path", "connect", "ensure_schema", "compress", "decompress",
           "document_row", "save_document", "get_document", "count"]
