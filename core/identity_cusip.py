"""CUSIP -> ticker and FIGI map (#207 F1, D19): the one writer of ``identity_cusip`` (raw.duckdb).

The 13F map step (``app/runners._13f_map_step``) asks OpenFIGI (:mod:`core.providers.openfigi`) about the CUSIPs of
the ingested holdings and hands the answers to :func:`record`. The table is a change history (rule A of
``phase2/06_interface.md`` §2), append-only, key ``(cusip, run_at)``:

- an answer equal to the CUSIP's newest row appends nothing;
- a changed answer appends a 'present' row;
- a CUSIP that OpenFIGI no longer knows appends one 'absent' row. A CUSIP it never knew gets one 'absent' row at its
  first ask, so the map step does not ask it again (D19's check counts the holdings' CUSIPs against open 'present'
  and 'absent' rows);
- no row is updated or deleted.

``valid_from`` is the run's New York session (rule B; NULL outside the holiday table) and ``source`` is 'openfigi'.
``share_class`` stays NULL: OpenFIGI returns no class letter, and ``share_class_figi`` is the class key.

The current map (:func:`current_map`) is each CUSIP's newest row when that row is 'present' with a ticker.
``cusip_ticker`` is rebuilt from it (``module_sec_13f/engine.py``), plus the legacy entries whose issuer prefix it
confirms, issue numbers 90-99 left out (``engine.legacy_fallback``, avo's option B minus 9x).
"""
from __future__ import annotations

from RevenueChaser.core import calendar, rawstore

TABLE = "identity_cusip"
SOURCE = "openfigi"
PRESENT, ABSENT = "present", "absent"

# The answer columns rule A compares; the rest of a row is stamps.
TRACKED = ("status", "ticker", "figi", "share_class_figi", "share_class", "name", "exch_code", "security_type")

_LATEST_SQL = (f"SELECT cusip, {', '.join(TRACKED)}, source, run_id, run_at FROM {TABLE} "
               "QUALIFY ROW_NUMBER() OVER (PARTITION BY cusip ORDER BY run_at DESC) = 1")


def _comparable(row: dict) -> tuple:
    return tuple((row.get(k) or None) for k in TRACKED)


def has_rows() -> bool:
    """Whether OpenFIGI has answered about any CUSIP yet (the map is seeded)."""
    return bool(rawstore.query(f"SELECT 1 AS one FROM {TABLE} LIMIT 1"))


def latest() -> dict[str, dict]:
    """``{cusip: newest row}`` over the whole table (read only)."""
    return {r["cusip"]: r for r in rawstore.query(_LATEST_SQL)}


def current_map() -> dict[str, dict]:
    """``{cusip: {ticker, figi, share_class_figi, source, run_at}}``: CUSIPs whose newest row is 'present' with a
    ticker. A CUSIP whose newest row is 'absent' is out of the map."""
    return {c: {"ticker": r["ticker"], "figi": r["figi"], "share_class_figi": r["share_class_figi"],
                "source": r["source"], "run_at": r["run_at"]}
            for c, r in latest().items() if r["status"] == PRESENT and r["ticker"]}


def ever_mapped() -> set[str]:
    """CUSIPs with any 'present' row with a ticker, newest or not (read only). The 13F map's legacy prefix fallback
    (``module_sec_13f/engine.legacy_fallback``) never revives one of them: OpenFIGI's later answer stands."""
    return {r["cusip"] for r in rawstore.query(
        f"SELECT DISTINCT cusip FROM {TABLE} WHERE status = ? AND coalesce(ticker, '') <> ''", [PRESENT])}


def record(answers: dict[str, dict], *, run_id: str, run_at: float, source: str = SOURCE) -> dict:
    """Append the answers that change the map, in one transaction. ``answers``: ``{cusip: answer}`` as
    :func:`core.providers.openfigi.map_cusips` returns them. Returns ``{"answered", "appended", "unchanged",
    "absent"}`` (``absent`` counts the 'absent' rows appended). Raises on a store error."""
    out = {"answered": len(answers), "appended": 0, "unchanged": 0, "absent": 0}
    if not answers:
        return out
    try:
        valid_from = calendar.session_for(run_at).session_date
    except ValueError:
        valid_from = None                      # outside the holiday table: the row keeps run_at only
    stamps = {"valid_from": valid_from, "source": source, "run_id": run_id, "run_at": run_at}

    def _write(tx):
        res = dict(out)
        tx.ensure_table(TABLE)
        cur = tx.execute(_LATEST_SQL)
        cols = [d[0] for d in cur.description]
        newest = {r["cusip"]: r for r in (dict(zip(cols, v)) for v in cur.fetchall())}
        rows = []
        for cusip, answer in sorted(answers.items()):
            row = {"cusip": cusip, **{k: answer.get(k) for k in TRACKED}}
            prev = newest.get(cusip)
            if prev is not None and _comparable(prev) == _comparable(row):
                res["unchanged"] += 1
                continue
            rows.append({**row, "fetched_at": answer.get("fetched_at"), **stamps})
            res["absent"] += row["status"] == ABSENT
        tx.write_rows(TABLE, rows)
        res["appended"] = len(rows)
        return res

    return rawstore.atomic(_write)
