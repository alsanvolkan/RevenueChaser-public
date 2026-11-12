"""The 13F board's own file, ``boards/sec_13f.duckdb`` (#732; owner, 12 Nov 2026: "13F panosu sonuclarini kendi pano
duckdb sine yazmali, taki update etmesi gerekene kadar da oradan okumalali").

The one module that writes the file. Three tables (``core/rawstore.py`` MANIFEST, ``"db": "sec_13f"``):

- ``sec13f_board_cells``: per ticker and report period, the builder's metrics as filed (``core.ownership``'s
  ``sec13f_totals`` cell: shares, value, holders, flows, ...), ``complete`` and ``closed``.
- ``sec13f_board_breaks``: per ticker and settled period, each filer that started or stopped reporting it.
- ``sec13f_board_meta``: ``source``, the stamp of the ``ownership.duckdb`` build and the New York day the rows were
  taken from.

**When it updates.** The board reads this file while its stamp matches the live ownership build and today's New
York date (a quarter's ``complete`` turns on a date, so a new day is a new stamp). Otherwise ``sync`` (the Refresh
and the Fetch, through ``app.runners.run_sec_13f_latest``) reads the builder again and writes the file. A reader
that finds the file stale and cannot write (the chart route) reads the builder and merges it the same way, without
writing.

**A closed quarter does not change.** Once the file holds a period as ``closed`` (the SEC data set that covers its
filing deadline is published, ``rcdata`` ``sec13f_data_sets``), its cells and breaks are kept as stored and the
builder's rows for it are not taken, whatever a later build says (owner: "tam sayılsın ve üzerinde daha değişiklik
almayacak şekilde kapansın").

The counts are kept as filed. The board puts them on today's share basis when it renders, with the split factors
of the prices store (``enrich.load_inputs``), because a split after a quarter changes that basis but not what was
filed.
"""
from __future__ import annotations

import json
import logging
import time

from RevenueChaser.core import calendar, ownership, rawstore

logger = logging.getLogger(__name__)

DB = "sec_13f"
CELLS = "sec13f_board_cells"
BREAKS = "sec13f_board_breaks"
META = "sec13f_board_meta"
SOURCE_KEY = "source"

METRICS = ownership.SEC13F_METRICS
BREAK_FIELDS = ("kind", "shares", "cik", "filer_name")


def _iso(v) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _query(sql: str, params: list | None = None) -> list[dict]:
    """A read of the board file; a file or a table that is not there yet is no rows, never an error."""
    try:
        return rawstore.query(sql, params, db=DB)
    except Exception as exc:                      # noqa: BLE001 - a missing table is an empty board file
        logger.debug("13F board file read: %s", exc)
        return []


# ── the stamp ────────────────────────────────────────────────────────────────

def source_stamp() -> str | None:
    """The live ownership build (its file's size and modification time) and today's New York date; None when there
    is no ownership store to read."""
    from RevenueChaser.rcdata import serving
    path = serving.live_path("ownership")
    try:
        st = path.stat()
    except OSError:
        return None
    return f"{st.st_size}:{st.st_mtime_ns}:{calendar.today_ny().isoformat()}"


def stored_stamp() -> str | None:
    rows = _query(f"SELECT value FROM {META} WHERE key = ?", [SOURCE_KEY])
    return json.loads(rows[0]["value"]) if rows and rows[0]["value"] is not None else None


def is_current() -> bool:
    """The file holds the rows of the live build of today: the board reads it and asks the builder nothing."""
    stamp = source_stamp()
    return stamp is not None and stamp == stored_stamp()


# ── what the file holds ──────────────────────────────────────────────────────

def stored_totals() -> dict[str, dict]:
    """``{ticker: {period: cell}}`` from the file, in ``core.ownership.sec13f_totals``' shape."""
    out: dict[str, dict] = {}
    for r in _query(f"SELECT * FROM {CELLS} ORDER BY ticker, period"):
        cell = {m: r.get(m) for m in METRICS}
        cell["complete"] = bool(r["complete"])
        if r.get("closed") is not None:
            cell["closed"] = bool(r["closed"])
        out.setdefault(r["ticker"], {})[_iso(r["period"])] = cell
    return out


def stored_breaks(tickers=None) -> dict[str, list[dict]]:
    """``{ticker: [break]}`` from the file, in ``core.ownership.sec13f_filer_breaks``' shape."""
    sql, params = f"SELECT * FROM {BREAKS}", []
    wanted = sorted({str(t).strip().upper() for t in tickers if t}) if tickers is not None else None
    if wanted is not None:
        if not wanted:
            return {}
        sql += f" WHERE ticker IN ({', '.join('?' * len(wanted))})"
        params = wanted
    out: dict[str, list[dict]] = {}
    for r in _query(sql + " ORDER BY ticker, period, kind, shares DESC, cik", params):
        out.setdefault(r["ticker"], []).append({"period": _iso(r["period"]), **{k: r.get(k) for k in BREAK_FIELDS}})
    return out


