"""Raw EDGAR 10-K/20-F HTML — the durable, file-based raw-document record.

One gzip file per filing, keyed by CIK + accession:
``{EDGAR_DOCS_DIR}/{cik}/{accession}.html.gz``. Files (not a DuckDB BLOB) because
the raw documents are large, immutable and write-once; ``edgar_documents`` in
``edgar.duckdb`` keeps only the metadata (path, sha256, sizes). CIK keying means
dual-class shares (same CIK, same filing) store one file, not one per ticker.

See docs/classification_design.md Phase 4 and docs/data_architecture.md.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import tempfile
from pathlib import Path

from .config import EDGAR_DOCS_DIR


def _cik_dir(cik) -> Path:
    s = str(cik)
    try:
        s = str(int(s))  # normalise "0000320193" / "320193"
    except (ValueError, TypeError):
        pass
    return Path(EDGAR_DOCS_DIR) / s


def path_for(cik, accession: str) -> Path:
    return _cik_dir(cik) / f"{accession}.html.gz"


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_html(cik, accession: str, html: str | bytes) -> dict:
    """Write one filing's HTML as gzip; return ``{path, sha256, bytes_raw, bytes_gzip}``.

    Atomic: a temp file in the destination dir is ``os.replace``d into place, so a
    crash can never leave a half-written document.
    """
    raw = html.encode("utf-8") if isinstance(html, str) else html
    gz = gzip.compress(raw, compresslevel=6)
    dest = path_for(cik, accession)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(gz)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"path": str(dest), "sha256": sha256_hex(raw),
            "bytes_raw": len(raw), "bytes_gzip": len(gz)}


def read_html(cik, accession: str) -> str | None:
    p = path_for(cik, accession)
    if not p.is_file():
        return None
    return gzip.decompress(p.read_bytes()).decode("utf-8", errors="replace")


def exists(cik, accession: str) -> bool:
    return path_for(cik, accession).is_file()


def count() -> int:
    root = Path(EDGAR_DOCS_DIR)
    if not root.exists():
        return 0
    return sum(1 for _ in root.rglob("*.html.gz"))


__all__ = ["path_for", "write_html", "read_html", "exists", "count", "sha256_hex"]
