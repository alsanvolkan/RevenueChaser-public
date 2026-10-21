"""SEC submissions (#299, D15): the one writer of ``sec_submissions`` and ``sec_submissions_cik`` (edgar.duckdb).

The daily EDGAR sync (``module_value_chain/sync.py``, job ``edgar_sync``, task RC-ValueChain-Sync 13:00) already
asks ``data.sec.gov/submissions/CIK##########.json`` once for every universe CIK. It hands each answer to a
:class:`Capture`, and the sync's own thread stores them here with the run's ``run_id``; storing makes no request.
A Refresh fetches nothing, so it writes nothing.

**Filings** (``sec_submissions``, event): one row per (cik, accession) from ``filings.recent``, written once. A filed
accession does not change, so a later answer never rewrites it (append, first write wins); a re-run stores only the
accessions it has not seen.

**Header** (``sec_submissions_cik``, change history, rule A of ``phase2/06_interface.md`` §2): name, SIC, entity type,
tickers and exchanges per CIK. A row is appended only when one of those differs from the newest row of the CIK;
``valid_from`` is the run's session (``core/calendar.py``; NULL outside the holiday table), ``status`` 'present'.
A CIK the SEC answers 404 for gets one 'absent' row when its newest row is 'present' (a CIK never stored gets none).
A failed request writes nothing: the stored rows stay.

**Watermark** (``source_checks``, raw.duckdb): one row per CIK the SEC answered (dataset and source
'sec_submissions', key = CIK), with the time and ``run_id`` of the last answer. Data Health reads the last run's CIK
count from it, since an unchanged header appends nothing. It is written after the edgar commit (another file, so not
in the same transaction); a lost watermark only under-counts that Data Health line.

CIKs are TEXT without leading zeros, as ``core/identity/sources/sec.py`` writes them.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date

from RevenueChaser.core import calendar, rawstore

logger = logging.getLogger(__name__)

FILINGS = "sec_submissions"
HEADER = "sec_submissions_cik"
CHECKS = "source_checks"
SOURCE = "sec_submissions"            # the rule-A ``source`` of both tables and the watermark's dataset and source
PRESENT, ABSENT = "present", "absent"

# The header columns rule A compares; the rest of a row is stamps.
TRACKED = ("name", "sic", "sic_description", "entity_type", "tickers", "exchanges")

EMPTY_RESULT = {"ciks": 0, "filings_appended": 0, "headers_appended": 0, "headers_unchanged": 0,
                "headers_absent": 0}


def _clock() -> float:
    """Epoch seconds; the one clock seam of this module (tests pin it)."""
    return time.time()


def norm_cik(cik) -> str:
    """``'0001045810'``, ``1045810`` -> ``'1045810'``."""
    return str(int(str(cik).strip()))


def _text(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _date(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _list(v) -> list:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return [v]
    return [None if x is None else str(x) for x in v] if isinstance(v, (list, tuple)) else []


def rows_from_payload(cik, payload: dict) -> tuple[dict, list[dict]]:
    """``(header, filings)`` of one submissions answer, without stamps. Filings come from ``filings.recent``; a row
    without an accession is dropped."""
    c = norm_cik(cik)
    payload = payload or {}
    header = {"cik": c, "name": _text(payload.get("name")), "sic": _text(payload.get("sic")),
              "sic_description": _text(payload.get("sicDescription")),
              "entity_type": _text(payload.get("entityType")),
              "tickers": _list(payload.get("tickers")), "exchanges": _list(payload.get("exchanges"))}
    recent = (payload.get("filings") or {}).get("recent") or {}
    accs = recent.get("accessionNumber") or []

    def col(name, i):
        vals = recent.get(name) or []
        return vals[i] if i < len(vals) else None

    filings = []
    for i, acc in enumerate(accs):
        acc = _text(acc)
        if not acc:
            continue
        filings.append({"cik": c, "accession": acc, "form": _text(col("form", i)),
                        "filing_date": _date(col("filingDate", i)),
                        "primary_document": _text(col("primaryDocument", i)), "items": _text(col("items", i))})
    return header, filings


def _comparable(row: dict) -> tuple:
    return tuple(json.dumps(_list(row.get(k))) if k in ("tickers", "exchanges") else _text(row.get(k))
                 for k in TRACKED)


def _placeholders(n: int) -> str:
    return ", ".join(["?"] * n)


def _latest_headers(tx, ciks: list[str]) -> dict[str, dict]:
    """``{cik: newest stored header row}`` of ``ciks``."""
    cur = tx.execute(
        f"SELECT cik, status, {', '.join(TRACKED)} FROM {HEADER} WHERE cik IN ({_placeholders(len(ciks))}) "
        f"QUALIFY ROW_NUMBER() OVER (PARTITION BY cik ORDER BY run_at DESC) = 1", ciks)
    cols = [d[0] for d in cur.description]
    return {r["cik"]: r for r in (dict(zip(cols, v)) for v in cur.fetchall())}


def _stored_accessions(tx, ciks: list[str]) -> set[tuple[str, str]]:
    cur = tx.execute(f"SELECT cik, accession FROM {FILINGS} WHERE cik IN ({_placeholders(len(ciks))})", ciks)
    return {(c, a) for c, a in cur.fetchall()}


def record(answers: dict[str, tuple[dict, list[dict], float]], missing: dict[str, float], *, run_id: str,
           run_at: float) -> dict:
    """Store one batch, all or nothing in one edgar transaction, then the watermarks.

    ``answers``: ``{cik: (header, filings, fetched_at)}`` from :func:`rows_from_payload`; ``missing``:
    ``{cik: fetched_at}`` of CIKs the SEC answered 404. Returns the counts (``EMPTY_RESULT`` keys). Raises on a
    store error."""
    res = dict(EMPTY_RESULT)
    if not answers and not missing:
        return res
    try:
        valid_from = calendar.session_for(run_at).session_date
    except ValueError:
        valid_from = None                      # outside the holiday table: the row keeps run_at only
    stamps = {"source": SOURCE, "run_id": run_id, "run_at": run_at}
    ciks = sorted(set(answers) | set(missing))

    def _write(tx):
        out = dict(EMPTY_RESULT)
        tx.ensure_table(FILINGS)
        tx.ensure_table(HEADER)
        latest = _latest_headers(tx, ciks)
        stored = _stored_accessions(tx, sorted(answers)) if answers else set()
        headers, filings = [], []
        for cik, (header, rows, fetched_at) in answers.items():
            prev = latest.get(cik)
            if prev and prev["status"] == PRESENT and _comparable(prev) == _comparable(header):
                out["headers_unchanged"] += 1
            else:
                headers.append({**header, "valid_from": valid_from, "status": PRESENT, "fetched_at": fetched_at,
                                **stamps})
            for r in rows:
                if (cik, r["accession"]) not in stored:
                    stored.add((cik, r["accession"]))
                    filings.append({**r, "fetched_at": fetched_at, **stamps})
        for cik, fetched_at in missing.items():
            prev = latest.get(cik)
            if prev and prev["status"] == PRESENT:
                headers.append({"cik": cik, "valid_from": valid_from, "status": ABSENT, "fetched_at": fetched_at,
                                **stamps})
                out["headers_absent"] += 1
        tx.write_rows(HEADER, headers)
        tx.write_rows(FILINGS, filings)
        out["headers_appended"] = len(headers) - out["headers_absent"]
        out["filings_appended"] = len(filings)
        return out

    res.update(rawstore.atomic(_write, db="edgar"))
    res["ciks"] = len(ciks)
    checks = {**{c: a[2] for c, a in answers.items()}, **missing}
    rawstore.write_rows(CHECKS, [{"dataset": SOURCE, "source": SOURCE, "key": c, "checked_at": t, "run_id": run_id}
                                 for c, t in sorted(checks.items())])
    return res


class Capture:
    """The submissions answers a sync collected, thread-safely, stored by :meth:`flush` on the sync's thread (once
    per batch of CIKs and at the end). ``run_at`` is the run's time, the same for every row of the run."""

    def __init__(self, run_at: float | None = None):
        self.run_at = _clock() if run_at is None else float(run_at)
        self._lock = threading.Lock()
        self._answers: dict[str, tuple[dict, list[dict], float]] = {}
        self._missing: dict[str, float] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._answers) + len(self._missing)

    def add(self, cik, payload: dict) -> None:
        """One 200 answer. A CIK twice in one batch keeps its first answer."""
        header, filings = rows_from_payload(cik, payload)
        now = _clock()
        with self._lock:
            self._answers.setdefault(header["cik"], (header, filings, now))

    def add_missing(self, cik) -> None:
        """A 404: the SEC does not serve this CIK."""
        now = _clock()
        with self._lock:
            self._missing.setdefault(norm_cik(cik), now)

    def flush(self, *, run_id: str) -> dict:
        """Store and clear what was collected. A store failure is an ``error`` key, never an exception: the corpus
        the sync builds does not depend on these rows."""
        with self._lock:
            answers, self._answers = self._answers, {}
            missing, self._missing = self._missing, {}
        if not answers and not missing:
            return dict(EMPTY_RESULT)
        try:
            return record(answers, missing, run_id=run_id, run_at=self.run_at)
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.exception("SEC submissions were not stored")
            return {**EMPTY_RESULT, "error": f"{type(exc).__name__}: {exc}"}


