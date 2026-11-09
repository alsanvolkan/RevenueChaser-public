"""Which build of each builder DuckDB this process actually opened, and for which job (#566).

``serving.open_read`` is the one door to a live builder file (``prices.duckdb``, ``fundamentals.duckdb``, ...), so
it notes every open here: when, the file's build (``build_id``, ``built_at``, read from the file's own version row
once per file change, not per open), and who opened it. A board's ``core/history.record_run`` then asks
:func:`reads` what its own job opened, and keeps that with the run (``core/board_sources.py``). Recording what was
opened, not what a board declares, is the review's blocker 1 on PR #568: a declaration is a plan, and several boards
plan reads they do not make.

Who opened it (the scope):

- a dashboard job: its id (``app.jobs.current_job_id``; looked up only when ``app.jobs`` is already imported, so a
  script never imports the web app for this);
- otherwise the thread. A board's helper threads (``ThreadPoolExecutor`` workers) carry no job id, so their opens
  are attributed by time: a helper open after the job started counts for the job. Two jobs whose helpers read at
  the same time can then see each other's opens; a page render's opens never count (its thread is not a pool
  worker).

The log keeps one entry per (scope, file, build) with the time of its newest open, so a board that opens a file
a thousand times costs one entry; it is bounded (``MAX_ENTRIES``, oldest first), and a scope is dropped once
:func:`take` has read it.
"""
from __future__ import annotations

import sys
import threading
import time
from collections import OrderedDict

import duckdb

#: (scope, file, build) entries kept in memory; the least recently opened fall off first.
MAX_ENTRIES = 5_000
#: A thread whose opens count for the job that started before them (a board's worker pool).
HELPER_PREFIXES = ("ThreadPoolExecutor",)

_LOCK = threading.Lock()
_LOG: OrderedDict = OrderedDict()              # (scope, kind, build_id) -> (last open t, built_at)
_STAMPS: dict[tuple, tuple] = {}                # (kind, ino, mtime_ns, size) -> (build_id, built_at)


def _job_id() -> str | None:
    jobs = sys.modules.get("RevenueChaser.app.jobs")
    try:
        return jobs.current_job_id() if jobs is not None else None
    except Exception:  # noqa: BLE001 - who opened a file is bookkeeping; it never fails a read
        return None


def scope() -> tuple:
    """``("job", id)`` inside a dashboard job, ``("helper", ident)`` on a pool worker, else ``("thread", ident)``."""
    jid = _job_id()
    if jid:
        return ("job", jid)
    t = threading.current_thread()
    return ("helper" if t.name.startswith(HELPER_PREFIXES) else "thread", t.ident)


def stamp_of(con: duckdb.DuckDBPyConnection, version_table: str, kind: str) -> tuple:
    """``(build_id, built_at)`` from an open file's version row; ``(None, None)`` when it has none."""
    try:
        cols = {r[0] for r in con.execute(f"DESCRIBE {version_table}").fetchall()}
        if not {"build_id", "built_at"} <= cols:
            return (None, None)
        rows = con.execute(f"SELECT build_id, built_at FROM {version_table} WHERE kind = ?", (kind,)).fetchall()
    except duckdb.Error:
        return (None, None)
    if len(rows) != 1:
        return (None, None)
    return (rows[0][0], float(rows[0][1]) if rows[0][1] is not None else None)


def note(kind: str, path, con: duckdb.DuckDBPyConnection, version_table: str) -> None:
    """Log one open of ``kind``'s live file. Never raises."""
    try:
        st = path.stat()
        key = (kind, st.st_ino, st.st_mtime_ns, st.st_size)
        with _LOCK:
            stamp = _STAMPS.get(key)
        if stamp is None:
            stamp = stamp_of(con, version_table, kind)
            with _LOCK:
                if len(_STAMPS) > 256:
                    _STAMPS.clear()
                _STAMPS[key] = stamp
        entry = (scope(), kind, stamp[0])
        with _LOCK:
            _LOG[entry] = (time.time(), stamp[1])
            _LOG.move_to_end(entry)
            while len(_LOG) > MAX_ENTRIES:
                _LOG.popitem(last=False)
    except Exception:  # noqa: BLE001 - bookkeeping never fails a read
        pass


def reads(of: tuple | None = None, since: float | None = None) -> dict[str, dict]:
    """``{kind: {build_id: built_at}}`` opened by scope ``of`` (the caller's own by default), plus pool-worker opens at
    or after ``since`` when given."""
    of = of or scope()
    out: dict[str, dict] = {}
    with _LOCK:
        entries = list(_LOG.items())
    for (sc, kind, build_id), (t, built_at) in entries:
        if sc == of or (since is not None and sc[0] == "helper" and t >= since):
            out.setdefault(kind, {})[build_id] = built_at
    return out


def take(of: tuple | None = None, since: float | None = None) -> dict[str, dict]:
    """:func:`reads`, then forget a thread scope's own opens, so the next run on that thread starts empty. A job's
    opens are kept (a job id is never reused, and the bound drops them): one job can record two boards
    (``module_sector_rotation`` and ``module_money_flow``), and both read the files the job opened."""
    of = of or scope()
    got = reads(of, since)
    if of[0] == "job":
        return got
    with _LOCK:
        for key in [k for k in _LOG if k[0] == of]:
            del _LOG[key]
    return got


def job_started_at() -> float | None:
    """Epoch start of the dashboard job running on this thread, or None."""
    jobs = sys.modules.get("RevenueChaser.app.jobs")
    jid = _job_id()
    if jobs is None or not jid:
        return None
    try:
        job = jobs.get(jid) or {}
        started = job.get("started_at")
        return jobs._epoch(started) if started else None
    except Exception:  # noqa: BLE001
        return None


def clear() -> None:
    """Forget everything (tests)."""
    with _LOCK:
        _LOG.clear()
        _STAMPS.clear()
