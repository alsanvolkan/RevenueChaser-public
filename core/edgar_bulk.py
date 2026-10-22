"""EDGAR bulk-file ingest (official free zips) -> ``raw.duckdb``.

Downloads the two bulk archives to ``.cache/edgar_bulk/``, ingests them, then
deletes each zip (they are large and regenerable) **only after** that archive was
ingested in full. A zip is never deleted after an error, with ``--limit`` (the
ingest stopped early) or with ``--keep``. A download goes to ``<name>.part`` and
replaces the zip only when it finished, so a failed download leaves the previous
zip as it was:

- ``companyfacts.zip`` -> ``edgar_facts`` (typed grain ``(cik, concept, period, form)``)
- ``submissions.zip``  -> ``sec_company_sic`` (SIC coverage for the universe)

The live download is deliberately separate from ingest so the 10-CIK fixture gate
and re-ingest can run offline:

    python -m RevenueChaser.core.edgar_bulk --download
    python -m RevenueChaser.core.edgar_bulk --ingest
"""
from __future__ import annotations

import json
import logging
import os
import time
import zipfile
from pathlib import Path

from . import rawstore, sec_files
from .config import OUTPUT_DIR
from .providers import sec_edgar

logger = logging.getLogger(__name__)

# Paths under ``sec_edgar.SEC_WWW_BASE``. The host is read when a download starts (a test or
# the e2e harness can point it at a local mock), so it is never frozen into a constant here.
COMPANYFACTS_PATH = "/Archives/edgar/daily-index/xbrl/companyfacts.zip"
SUBMISSIONS_PATH = "/Archives/edgar/daily-index/bulkdata/submissions.zip"

_FACT_CHUNK = 50_000


def _used_concepts() -> set[str]:
    """Concept names the app actually reads (``sec_edgar.CONCEPTS``).

    The bulk ingest keeps only these, which cuts ``edgar_facts`` ~10x versus
    storing every XBRL concept.
    """
    out: set[str] = set()
    for spec in getattr(sec_edgar, "CONCEPTS", {}).values():
        if isinstance(spec, (tuple, list)) and spec:
            out.update(spec[0] or [])
    return out


USED_CONCEPTS = _used_concepts()