def health_summary() -> dict:
    """Read-only summary for the Data Health row, zero network."""
    out = {"filings": 0, "ciks": 0, "newest_filing_date": None, "newest_fetched_at": None, "last_run_id": None,
           "last_run_at": None, "last_run_ciks": 0, "checked_ciks": set(), "last_run_checked": set()}
    f = rawstore.query(f"SELECT COUNT(*) AS n, COUNT(DISTINCT cik) AS c, MAX(filing_date) AS d, "
                       f"MAX(fetched_at) AS t FROM {FILINGS}", db="edgar")
    if f:
        out.update(filings=int(f[0]["n"] or 0), ciks=int(f[0]["c"] or 0), newest_filing_date=f[0]["d"],
                   newest_fetched_at=f[0]["t"])
    checks = rawstore.query(f"SELECT key, checked_at, run_id FROM {CHECKS} WHERE dataset = ? AND source = ?",
                            [SOURCE, SOURCE])
    if checks:
        last = max(checks, key=lambda r: r["checked_at"] or 0)
        out["last_run_id"], out["last_run_at"] = last["run_id"], last["checked_at"]
        out["checked_ciks"] = {r["key"] for r in checks}
        out["last_run_checked"] = {r["key"] for r in checks if r["run_id"] == last["run_id"]}
        out["last_run_ciks"] = len(out["last_run_checked"])
    return out


__all__ = ["FILINGS", "HEADER", "SOURCE", "Capture", "record", "rows_from_payload", "norm_cik", "health_summary"]
