"""Which build of each builder DuckDB this process actually opened, and for which job (#566).

``serving.open_read`` is the one door to a live builder file (``prices.duckdb``, ``fundamentals.duckdb``, ...), so
it notes every open here: when, the file's build (``build_id``, ``built_at``, read from the file's own version row
once per file change, not per open), and who opened it. A board's ``core/history.record_run`` then asks
:func:`reads` what its own job opened, and keeps that with the run (``core/board_sources.py``). Recording what was
opened, not what a board declares, is the review's blocker 1 on PR #568: a declaration is a plan, and several boards
plan reads they do not make.

Who opened it (the scope): inside a dashboard job, its id (``app.jobs.current_job_id``; looked up only when
``app.jobs`` is already imported, so a script never imports the web app for this); otherwise the thread. A board's
worker pool inherits the scope of the thread that made it (``pool_kwargs``, passed to every ``ThreadPoolExecutor`` a
board or ``core`` module starts), so a pooled read counts for its own run and for no other job (delta review of
#568, B and C). A page render's opens are its own thread's and never count for a board.

The build is read from the connection itself on every open (one one-row SELECT): a new read-only connect can still
get the previous build while another handle holds it (DuckDB's per-path instance, S7), so a cache keyed by the
file's ``stat`` could record a build the run did not read (delta review A).

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

_LOCK = threading.Lock()
_LOG: OrderedDict = OrderedDict()              # (scope, kind, build_id) -> (last open t, built_at)
_LOCAL = threading.local()                      # a pool worker's inherited scope


def _job_id() -> str | None:
    jobs = sys.modules.get("RevenueChaser.app.jobs")
    try:
        return jobs.current_job_id() if jobs is not None else None
    except Exception:  # noqa: BLE001 - who opened a file is bookkeeping; it never fails a read
        return None


def scope() -> tuple:
    """``("job", id)`` inside a dashboard job, the inherited scope on a board's pool worker, else
    ``("thread", ident)``."""
    inherited = getattr(_LOCAL, "scope", None)
    if inherited is not None:
        return inherited
    jid = _job_id()
    if jid:
        return ("job", jid)
    return ("thread", threading.get_ident())


def _inherit(sc: tuple) -> None:
    _LOCAL.scope = sc


def pool_kwargs() -> dict:
    """``ThreadPoolExecutor(..., **opens.pool_kwargs())``: the pool's workers read for the caller's run."""
    return {"initializer": _inherit, "initargs": (scope(),)}


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
    """Log one open of ``kind``'s live file at the build this connection reads. Never raises."""
    try:
        build_id, built_at = stamp_of(con, version_table, kind)
        entry = (scope(), kind, build_id)
        with _LOCK:
            _LOG[entry] = (time.time(), built_at)
            _LOG.move_to_end(entry)
            while len(_LOG) > MAX_ENTRIES:
                _LOG.popitem(last=False)
    except Exception:  # noqa: BLE001 - bookkeeping never fails a read
        pass


def reads(of: tuple | None = None) -> dict[str, dict]:
    """``{kind: {build_id: built_at}}`` opened by scope ``of`` (the caller's own by default)."""
    of = of or scope()
    out: dict[str, dict] = {}
    with _LOCK:
        entries = list(_LOG.items())
    for (sc, kind, build_id), (_t, built_at) in entries:
        if sc == of:
            out.setdefault(kind, {})[build_id] = built_at
    return out


def take(of: tuple | None = None) -> dict[str, dict]:
    """:func:`reads`, then forget a thread scope's own opens, so the next run on that thread starts empty. A job's
    opens are kept (a job id is never reused, and the bound drops them): one job can record two boards
    (``module_sector_rotation`` and ``module_money_flow``), and both read the files the job opened."""
    of = of or scope()
    got = reads(of)
    if of[0] == "job":
        return got
    with _LOCK:
        for key in [k for k in _LOG if k[0] == of]:
            del _LOG[key]
    return got


def clear() -> None:
    """Forget everything (tests)."""
    with _LOCK:
        _LOG.clear()