def bulk_dir() -> Path:
    p = OUTPUT_DIR / "edgar_bulk"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _f(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def download(url: str, dest: Path, client=None) -> Path:
    """Stream ``url`` to ``dest`` (SEC headers + shared throttle).

    The bytes go to ``<dest>.part`` and replace ``dest`` (``os.replace``, atomic) only
    after the stream ended and the status was checked. A failure at any point removes
    the ``.part`` file and leaves the previous ``dest`` byte for byte as it was.
    """
    import httpx
    own = client is None
    if own:
        client = httpx.Client(headers=sec_edgar._headers(), timeout=300,
                              follow_redirects=True)
    throttle = getattr(sec_edgar, "_throttle", None)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        if callable(throttle):
            throttle()
        with client.stream("GET", url) as r:
            r.raise_for_status()
            with open(part, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)
        if own:
            client.close()
    return dest


def bulk_urls() -> dict[str, str]:
    """``{zip name: url}`` under the SEC host as it is *now* (``sec_edgar.SEC_WWW_BASE``)."""
    base = sec_edgar.SEC_WWW_BASE.rstrip("/")
    return {"companyfacts.zip": base + COMPANYFACTS_PATH,
            "submissions.zip": base + SUBMISSIONS_PATH}


def download_all(client=None) -> dict:
    d = bulk_dir()
    out = {}
    for name, url in bulk_urls().items():
        out[name] = str(download(url, d / name, client))
    return out


def companyfacts_rows(payload: dict) -> list[dict]:
    """Flatten one ``CIK##########.json`` companyfacts payload to fact rows."""
    cik = str(payload.get("cik") or "")
    if not cik:
        return []
    rows = []
    for _taxonomy, concepts in (payload.get("facts") or {}).items():
        for concept, body in (concepts or {}).items():
            if USED_CONCEPTS and concept not in USED_CONCEPTS:
                continue
            for unit, facts in ((body or {}).get("units") or {}).items():
                for fact in facts or []:
                    val = _f(fact.get("val"))
                    if val is None:
                        continue
                    period = fact.get("end") or fact.get("start")
                    form = fact.get("form") or ""
                    if not period:
                        continue
                    rows.append({"cik": cik, "concept": concept, "period": period,
                                 "form": form, "value": val, "unit": unit})
    return rows


def ingest_companyfacts(zip_path: Path, limit: int | None = None) -> dict:
    """Ingest ``companyfacts.zip`` -> ``edgar_facts`` (upsert by fact key)."""
    rawstore.ensure_table("edgar_facts")
    files = facts = 0
    buf: list[dict] = []
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.endswith(".json"):
                continue
            try:
                payload = json.loads(zf.read(name))
            except (json.JSONDecodeError, KeyError):
                continue
            buf.extend(companyfacts_rows(payload))
            files += 1
            while len(buf) >= _FACT_CHUNK:
                facts += rawstore.write_rows("edgar_facts", buf[:_FACT_CHUNK])
                buf = buf[_FACT_CHUNK:]
            if limit and files >= limit:
                break
    if buf:
        facts += rawstore.write_rows("edgar_facts", buf)
    return {"files": files, "facts": facts}


def submissions_rows(payload: dict) -> dict | None:
    """``{cik, sic, sic_description, name}`` from one submissions payload."""
    cik = str(payload.get("cik") or "").lstrip("0") or "0"
    sic = str(payload.get("sic") or "")
    if not cik or not sic:
        return None
    return {"cik": cik, "sic": sic,
            "sic_description": str(payload.get("sicDescription") or ""),
            "name": str(payload.get("name") or "")}


def ingest_submissions(zip_path: Path, limit: int | None = None) -> dict:
    """Ingest ``submissions.zip`` -> ``sec_company_sic`` (ticker-keyed)."""
    by_cik = {}
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.endswith(".json"):
                continue
            try:
                payload = json.loads(zf.read(name))
            except (json.JSONDecodeError, KeyError):
                continue
            row = submissions_rows(payload)
            if row:
                by_cik[row["cik"]] = row
            if limit and len(by_cik) >= limit:
                break

    tickers = {r["ticker"]: str(r["cik"]).lstrip("0")
               for r in sec_files.latest("exchange") if r.get("cik")}
    now = time.time()
    rows = [{"ticker": t, "cik": c, "sic": by_cik[c]["sic"],
             "sic_description": by_cik[c]["sic_description"], "fetched_at": now}
            for t, c in tickers.items() if c in by_cik]
    if rows:
        rawstore.write_rows("sec_company_sic", rows)
    coverage = (len(rows) / len(tickers)) if tickers else 0.0
    return {"ciks": len(by_cik), "matched": len(rows),
            "tickers": len(tickers), "coverage": round(coverage, 4)}


def ingest_dir(limit: int | None = None, delete: bool = True) -> dict:
    """Ingest whatever bulk zips are present, then (by default) delete the finished ones.

    A zip is deleted only after its ingest ran to the end: not when ``limit`` cut the
    ingest short, not when it raised (the exception propagates and the zip stays for a
    re-run), and never with ``delete=False``.
    """
    d = bulk_dir()
    out: dict = {}
    for name, fn in (("companyfacts.zip", ingest_companyfacts),
                     ("submissions.zip", ingest_submissions)):
        p = d / name
        if not p.is_file():
            continue
        out[name] = fn(p, limit=limit)
        if delete and not limit:
            p.unlink(missing_ok=True)
        else:
            logger.info("kept %s (%s)", p, "limit" if limit else "delete=False")
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="EDGAR bulk zip download + ingest")
    p.add_argument("--download", action="store_true", help="fetch the bulk zips")
    p.add_argument("--ingest", action="store_true", help="ingest local bulk zips")
    p.add_argument("--limit", type=int, default=None, help="cap files per archive")
    p.add_argument("--keep", action="store_true", help="do not delete zips after ingest")
    args = p.parse_args(argv)
    if args.download:
        print(download_all())
    if args.ingest:
        print(ingest_dir(limit=args.limit, delete=not args.keep))
    if not args.download and not args.ingest:
        print("use --download and/or --ingest")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
