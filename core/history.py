"""Result history writer (#278, schema 11): the one function every board's Fetch hook calls.

A board run writes ONE header row to ``result_runs`` and its rows to its run-keyed tables, in one transaction on
``history.duckdb``: a header without its rows, or rows without a header, cannot exist. Everything that is per run
lives in the header: the session (rule B, ``core.calendar.session_for``), the quality, ``model_version``, params, the
funnel, the board payload. The row tables carry ``run_id`` and the row's own values only.

- Every run gets a header. A run board (Earnings, OIPD, Relatedness, Value Chain, Financials, 13F) keeps every
  run's rows. A session board (the screeners, LEAPS, Rotation, Money Flow; MANIFEST ``"snapshot": "session"``) keeps
  the rows of ONE run per session, the first complete, error-free run recorded (rule B); a later or an intraday run
  of that session records its header only and says why in ``quality_note``. So a session's rows are one whole run,
  never a mix of two.
- Called after the board's latest write (raw.duckdb), never before: history holds what the board showed. DuckDB
  cannot write two files in one transaction (measured on 1.5.5 and 1.5.6), so a failure here leaves the latest
  table in place; the caller logs it as a job note and Data Health counts job runs without a header.
- Fetch only (06 rule 9): a Refresh never calls it.
- Append-only: every table is ``"history": true`` (first write of a key wins; rewrites raise, #286). Recording the
  same ``run_id`` twice writes nothing the second time.
- Read back through ``last_run`` / ``runs`` / ``sessions`` (K2, #383 R1): decoded headers, newest first. A read
  never creates the file: with no recorded run they read as ``None`` / ``[]`` (J3).
- One call at a time (O1, #346). The dashboard's two job lanes are threads of one process (``app/jobs.py``), so two
  Fetches can finish together. DuckDB raises no conflict for two header inserts with different keys, so two
  overlapping runs of one session would both become its snapshot, and two first runs race to create the file's
  tables. ``_RECORD_LOCK`` runs one call per process at a time, up to its commit. Another process cannot overlap:
  DuckDB lets one process open the file at a time. A run waits for a RevenueChaser peer (the reader budget, ~29 s)
  and fails fast on any other holder (``rawstore.StoreLockedError``), with no header and no rows; the runner makes
  that a job note.
"""
from __future__ import annotations

import json
import threading

from RevenueChaser.core import calendar, rawstore

COMPLETE, PARTIAL = "complete", "partial"
_RECORD_LOCK = threading.Lock()


def uw_problems(health: dict | None) -> list[str]:
    """Run-level reasons from a UW client's ``health()`` that a run's inputs are not whole (empty when they are):
    the key was rejected, the client stopped sending requests (breaker open), or some calls failed. The boards'
    own "Data incomplete" banner reads the same health."""
    out, health = [], health or {}
    calls, failed = int(health.get("calls") or 0), int(health.get("failed") or 0)
    auth = health.get("auth_error")
    if auth:
        out.append(f"Unusual Whales rejected the API key (HTTP {auth.get('status', '?')})")
    if health.get("breaker_open"):
        out.append("the Unusual Whales client stopped sending requests after repeated failures "
                   f"({health.get('skipped_calls') or 0} calls skipped)")
    if failed and not auth and not health.get("breaker_open"):
        out.append(f"{failed} of {calls or failed} Unusual Whales calls failed")
    return out


def tables_of(board: str) -> list[str]:
    return sorted(n for n, s in rawstore.MANIFEST.items() if s.get("db") == "history" and s.get("board") == board)


def is_session_board(board: str) -> bool:
    """True for a session board (MANIFEST ``"snapshot": "session"``: one run's rows per session), False for a run
    board (every run's rows) or a board with no history tables."""
    return any(rawstore.MANIFEST[t].get("snapshot") == "session" for t in tables_of(board))