def closed_periods(totals: dict) -> set[str]:
    """The periods a totals mapping holds as closed."""
    return {p for periods in totals.values() for p, cell in periods.items() if cell.get("closed") is True}


# ── the merge ────────────────────────────────────────────────────────────────

def merge(stored: dict, source: dict, stored_brk: dict, source_brk: dict) -> tuple[dict, dict]:
    """``(totals, breaks)``: the builder's rows, except that every period the file holds as closed keeps the file's
    rows and takes none of the builder's."""
    frozen = closed_periods(stored)
    totals: dict[str, dict] = {}
    for t, periods in source.items():
        for p, cell in periods.items():
            if p not in frozen:
                totals.setdefault(t, {})[p] = cell
    for t, periods in stored.items():
        for p, cell in periods.items():
            if p in frozen:
                totals.setdefault(t, {})[p] = cell
    breaks: dict[str, list[dict]] = {}
    for t, rows in source_brk.items():
        kept = [b for b in rows if b["period"] not in frozen]
        if kept:
            breaks.setdefault(t, []).extend(kept)
    for t, rows in stored_brk.items():
        kept = [b for b in rows if b["period"] in frozen]
        if kept:
            breaks.setdefault(t, []).extend(kept)
    for rows in breaks.values():
        rows.sort(key=lambda b: (b["period"], b["kind"], -(b.get("shares") or 0), b.get("cik") or ""))
    return totals, breaks


def _from_builder() -> tuple[dict, dict]:
    totals = ownership.sec13f_totals()
    return totals, ownership.sec13f_filer_breaks(list(totals))


def read_totals() -> dict:
    """The totals the board shows: the file when it is current, else the builder's merged over the file's closed
    quarters (nothing written). ``engine.load_totals`` is this."""
    if is_current():
        return stored_totals()
    return merge(stored_totals(), ownership.sec13f_totals(), {}, {})[0]


def read_breaks(tickers) -> dict[str, list[dict]]:
    """The breaks of ``tickers`` the board shows, by the same rule as ``read_totals``."""
    if is_current():
        return stored_breaks(tickers)
    stored = stored_totals()
    return merge(stored, {}, stored_breaks(tickers), ownership.sec13f_filer_breaks(tickers))[1]


def sync() -> tuple[dict, dict, bool]:
    """``(totals, breaks, written)``: bring the file up to the live build when it is not current, then return what
    it holds. Nothing is written when the file is current or there is no ownership store; a closed quarter in the
    file is never rewritten."""
    stamp = source_stamp()
    if stamp is not None and stamp == stored_stamp():
        return stored_totals(), stored_breaks(), False
    src_totals, src_breaks = _from_builder()
    if stamp is None or not src_totals:
        # No build to take: show what the file holds, merged over nothing, and leave it as it is.
        stored = stored_totals()
        return (stored or src_totals), (stored_breaks() if stored else src_breaks), False
    stored = stored_totals()
    totals, breaks = merge(stored, src_totals, stored_breaks(), src_breaks)
    frozen = closed_periods(stored)
    write(totals, breaks, stamp, keep=frozen)
    return totals, breaks, True


def write(totals: dict, breaks: dict, stamp: str, keep: set[str] = frozenset()) -> None:
    """Replace the file's rows of every period not in ``keep`` with ``totals`` and ``breaks``, and set the stamp, in
    one transaction. The rows of a period in ``keep`` (a closed quarter) are left untouched."""
    now = time.time()
    cells = [{"ticker": t, "period": p, **{m: cell.get(m) for m in METRICS}, "complete": bool(cell.get("complete")),
              "closed": cell.get("closed"), "stored_at": now}
             for t, periods in totals.items() for p, cell in periods.items() if p not in keep]
    brk = [{"ticker": t, "period": b["period"], **{k: b.get(k) for k in BREAK_FIELDS}, "stored_at": now}
           for t, rows in breaks.items() for b in rows if b["period"] not in keep]
    kept = sorted(keep)
    marks = ", ".join("?" * len(kept))
    not_kept = f" WHERE period NOT IN ({marks})" if kept else ""

    def _write(tx):
        tx.ensure_table(CELLS)
        tx.ensure_table(BREAKS)
        tx.execute(f"DELETE FROM {CELLS}{not_kept}", kept)
        tx.execute(f"DELETE FROM {BREAKS}{not_kept}", kept)
        tx.write_rows(CELLS, cells)
        tx.write_rows(BREAKS, brk)
        tx.write_rows(META, [{"key": SOURCE_KEY, "value": json.dumps(stamp), "updated_at": now}])

    rawstore.atomic(_write, db=DB)
    logger.info("13F board file: %d cells and %d breaks written, %d closed period(s) kept", len(cells), len(brk),
                len(kept))