def record_run(board: str, *, run_id: str, run_at: float, model_version: str | None,
               rows: dict[str, list[dict]] | None = None, params: dict | None = None, context: dict | None = None,
               funnel: dict | None = None, payload=None, warnings: list | None = None, partial: str | None = None,
               error: str | None = None, provider_health: dict | list[dict] | str | None = None) -> dict[str, int]:
    """Write one board run: the header and the rows it keeps, all or nothing. Returns rows written per table.

    ``partial`` is the reason the run is incomplete (a degraded pull, a partial chain); the run is kept with quality
    'partial' and is never a session snapshot.

    ``provider_health`` is the ``ProviderHealth.health()`` dict (or one dict per provider) the run's prose warning
    was made from, kept as data in its own JSON column (PH-1). It is stored as given, serialised exactly once: an
    already-encoded JSON string is decoded first, so it is never stored as a string of a string."""
    if isinstance(provider_health, (str, bytes, bytearray)):
        provider_health = json.loads(provider_health)
    rows = rows or {}
    own = set(tables_of(board))
    stray = [t for t in rows if t not in own]
    if stray:
        raise rawstore.RawStoreError(f"{board} does not own {stray}; its history tables are {sorted(own)}")
    try:
        stamp = calendar.session_for(run_at)
        session, quality, note = stamp.session_date, stamp.quality, None
    except ValueError as exc:
        session, quality, note = None, PARTIAL, f"no session: {exc}"
    if partial:
        quality, note = PARTIAL, partial
    per_session = is_session_board(board)

    def _write(tx):
        tx.ensure_table("result_runs")
        if tx.execute("SELECT 1 FROM result_runs WHERE board = ? AND run_id = ?", [board, run_id]).fetchone():
            return {}                                       # recorded already: first write wins, rows included
        keep, snapshot, why = True, None, note
        if per_session:
            taken = tx.execute("SELECT run_id FROM result_runs WHERE board = ? AND session_date = ? "
                               "AND session_snapshot", [board, session]).fetchone() if session else None
            snapshot = quality == COMPLETE and error is None and taken is None
            keep = snapshot
            if not snapshot:
                why = why or ("error" if error else f"{quality} run" if quality != COMPLETE
                              else f"session {session} already recorded by run {taken[0]}")
                why = f"rows not kept ({sum(map(len, rows.values()))} given): {why}"
        counts = {t: len(r) if keep else 0 for t, r in rows.items()}
        tx.write_rows("result_runs", [{
            "board": board, "run_id": run_id, "session_date": session, "run_at": run_at, "quality": quality,
            "quality_note": why, "session_snapshot": snapshot, "model_version": model_version, "params": params,
            "context": context, "funnel": funnel, "row_counts": counts, "warnings": warnings, "error": error,
            "provider_health": provider_health, "payload": payload}])
        if keep:
            for table, trows in rows.items():
                numbered = "row_no" in rawstore.MANIFEST[table]["key"]     # a key-less latest table's mirror
                tx.write_rows(table, [{**r, "run_id": run_id, **({"row_no": i} if numbered else {})}
                                      for i, r in enumerate(trows)])
        return counts

    with _RECORD_LOCK:                        # check and write as one step per process (O1, #346)
        rawstore.ensure_history()             # the first recorded run makes the file, its tables and views
        return rawstore.atomic(_write, db="history")


# ── readers (K2, #383 R1) ────────────────────────────────────────────────────────────────────────────────────────────
# Every header column but the compressed board payload, which a reader asks for (``payload=True``).
_HEADER = ("board, run_id, session_date, run_at, quality, quality_note, session_snapshot, model_version, params, "
           "context, funnel, row_counts, warnings, error")


def _headers(where: str, params: list, *, payload: bool = False, limit: int | None = None) -> list[dict]:
    cols = _HEADER + (", payload" if payload else "")
    tail = f" LIMIT {int(limit)}" if limit is not None else ""
    rows = rawstore.query(f"SELECT {cols} FROM result_runs WHERE {where} ORDER BY run_at DESC, run_id DESC{tail}",
                          params, db="history")
    return [rawstore._decode_row("result_runs", r) for r in rows]


def last_run(board: str, *, payload: bool = False) -> dict | None:
    """The board's newest recorded run header (``context``, ``warnings``, ``quality``, ``quality_note``, ...), decoded;
    with ``payload=True`` also the board payload the Fetch recorded. ``None`` when nothing is recorded."""
    rows = _headers("board = ?", [board], payload=payload, limit=1)
    return rows[0] if rows else None


def runs(board: str, limit: int = 20, *, payload: bool = False) -> list[dict]:
    """The board's last ``limit`` run headers, newest first. A run board keeps every run's rows; on a session board
    this lists every Fetch, header-only runs included."""
    return _headers("board = ?", [board], payload=payload, limit=limit)


def sessions(board: str, limit: int = 10) -> list[dict]:
    """A session board's last ``limit`` session snapshots, newest first: the snapshot run's header plus ``runs``,
    the number of runs that session recorded (header-only runs included). A session with no snapshot (intraday,
    partial or failed runs only) is not listed; a run board has none."""
    snaps = _headers("board = ? AND session_snapshot", [board], limit=limit)
    if not snaps:
        return []
    days = [s["session_date"] for s in snaps]
    counts = {r["session_date"]: int(r["n"]) for r in rawstore.query(
        f"SELECT session_date, count(*) AS n FROM result_runs WHERE board = ? "
        f"AND session_date IN ({', '.join('?' for _ in days)}) GROUP BY session_date", [board, *days], db="history")}
    return [{**s, "runs": counts.get(s["session_date"], 0)} for s in snaps]
