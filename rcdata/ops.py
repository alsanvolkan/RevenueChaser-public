"""The market store's operational state (D6): ``ops.sqlite``, or Redis when ``RC_REDIS_URL`` is set (Order 3).

**Two backends, one surface.** With ``RC_REDIS_URL`` set, every public function here reads and writes Redis
(``rcdata/ops_redis.py``: the same tables as rows, the same answers, kept by the Redis container's AOF). Unset, they
use ``ops.sqlite`` as described below. The switch is the environment, read on every call, so going back is unsetting
the variable and restarting: the file is never written while Redis is on. ``ops.sqlite`` stays only as that rollback
path until it is retired; ``rcdata/ops_copy.py`` copies it into Redis once.

SQLite in WAL mode, beside the serving files (``config.MARKET_DIR``), because several processes use it at once: the
dashboard enqueues build requests and reads the check rows, the builder takes requests and writes runs, checks and
call counts. WAL lets a reader go on while a writer writes; ``busy_timeout`` (set on every open, and the only busy
rule: the connection's own ``timeout`` is 0) makes a second writer wait up to 5 s instead of failing with "database
is locked".

The tables and what writes them:

- ``build_requests``: the queue. A page or the CLI enqueues; the builder takes the oldest queued row (FIFO).
- ``dataset_check``: one row per dataset run (changed / unchanged / failed / skipped), the status a read returns.
- ``build_issues``: a row the builder could not use, with its key, until someone resolves it.
- ``job_runs2``: one row per builder run.
- ``read_log``: reads per day, process and table; counted in memory and flushed once a minute.
- ``uw_remaining``: UW's rate headers, sampled once a run: the daily count UW reports, what is left of our own cap
  (computed), the per-minute window, and the token limit as sent (not a daily cap).
- ``provider_calls_daily``: calls per provider and New York day; the day cap (``caps``) reads it.
- ``watchlist_option`` / ``watchlist_stock``: the two watch lists (D2), one row per ticker with when, by which board
  and an optional note; ``watchlist_*_changes`` is their history, one row per add or remove. The dashboard's toggle
  routes write them (PR 4); the option list is what the options build will cover besides SPX.
- ``dataset_schedule``: cadence and pause state per dataset.
- ``kind_automation``: what the hourly pass queues on its own for each serving kind (``KIND_MODES``); no row is off.

``ensure()`` creates the tables and stamps ``ops_schema``; it upgrades an older file in place and refuses a file
stamped by a newer version.
"""
from __future__ import annotations

import atexit
import functools
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import closing, contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import urlsplit

from ..core import calendar, config

OPS_SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5000
#: How long a thread waits for another thread of the same process to finish its write transaction (``_tx``). The
#: threads queue on ``_WRITE_LOCK`` and are woken when it is released, so this is patience for a stuck transaction,
#: not a share of ``busy_timeout``, which is now only what a writer waits for another process (#522).
THREAD_WRITE_WAIT_S = 60.0
FILE_NAME = "ops.sqlite"

REQUEST_STATUSES = ("queued", "running", "done", "failed", "skipped")
CHECK_RESULTS = ("changed", "unchanged", "failed", "skipped")
WATCH_KINDS = ("option", "stock")
#: What the hourly pass queues for a kind's due datasets (avo, 6 Nov 2026): nothing, a raw-only fetch, raw + Parquet,
#: or the whole run up to the served DuckDB file. A kind with no ``kind_automation`` row is off.
KIND_MODES = ("off", "raw", "parquet", "full")
#: The board's two switches (#527, avo 6 Nov 2026): ``fetch`` stops every fetch and build, ``backup`` every backup.
#: A switch with no ``switch`` row, or with ``off_at`` NULL, is on.
SWITCHES = ("fetch", "backup")
#: One switch per served DuckDB file (avo, 10 Nov 2026: "main > midswitch > subswitches", and "Fetch de dursun"):
#: ``file-<kind>`` off holds every dataset of that file, on its own or pressed by hand, under the fetch switch and
#: over each dataset's own. The kinds are ``serving.KINDS`` (not imported here: serving imports duckdb).
FILE_KINDS = ("market", "spine", "ownership", "prices", "fundamentals", "options", "sec_forms")
FILE_SWITCHES = tuple(f"file-{k}" for k in FILE_KINDS)
# avo, 8 Nov 2026 ("B", #387): a resolved build issue is kept seven days; an open one until it is resolved.
RESOLVED_ISSUE_DAYS = 7
# The UW response headers sampled into uw_remaining: only UW's own, never a cookie or anything else the answer had.
UW_HEADER_PREFIX = "x-uw-"
# What a live answer carries (27 Oct 2026): x-uw-daily-req-count is today's count; the per-minute pair is a rate
# window; x-uw-token-req-limit (100,000,000) is NOT the daily cap. The day's remaining is computed from our own cap
# (rcdata/caps.py) and labelled as such.
UW_DAILY_COUNT_HEADER = "x-uw-daily-req-count"
UW_MINUTE_REMAINING_HEADER = "x-uw-req-per-minute-remaining"
UW_MINUTE_RESET_HEADER = "x-uw-req-per-minute-reset"
UW_TOKEN_LIMIT_HEADER = "x-uw-token-req-limit"
READ_LOG_FLUSH_S = 60.0
#: The per-request log (#715): one row a read, a need ask and a provider call, newest ``REQUEST_LOG_KEEP`` kept
#: (about seven days at the volume measured on 12 Nov 2026: some 8,000 reads and 9,000 calls a day).
REQUEST_KINDS = ("read", "need", "call")
REQUEST_LOG_KEEP = 100_000
#: Rows a process holds between two flushes; past it the oldest are dropped and counted, never the read failed.
REQUEST_LOG_BUFFER = 5_000
REQUEST_WHAT_MAX = 200
#: ``RC_REQUEST_LOG=0`` turns the per-request log off (the daily counters go on): the rollback without a revert.
REQUEST_LOG_ENV = "RC_REQUEST_LOG"
# The Redis that holds this state instead of ops.sqlite (redis://host:port/db). Unset: ops.sqlite.
REDIS_URL_ENV = "RC_REDIS_URL"

_DDL = {
    "ops_schema": "CREATE TABLE IF NOT EXISTS ops_schema (version INTEGER NOT NULL, applied_at REAL NOT NULL)",
    "build_requests": """CREATE TABLE IF NOT EXISTS build_requests (
        id INTEGER PRIMARY KEY, dataset TEXT NOT NULL, requested_at REAL NOT NULL, requested_by TEXT NOT NULL,
        params TEXT, status TEXT NOT NULL CHECK (status IN ('queued','running','done','failed','skipped')),
        started_at REAL, ended_at REAL, note TEXT)""",
    "dataset_check": """CREATE TABLE IF NOT EXISTS dataset_check (
        id INTEGER PRIMARY KEY, dataset TEXT NOT NULL, checked_on TEXT NOT NULL,
        result TEXT NOT NULL CHECK (result IN ('changed','unchanged','failed','skipped')),
        source_as_of TEXT, rows INTEGER, added INTEGER, removed INTEGER, coverage REAL, calls INTEGER,
        next_check_due TEXT, run_id TEXT, note TEXT)""",
    "build_issues": """CREATE TABLE IF NOT EXISTS build_issues (
        id INTEGER PRIMARY KEY, dataset TEXT NOT NULL, found_on TEXT NOT NULL, kind TEXT NOT NULL, key TEXT,
        detail TEXT, run_id TEXT, resolved_on TEXT)""",
    "job_runs2": """CREATE TABLE IF NOT EXISTS job_runs2 (
        run_id TEXT PRIMARY KEY, dataset TEXT, trigger TEXT, pid INTEGER, host TEXT, started_at REAL,
        finished_at REAL, status TEXT, params TEXT, error TEXT)""",
    "read_log": """CREATE TABLE IF NOT EXISTS read_log (
        day TEXT NOT NULL, process TEXT NOT NULL, table_name TEXT NOT NULL, reads INTEGER NOT NULL,
        PRIMARY KEY (day, process, table_name))""",
    "uw_remaining": """CREATE TABLE IF NOT EXISTS uw_remaining (
        sampled_at REAL PRIMARY KEY, run_id TEXT, daily_req_count INTEGER, daily_cap INTEGER,
        daily_remaining_computed INTEGER, per_minute_remaining INTEGER, per_minute_reset_ms INTEGER,
        token_req_limit INTEGER, raw_headers TEXT)""",
    "provider_calls_daily": """CREATE TABLE IF NOT EXISTS provider_calls_daily (
        provider TEXT NOT NULL, ny_day TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0, http_429 INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (provider, ny_day))""",
    **{f"watchlist_{k}": f"""CREATE TABLE IF NOT EXISTS watchlist_{k} (
        ticker TEXT PRIMARY KEY, added_at REAL NOT NULL, added_by TEXT, note TEXT)""" for k in WATCH_KINDS},
    # Version 2: an ``id`` that keeps the history's order (a bare rowid may be renumbered by VACUUM), and the note.
    **{f"watchlist_{k}_changes": f"""CREATE TABLE IF NOT EXISTS watchlist_{k}_changes (
        id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, action TEXT NOT NULL CHECK (action IN ('add','remove')),
        at REAL NOT NULL, by TEXT, note TEXT)""" for k in WATCH_KINDS},
    "dataset_schedule": """CREATE TABLE IF NOT EXISTS dataset_schedule (
        dataset TEXT PRIMARY KEY, cadence TEXT, next_due TEXT, paused_at REAL, paused_by TEXT)""",
    # A new table, no upgrade step: ``ensure`` makes it in an older file too, and an older checkout ignores it.
    "kind_automation": """CREATE TABLE IF NOT EXISTS kind_automation (
        kind TEXT PRIMARY KEY, mode TEXT NOT NULL, set_at REAL, set_by TEXT)""",
    # #527, a new table as kind_automation was: no upgrade step.
    "switch": """CREATE TABLE IF NOT EXISTS switch (
        name TEXT PRIMARY KEY, off_at REAL, off_by TEXT)""",
    # #527: one source's own automation, over its kind's; no row follows the kind. A new table: no upgrade step.
    "dataset_automation": """CREATE TABLE IF NOT EXISTS dataset_automation (
        dataset TEXT PRIMARY KEY, mode TEXT NOT NULL, set_at REAL, set_by TEXT)""",
    # #715, the per-request log. A new table, no upgrade step; trimmed to its newest REQUEST_LOG_KEEP rows by every
    # write, and never part of a backup (a log, not state).
    "request_log": """CREATE TABLE IF NOT EXISTS request_log (
        id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, process TEXT NOT NULL, what TEXT NOT NULL,
        dataset TEXT, result TEXT, ms INTEGER)""",
}
TABLES = tuple(_DDL)
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS build_requests_status ON build_requests (status, id)",
    "CREATE INDEX IF NOT EXISTS dataset_check_dataset ON dataset_check (dataset, id)",
    *(f"CREATE INDEX IF NOT EXISTS watchlist_{k}_changes_ticker ON watchlist_{k}_changes (ticker, id)"
      for k in WATCH_KINDS),
)


class OpsSchemaTooNew(RuntimeError):
    """ops.sqlite was stamped by a newer rcdata: this checkout must not write to it."""


class OpsNotCopied(RuntimeError):
    """Redis is on, its namespace was never written, and ``ops.sqlite`` beside it still holds rows: starting an empty
    store would read today's calls as 0 and the watch lists as empty. ``rcdata.ops_copy`` is the way in."""


def backend() -> str:
    """``"redis"`` while ``RC_REDIS_URL`` is set, else ``"sqlite"``; read on every call, like ``ops_path``."""
    return "redis" if os.getenv(REDIS_URL_ENV, "").strip() else "sqlite"


def _routed(fn):
    """Run ``fn``'s namesake in ``ops_redis`` while the Redis backend is on: same name, same arguments, same answer."""
    @functools.wraps(fn)
    def call(*args, **kwargs):
        if backend() == "redis":
            from . import ops_redis
            return getattr(ops_redis, fn.__name__)(*args, **kwargs)
        return fn(*args, **kwargs)
    return call


def _store_errors() -> tuple[type[BaseException], ...]:
    try:
        import redis
    except ImportError:                                   # SQLite only: redis-py is not needed to run on the file
        return ()
    return (redis.RedisError,)


# What a caller catches for "the ops store could not answer now" (locked past busy_timeout, Redis unreachable or
# timed out), and for any ops store error.
BUSY_ERRORS: tuple[type[BaseException], ...] = (sqlite3.OperationalError, *_store_errors())
STORE_ERRORS: tuple[type[BaseException], ...] = (sqlite3.Error, *_store_errors())


def ops_path() -> Path:
    """``<MARKET_DIR>/ops.sqlite``, resolved at call time (so a moved root counts at once)."""
    return Path(config.MARKET_DIR) / FILE_NAME


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """The one connection factory: WAL, ``busy_timeout``, autocommit (statements commit alone; ``_tx`` groups).

    Closed on exit. ``timeout=0`` on purpose: the PRAGMA is the busy rule, so dropping it cannot go unnoticed.
    """
    path = Path(path or ops_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=0, isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        con.execute("PRAGMA journal_mode = WAL")
        yield con
    finally:
        con.close()


# One write transaction at a time per process (#522). SQLite's busy handler polls with growing sleeps (up to 100 ms)
# and keeps no queue, so n threads of one process racing for the write lock spend their ``busy_timeout`` in idle
# sleeps and behind each other: on a loaded box the last one ran out of it and failed with "database is locked"
# (``test_same_ticker_concurrent_toggles``, run on main 6 Nov 2026). Threads now queue here and are woken in turn;
# ``busy_timeout`` is left for a writer in another process (the builder against the dashboard).
_WRITE_LOCK = threading.RLock()


@contextmanager
def _tx(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One write transaction, taken at once (``BEGIN IMMEDIATE``), so a read-then-write cannot race.

    Held under ``_WRITE_LOCK``, so the threads of this process take turns instead of polling SQLite's lock."""
    if not _WRITE_LOCK.acquire(timeout=THREAD_WRITE_WAIT_S):
        raise sqlite3.OperationalError(
            f"database is locked: another thread of this process held the ops write lock for {THREAD_WRITE_WAIT_S:g} s")
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
    finally:
        _WRITE_LOCK.release()


_ENSURED: set[str] = set()
_ENSURE_LOCK = threading.Lock()


def _upgrade_to_2(con: sqlite3.Connection) -> None:
    """v1 -> v2: each ``watchlist_*_changes`` gets ``id INTEGER PRIMARY KEY`` and ``note``; its rows are copied in
    the order they were written. The list tables themselves are unchanged.

    A table that already has ``id`` is left as it is: a v2 file stamped back down to 1 (the rollback) keeps its v2
    tables, and the next deploy must not copy them again, which would drop their notes."""
    for k in WATCH_KINDS:
        name = f"watchlist_{k}_changes"
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone():
            continue
        if "id" in {r[1] for r in con.execute(f"PRAGMA table_info({name})")}:
            continue
        con.execute(f"ALTER TABLE {name} RENAME TO {name}_v1")
        con.execute(_DDL[name])
        con.execute(f"INSERT INTO {name} (ticker, action, at, by) SELECT ticker, action, at, by FROM {name}_v1 "
                    "ORDER BY rowid")
        con.execute(f"DROP TABLE {name}_v1")


# The step that brings a file stamped ``n - 1`` to ``n``; ``ensure`` runs every step past the stamp it finds.
_UPGRADES: dict[int, Callable[[sqlite3.Connection], None]] = {2: _upgrade_to_2}


@_routed
def ensure(path: Path | None = None) -> None:
    """Create every table (idempotent) and stamp ``ops_schema``; raise ``OpsSchemaTooNew`` on a newer stamp.

    A file stamped older is upgraded in place, in the same transaction, one ``_UPGRADES`` step per version, before
    the tables and indexes of this version are made; a failed step rolls the whole file back to what it was.
    """
    path = Path(path or ops_path())
    with _ENSURE_LOCK:
        with connect(path) as con, _tx(con):
            con.execute(_DDL["ops_schema"])
            found = con.execute("SELECT max(version) FROM ops_schema").fetchone()[0]
            if found is not None and found > OPS_SCHEMA_VERSION:
                raise OpsSchemaTooNew(f"{path} is ops schema {found}; this checkout knows {OPS_SCHEMA_VERSION}")
            for version in range((found or OPS_SCHEMA_VERSION) + 1, OPS_SCHEMA_VERSION + 1):
                _UPGRADES[version](con)
            for ddl in _DDL.values():
                con.execute(ddl)
            for ddl in _INDEXES:
                con.execute(ddl)
            if found != OPS_SCHEMA_VERSION:
                con.execute("INSERT INTO ops_schema (version, applied_at) VALUES (?, ?)",
                            (OPS_SCHEMA_VERSION, time.time()))
        # After the COMMIT: ``_open`` skips ``ensure`` for a marked file, so a mark made inside the transaction let
        # another thread read the file before its tables were committed (CI on PR 251, 28 Oct 2026).
        _ENSURED.add(str(path))


@contextmanager
def _open() -> Iterator[sqlite3.Connection]:
    """A connection to the current ops file, its tables made on this process's first use of that file."""
    path = ops_path()
    if str(path) not in _ENSURED or not path.exists():
        ensure(path)
    with connect(path) as con:
        yield con


def _today() -> str:
    return calendar.today_ny().isoformat()


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


# ── The request queue ──────────────────────────────────────────────────────────────────────────────────────────

@_routed
def enqueue(dataset: str, params: dict | None = None, *, by: str) -> int:
    """Queue one build of ``dataset``; returns the request id. The only write a page ever makes (F7 A)."""
    with _open() as con:
        cur = con.execute("INSERT INTO build_requests (dataset, requested_at, requested_by, params, status) "
                          "VALUES (?, ?, ?, ?, 'queued')", (dataset, time.time(), by, _json(params)))
        return int(cur.lastrowid)


@_routed
def enqueue_once(dataset: str, params: dict | None = None, *, by: str) -> int | None:
    """Queue one build of ``dataset`` unless one is already queued or running; the request id, or None.

    The check and the insert are one ``BEGIN IMMEDIATE`` transaction, so two clicks cannot both get through.
    """
    with _open() as con, _tx(con):
        busy = con.execute("SELECT count(*) FROM build_requests WHERE dataset = ? AND status IN ('queued','running')",
                           (dataset,)).fetchone()[0]
        if busy:
            return None
        cur = con.execute("INSERT INTO build_requests (dataset, requested_at, requested_by, params, status) "
                          "VALUES (?, ?, ?, ?, 'queued')", (dataset, time.time(), by, _json(params)))
        return int(cur.lastrowid)


@_routed
def queue_view(finished: int = 20) -> dict:
    """``{"active": queued and running rows, oldest first; "finished": the newest ``finished`` others}``."""
    with _open() as con:
        active = con.execute("SELECT * FROM build_requests WHERE status IN ('queued','running') ORDER BY id").fetchall()
        done = con.execute("SELECT * FROM build_requests WHERE status NOT IN ('queued','running') "
                           "ORDER BY id DESC LIMIT ?", (int(finished),)).fetchall()
    return {"active": [_request(r) for r in active], "finished": [_request(r) for r in done]}


#: A request carrying any of these builds no serving file (``--no-duckdb``, ``--no-parquet``, the old ``--no-build``).
FETCH_ONLY_PARAMS = ("no_duckdb", "no_parquet", "no_build")


def is_fetch_only(params) -> bool:
    """True for a request's params (a dict, its JSON text, or None) that build no serving file."""
    if isinstance(params, str):
        params = json.loads(params) if params else None
    return any((params or {}).get(k) for k in FETCH_ONLY_PARAMS)


def _request(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["params"] = json.loads(out["params"]) if out["params"] else None
    return out


@_routed
def next_request(dataset: str | None = None, *, skip: Iterable[str] = (),
                 only: Iterable[str] | None = None, by: str | None = None,
                 fetch_only: bool = False) -> dict | None:
    """Take the oldest queued request (of ``dataset``, when given) and mark it running; None when none is queued.

    ``skip`` leaves those datasets' requests in the queue, and ``only`` claims nothing else: that is how a request
    waits for the runner that may run it instead of being consumed by the one that may not. The hourly builder
    skips the attended datasets (a person logs in to WRDS), the attended runner claims only them. The two are
    complements, so asking for both at once is a mistake, not an intersection.

    ``by`` claims only what that requester queued. A person's ``backfill <attended>`` uses it so their run is
    their own: a request a page left for the attended runner is not theirs to spend the WRDS login and the day
    cap on (review SHOULD-2).

    ``fetch_only`` claims only requests that build no serving file (``FETCH_ONLY_PARAMS``): a fetch-only process
    holds no builder lock, so an ordinary request it drained would build and swap beside the real builder (#502).
    """
    skip = tuple(skip)
    if skip and only is not None:
        raise ValueError("next_request takes skip or only, not both")
    with _open() as con, _tx(con):
        sql = "SELECT id FROM build_requests WHERE status = 'queued'"
        args: tuple = ()
        if dataset is not None:
            sql += " AND dataset = ?"
            args = (dataset,)
        if skip:
            sql += f" AND dataset NOT IN ({', '.join('?' * len(skip))})"
            args += skip
        if by is not None:
            sql += " AND requested_by = ?"
            args += (by,)
        if fetch_only:
            sql += " AND (" + " OR ".join(f"coalesce(json_extract(params, '$.{k}'), 0)" for k in FETCH_ONLY_PARAMS) + ")"
        if only is not None:
            names = tuple(only)
            if not names:
                return None                     # "only these" with no name is nothing to claim, not
                #                                 everything. SQLite reads IN () as false too, so this is the
                #                                 explicit form of behaviour the SQL already has.
            sql += f" AND dataset IN ({', '.join('?' * len(names))})"
            args += names
        row = con.execute(sql + " ORDER BY id LIMIT 1", args).fetchone()
        if row is None:
            return None
        con.execute("UPDATE build_requests SET status = 'running', started_at = ? WHERE id = ?", (time.time(), row[0]))
        return _request(con.execute("SELECT * FROM build_requests WHERE id = ?", (row[0],)).fetchone())


@_routed
def queued(dataset: str, *, by: str | None = None) -> bool:
    """True while ``dataset`` has a request waiting; with ``by``, one that requester queued."""
    sql = "SELECT count(*) FROM build_requests WHERE status = 'queued' AND dataset = ?"
    args: tuple = (dataset,)
    if by is not None:
        sql += " AND requested_by = ?"
        args += (by,)
    with _open() as con:
        return con.execute(sql, args).fetchone()[0] > 0


@_routed
def finish_request(request_id: int, status: str, note: str | None = None) -> None:
    if status not in REQUEST_STATUSES:
        raise ValueError(f"unknown request status {status!r}")
    with _open() as con:
        con.execute("UPDATE build_requests SET status = ?, ended_at = ?, note = ? WHERE id = ?",
                    (status, time.time(), note, request_id))


def _builder_alive(pid: int | None) -> bool:
    """True while ``pid`` is a live builder: its ``.rc-proc-<pid>`` marker is in the market dir and names this very
    process (``core/procs.pid_alive`` plus its start time, so a reused pid does not read as the builder)."""
    if not pid or pid == os.getpid():
        return False
    from ..core import rawstore
    return pid in rawstore._peer_pids(Path(config.MARKET_DIR) / FILE_NAME)


@_routed
def fail_orphans(*, dead_pids_only: bool = False) -> tuple[int, int]:
    """Reclaim every request and run a dead builder left running: ``(requests, runs)`` failed.

    Called by the builder at start, while it holds the ``rcdata_build`` lock. A request's pid is its run's (the
    ``job_runs2`` row of the same dataset that started first at or after the request was taken). A request or run
    whose builder is still alive is left alone; any other becomes ``failed`` with the note ``lost: pid <n> gone``
    (``lost: pid unknown`` when the builder died before its run row), and each reclaimed request gets a ``failed``
    ``dataset_check`` row with that note, so the dataset's card shows the lost run.

    ``dead_pids_only`` (#602, ``fail_dead_requests``): a caller that is not the builder and holds no build lock.
    Only a run whose own pid names no process counts as dead, and a request with no run row yet is left alone.
    """
    me = os.getpid()
    now, tail = time.time(), _orphan_tail(me, dead_pids_only)
    with _open() as con, _tx(con):
        runs = [dict(r) for r in con.execute("SELECT run_id, dataset, pid, host, started_at FROM job_runs2 "
                                             "WHERE status = 'running' AND pid IS NOT ? ORDER BY started_at",
                                             (me,))]
        alive = {r["run_id"]: _run_alive(r["pid"], dead_pids_only, r["host"]) for r in runs}
        n_reqs = 0
        for req in con.execute("SELECT id, dataset, started_at FROM build_requests WHERE status = 'running' "
                               "ORDER BY id").fetchall():
            run = next((r for r in runs if r["dataset"] == req["dataset"]
                        and (r["started_at"] or 0) >= (req["started_at"] or 0)), None)
            if (run is None and dead_pids_only) or (run is not None and alive[run["run_id"]]):
                continue
            note = (f"lost: pid {run['pid']} gone" if run is not None else "lost: pid unknown (no run row)")
            note += f" ({tail})"
            con.execute("UPDATE build_requests SET status = 'failed', ended_at = ?, note = ? WHERE id = ?",
                        (now, note, req["id"]))
            con.execute("INSERT INTO dataset_check (dataset, checked_on, result, run_id, note) "
                        "VALUES (?, ?, 'failed', ?, ?)",
                        (req["dataset"], _today(), run["run_id"] if run else None, note))
            n_reqs += 1
        n_runs = 0
        for run in runs:
            if alive[run["run_id"]]:
                continue
            con.execute("UPDATE job_runs2 SET status = 'failed', finished_at = ?, error = ? WHERE run_id = ?",
                        (now, f"lost: pid {run['pid']} gone ({tail})", run["run_id"]))
            n_runs += 1
    return n_reqs, n_runs


def _orphan_tail(me: int, dead_pids_only: bool) -> str:
    return (f"its process is gone; found running by the queue read of pid {me}" if dead_pids_only
            else f"builder died; found running by the builder started as pid {me}")


def _run_alive(pid: int | None, dead_pids_only: bool, host: str | None = None) -> bool:
    """The builder's start asks whether the run's builder is alive; a reader asks only whether its pid is gone.

    A reader never judges a run another host started (#611): that pid names nothing here. A row with no host (written
    before the column was filled) is taken as this host's."""
    if dead_pids_only:
        import socket
        from ..core import procs
        if host and host != socket.gethostname():
            return True
        return not pid or procs.pid_alive(pid)
    return _builder_alive(pid)


def fail_dead_requests() -> tuple[int, int]:
    """#602: close the requests and runs whose process is gone (SIGKILL, OOM, a power cut) without waiting for the
    next builder start, which may be days away while the builder timer is off. Safe without the build lock: it acts
    only on a run whose own pid is dead."""
    return fail_orphans(dead_pids_only=True)


# ── Checks, issues, runs ───────────────────────────────────────────────────────────────────────────────────────

CHECK_FIELDS = ("dataset", "checked_on", "result", "source_as_of", "rows", "added", "removed", "coverage", "calls",
                "next_check_due", "run_id", "note")


@_routed
def record_check(dataset: str, result: str, *, source_as_of: str | None = None, rows: int | None = None,
                 added: int | None = None, removed: int | None = None, coverage: float | None = None,
                 calls: int | None = None, next_check_due: str | None = None, run_id: str | None = None,
                 note: str | None = None, checked_on: str | None = None) -> int:
    """One ``dataset_check`` row; ``checked_on`` defaults to today in New York."""
    if result not in CHECK_RESULTS:
        raise ValueError(f"unknown check result {result!r}")
    values = (dataset, checked_on or _today(), result, None if source_as_of is None else str(source_as_of), rows,
              added, removed, coverage, calls, next_check_due, run_id, note)
    with _open() as con:
        cur = con.execute(f"INSERT INTO dataset_check ({', '.join(CHECK_FIELDS)}) "
                          f"VALUES ({', '.join('?' * len(CHECK_FIELDS))})", values)
        return int(cur.lastrowid)


@_routed
def latest_check(dataset: str) -> dict | None:
    """The newest ``dataset_check`` row of ``dataset`` (by insertion), or None."""
    with _open() as con:
        row = con.execute("SELECT * FROM dataset_check WHERE dataset = ? ORDER BY id DESC LIMIT 1",
                          (dataset,)).fetchone()
    return dict(row) if row else None


@_routed
def record_issue(dataset: str, kind: str, key: str | None, detail: str | None, run_id: str | None = None) -> int:
    with _open() as con:
        cur = con.execute("INSERT INTO build_issues (dataset, found_on, kind, key, detail, run_id) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (dataset, _today(), kind, key, detail, run_id))
        return int(cur.lastrowid)


@_routed
def sync_issues(dataset: str, kinds, found, run_id: str | None = None, keep=None) -> tuple[int, int]:
    """Make the open ``build_issues`` of ``dataset`` and ``kinds`` the rows ``found`` (``(kind, key, detail)``).

    A found row that is already open stays as it is (its first ``found_on`` and ``run_id``); a new one is added;
    an open row of those kinds that is no longer found gets ``resolved_on`` today, unless ``keep(kind, key)`` says
    the build did not look where it was found (``prices``' build on the live copy, #446). Rows of other kinds or
    datasets are not touched. One transaction. Returns ``(added, resolved)``.
    """
    kinds = sorted(set(kinds))
    want = {(k, key): detail for k, key, detail in found}
    stray = sorted({k for k, _ in want} - set(kinds))
    if stray:
        raise ValueError(f"found rows of kinds outside {kinds}: {stray}")
    if not kinds:
        return 0, 0
    today = _today()
    with _open() as con, _tx(con):
        marks = ", ".join("?" * len(kinds))
        open_rows = con.execute(f"SELECT id, kind, key FROM build_issues WHERE dataset = ? AND resolved_on IS NULL "
                                f"AND kind IN ({marks})", (dataset, *kinds)).fetchall()
        # Every row of a key, not one: ``record_issue`` writes a row per run, so one unfixed problem becomes
        # many rows with the same key (FRED's missing key left six), and keeping all but one open means the
        # health screen never stops claiming a problem that is over.
        have: dict[tuple, list[int]] = {}
        for r in open_rows:
            have.setdefault((r["kind"], r["key"]), []).append(r["id"])
        gone = [i for k, ids in have.items() if k not in want and not (keep is not None and keep(*k)) for i in ids]
        con.executemany("UPDATE build_issues SET resolved_on = ? WHERE id = ?", [(today, i) for i in gone])
        new = [(dataset, today, k, key, detail, run_id) for (k, key), detail in sorted(want.items())
               if (k, key) not in have]
        con.executemany("INSERT INTO build_issues (dataset, found_on, kind, key, detail, run_id) "
                        "VALUES (?, ?, ?, ?, ?, ?)", new)
    return len(new), len(gone)


@_routed
def start_run(run_id: str, dataset: str, trigger: str, params: dict | None = None) -> None:
    import socket
    with _open() as con:
        con.execute("INSERT INTO job_runs2 (run_id, dataset, trigger, pid, host, started_at, status, params) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
                    (run_id, dataset, trigger, os.getpid(), socket.gethostname(), time.time(), _json(params)))


@_routed
def running_runs() -> list[dict]:
    """The ``job_runs2`` rows still running (no ``finished_at``), from whichever backend holds the ops state."""
    return _select("SELECT * FROM job_runs2 WHERE status = 'running' AND finished_at IS NULL")


@_routed
def finish_run(run_id: str, status: str, error: str | None = None) -> None:
    with _open() as con:
        con.execute("UPDATE job_runs2 SET status = ?, finished_at = ?, error = ? WHERE run_id = ?",
                    (status, time.time(), error, run_id))


# ── Provider accounting ────────────────────────────────────────────────────────────────────────────────────────

def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def sample_uw_headers(headers, run_id: str | None = None) -> bool:
    """Store UW's rate headers from one response (once a run). Only ``x-uw-*`` headers are kept. False: none there."""
    kept = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()
            if str(k).lower().startswith(UW_HEADER_PREFIX)}
    if not kept:
        return False
    from . import caps                              # caps reads ops: imported here, not at module level
    count, cap = _int(kept.get(UW_DAILY_COUNT_HEADER)), caps.cap_for("uw")
    left = cap - count if (cap is not None and count is not None) else None
    put_rows("uw_remaining", [{
        "sampled_at": time.time(), "run_id": run_id, "daily_req_count": count, "daily_cap": cap,
        "daily_remaining_computed": left, "per_minute_remaining": _int(kept.get(UW_MINUTE_REMAINING_HEADER)),
        "per_minute_reset_ms": _int(kept.get(UW_MINUTE_RESET_HEADER)),
        "token_req_limit": _int(kept.get(UW_TOKEN_LIMIT_HEADER)), "raw_headers": _json(kept)}])
    return True


@_routed
def count_calls(provider: str, n: int = 1, failures: int = 0, http_429: int = 0) -> None:
    """Add to ``provider``'s counter for today (New York day)."""
    with _open() as con:
        con.execute("INSERT INTO provider_calls_daily (provider, ny_day, calls, failures, http_429) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (provider, ny_day) DO UPDATE SET "
                    "calls = calls + excluded.calls, failures = failures + excluded.failures, "
                    "http_429 = http_429 + excluded.http_429", (provider, _today(), n, failures, http_429))


@_routed
def calls_today(provider: str) -> int:
    with _open() as con:
        row = con.execute("SELECT calls FROM provider_calls_daily WHERE provider = ? AND ny_day = ?",
                          (provider, _today())).fetchone()
    return int(row[0]) if row else 0


# ── The read log ───────────────────────────────────────────────────────────────────────────────────────────────

#: A ``python -m <package>`` run starts from ``__main__.py``; its read-log name is its package's, or this word.
_MAIN_NAMES = {"build": "builder"}


def _process_name() -> str:
    """``RC_PROCESS_NAME``, else the script's name; a ``__main__.py`` is named after its package (#331).

    Without that, the builder, the backup CLI and every board run as ``python -m`` all counted under ``__main__``.
    Both separators are split, because the live machine is Windows.
    """
    env = os.getenv("RC_PROCESS_NAME")
    if env:
        return env
    parts = [p for p in re.split(r"[\\/]", sys.argv[0] or "") if p]
    if not parts:
        return "python"
    stem = parts[-1].rsplit(".", 1)[0] if "." in parts[-1] else parts[-1]
    if stem == "__main__" and len(parts) > 1:
        return _MAIN_NAMES.get(parts[-2], parts[-2])
    return stem or "python"


def safe_url(url: str) -> str:
    """``host/path`` of ``url`` for the request log: never the query, the fragment or a ``user:password@``, which
    are where a key travels (FRED's ``api_key=``); a ``;parameter`` is cut off a path segment, and a segment that
    looks like a token is written ``~``."""
    try:
        parts = urlsplit(str(url))
        host = parts.hostname or ""
    except ValueError:
        return "unreadable url"
    path = "/".join("~" if _token_like(seg) else seg for seg in (s.split(";", 1)[0] for s in parts.path.split("/")))
    return f"{host}{path}" if host else path


#: 32 or more key-alphabet characters in one path segment: longer than any accession number or ticker we ask for.
_TOKEN_LIKE = re.compile(r"[A-Za-z0-9_\-+=%:~]{32,}")


def _token_like(segment: str) -> bool:
    """A path segment that may be a key: 32+ key characters, ``<id>:<token>``, or three dotted parts (a JWT)."""
    return bool(_TOKEN_LIKE.fullmatch(segment) or (":" in segment and len(segment) >= 20)
                or (segment.count(".") >= 2 and len(segment) >= 40))


def request_log_on() -> bool:
    return os.getenv(REQUEST_LOG_ENV, "").strip().lower() not in ("0", "off", "false", "no")


class RequestLog:
    """One row a request (#715), held in this process and written with the read log's flush (and at exit).

    ``add`` never raises and never writes: it appends to a list. Past ``REQUEST_LOG_BUFFER`` unwritten rows the
    oldest go and are counted in ``dropped``, so a store that stays down costs log rows, not memory or a read.
    """

    def __init__(self, process: str | None = None, now: Callable[[], float] = time.time):
        self.process = process
        self.dropped = 0
        self._now = now
        self._rows: list[tuple] = []
        self._lock = threading.Lock()

    def add(self, kind: str, what: str, *, dataset: str | None = None, result: str | None = None,
            ms: float | None = None) -> None:
        if kind not in REQUEST_KINDS:
            raise ValueError(f"unknown request kind {kind!r}; one of {REQUEST_KINDS}")
        if not request_log_on():
            return
        row = (self._now(), kind, str(what)[:REQUEST_WHAT_MAX], dataset,
               None if result is None else str(result)[:REQUEST_WHAT_MAX], None if ms is None else int(round(ms)))
        with self._lock:
            self._rows.append(row)
            self._cap()

    def _cap(self) -> None:
        over = len(self._rows) - REQUEST_LOG_BUFFER
        if over > 0:
            del self._rows[:over]
            self.dropped += over

    def flush(self) -> int:
        """Write the held rows; how many. A failed write puts them back (oldest first, capped) and raises."""
        with self._lock:
            rows, self._rows = self._rows, []
        if not rows:
            return 0
        try:
            _add_requests(self.process or _process_name(), rows)
        except Exception:
            with self._lock:
                self._rows[:0] = rows
                self._cap()
            raise
        return len(rows)


class ReadLog:
    """Reads per table, counted in this process and written at most once a minute (and at exit).

    Every bump is also one row of ``requests`` (the per-request log, #715), written by the same flush.
    """

    def __init__(self, process: str | None = None, clock: Callable[[], float] = time.monotonic):
        self.process = process
        self.requests = RequestLog(process)
        self._clock = clock
        self._counts: dict[tuple[str, str], int] = {}
        self._last = clock()
        self._lock = threading.Lock()

    def bump(self, table: str) -> None:
        if table.startswith(NEED_READ_PREFIX):
            self.requests.add("need", table[len(NEED_READ_PREFIX):])
        else:
            self.requests.add("read", table)
        with self._lock:
            key = (_today(), table)
            self._counts[key] = self._counts.get(key, 0) + 1
        self.flush_if_due()

    def flush_if_due(self) -> None:
        """Flush when a minute has passed since the last one; a failed flush is logged and never raised."""
        with self._lock:
            due = self._clock() - self._last >= READ_LOG_FLUSH_S
        if due:
            try:
                self.flush()
            except Exception as exc:  # noqa: BLE001 - #228 item 5: a failed flush never fails the read
                logging.getLogger(__name__).warning("read log flush failed, kept for the next one: %s", exc)

    def flush(self) -> None:
        with self._lock:
            counts, self._counts = self._counts, {}
            self._last = self._clock()
        if counts:
            try:
                _add_reads(self.process or _process_name(), [(day, table, n) for (day, table), n in counts.items()])
            except Exception:
                # Review S1 (#356): the counts go back, so the next flush retries them; the retirement count reads
                # them. The request rows are not tried against a store that just failed: they wait in memory.
                with self._lock:
                    for key, n in counts.items():
                        self._counts[key] = self._counts.get(key, 0) + n
                raise
        try:
            self.requests.flush()
        except Exception as exc:  # noqa: BLE001 - the request log is a log: its rows wait, the counts are written
            logging.getLogger(__name__).warning("request log flush failed, kept for the next one: %s", exc)


@_routed
def _add_reads(process: str, counts: list[tuple[str, str, int]]) -> None:
    """Add ``(day, table, reads)`` to ``read_log`` for ``process``, in one transaction."""
    with _open() as con, _tx(con):
        con.executemany("INSERT INTO read_log (day, process, table_name, reads) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT (day, process, table_name) DO UPDATE SET reads = reads + excluded.reads",
                        [(day, process, table, n) for day, table, n in counts])


@_routed
def _add_requests(process: str, entries: list[tuple]) -> None:
    """Append ``(at, kind, what, dataset, result, ms)`` rows to ``request_log`` for ``process`` and drop all but the
    newest ``REQUEST_LOG_KEEP``, in one transaction."""
    with _open() as con, _tx(con):
        con.executemany("INSERT INTO request_log (at, kind, process, what, dataset, result, ms) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(at, kind, process, what, dataset, result, ms) for at, kind, what, dataset, result, ms in entries])
        con.execute("DELETE FROM request_log WHERE id <= (SELECT max(id) FROM request_log) - ?", (REQUEST_LOG_KEEP,))


@_routed
def recent_requests(after: int = 0, limit: int = 200) -> list[dict]:
    """The ``limit`` rows of ``request_log`` with the highest ids above ``after``, highest first. Never creates the
    file. Ids are given in the order rows are *written*, and a process writes its rows up to a minute after it made
    them, so id order is not time order and ``after`` is no cursor to page a live log with: sort by ``at``."""
    return _select("SELECT * FROM request_log WHERE id > ? ORDER BY id DESC LIMIT ?", (int(after), max(0, int(limit))))


def log_call(provider: str, what: str | None = None, *, dataset: str | None = None, result: str | None = None,
             ms: float | None = None) -> None:
    """One provider call into the per-request log: ``provider`` and, when given, ``what`` (pass a URL through
    ``safe_url`` first). Held in memory; written by the next flush, which this asks for when a minute has passed."""
    read_log.requests.add("call", f"{provider} {what}" if what else provider, dataset=dataset, result=result, ms=ms)
    read_log.flush_if_due()


read_log = ReadLog()


def _flush_at_exit() -> None:
    try:
        read_log.flush()
    except Exception as exc:  # noqa: BLE001 - no traceback at exit, but say so: kept counts can span many minutes
        logging.getLogger(__name__).warning("read log flush at exit failed, counts lost: %s", exc)


atexit.register(_flush_at_exit)


# ── Watch lists and the schedule ───────────────────────────────────────────────────────────────────────────

def _watch_kind(kind: str) -> str:
    if kind not in WATCH_KINDS:
        raise ValueError(f"unknown watch list {kind!r}; one of {WATCH_KINDS}")
    return kind


def _watch_write(con: sqlite3.Connection, kind: str, ticker: str, action: str, by: str, note: str | None) -> bool:
    """Add or remove ``ticker`` and write its history row, inside the caller's transaction; False when nothing moved."""
    now = time.time()
    if action == "add":
        cur = con.execute(f"INSERT OR IGNORE INTO watchlist_{kind} VALUES (?, ?, ?, ?)", (ticker, now, by, note))
    else:
        cur = con.execute(f"DELETE FROM watchlist_{kind} WHERE ticker = ?", (ticker,))
    if not cur.rowcount:
        return False
    con.execute(f"INSERT INTO watchlist_{kind}_changes (ticker, action, at, by, note) VALUES (?, ?, ?, ?, ?)",
                (ticker, action, now, by, note))
    return True


@_routed
def watch_add(kind: str, ticker: str, *, by: str, note: str | None = None) -> bool:
    """Add ``ticker`` (upper-cased) to a watch list and its history; False when it was already there."""
    kind, ticker = _watch_kind(kind), ticker.strip().upper()
    with _open() as con, _tx(con):
        return _watch_write(con, kind, ticker, "add", by, note)


@_routed
def watch_remove(kind: str, ticker: str, *, by: str) -> bool:
    kind, ticker = _watch_kind(kind), ticker.strip().upper()
    with _open() as con, _tx(con):
        return _watch_write(con, kind, ticker, "remove", by, None)


@_routed
def watch_toggle(kind: str, ticker: str, *, by: str) -> bool:
    """Add ``ticker`` when it is not on the list, else remove it; whether it is on the list now.

    The read and the write are one ``BEGIN IMMEDIATE`` transaction, so two clicks that race flip it twice, in turn,
    and each writes its own history row.
    """
    kind, ticker = _watch_kind(kind), ticker.strip().upper()
    with _open() as con, _tx(con):
        there = con.execute(f"SELECT 1 FROM watchlist_{kind} WHERE ticker = ?", (ticker,)).fetchone() is not None
        _watch_write(con, kind, ticker, "remove" if there else "add", by, None)
        return not there


@_routed
def watch_replace(kind: str, tickers, *, by: str) -> tuple[int, int]:
    """Make the list exactly ``tickers`` in one transaction; ``(added, removed)``, each with its history row."""
    kind = _watch_kind(kind)
    want = {str(t).strip().upper() for t in tickers if str(t).strip()}
    with _open() as con, _tx(con):
        have = {r[0] for r in con.execute(f"SELECT ticker FROM watchlist_{kind}")}
        for t in sorted(have - want):
            _watch_write(con, kind, t, "remove", by, None)
        for t in sorted(want - have):
            _watch_write(con, kind, t, "add", by, None)
    return len(want - have), len(have - want)


@_routed
def watch_list(kind: str) -> list[str]:
    with _open() as con:
        return [r[0] for r in con.execute(f"SELECT ticker FROM watchlist_{_watch_kind(kind)} ORDER BY ticker")]


@_routed
def schedule_set(dataset: str, cadence: str, next_due: str | None) -> None:
    with _open() as con:
        con.execute("INSERT INTO dataset_schedule (dataset, cadence, next_due) VALUES (?, ?, ?) "
                    "ON CONFLICT (dataset) DO UPDATE SET cadence = excluded.cadence, next_due = excluded.next_due",
                    (dataset, cadence, next_due))


def cancel_note(by: str, at: float) -> str:
    """The note a request cancelled by a Pause carries: who and when (UTC, to the minute)."""
    return f"cancelled: paused by {by} at {time.strftime('%Y-%m-%d %H:%M', time.gmtime(at))} UTC"


def _cancel_queued(con: sqlite3.Connection, dataset: str, by: str, at: float) -> int:
    # only ``queued`` rows: ``next_request`` claims those alone, so the builder and a cancel never hold one row
    return con.execute("UPDATE build_requests SET status = 'skipped', ended_at = ?, note = ? "
                       "WHERE dataset = ? AND status = 'queued'", (at, cancel_note(by, at), dataset)).rowcount


@_routed
def cancel_queued(dataset: str, *, by: str) -> int:
    """Skip every queued request of ``dataset`` (``cancelled: paused by <by> at <when> UTC``); how many. A running
    request is never touched: the running fetch stops itself at its next provider call (``base.RunContext``)."""
    with _open() as con, _tx(con):
        return _cancel_queued(con, dataset, by, time.time())


@_routed
def schedule_pause(dataset: str, *, by: str) -> int:
    """Hold ``dataset`` and cancel its queued requests in one transaction (#492 PR (ii)); how many were cancelled."""
    now = time.time()
    with _open() as con, _tx(con):
        con.execute("INSERT INTO dataset_schedule (dataset, paused_at, paused_by) VALUES (?, ?, ?) "
                    "ON CONFLICT (dataset) DO UPDATE SET paused_at = excluded.paused_at, paused_by = excluded.paused_by",
                    (dataset, now, by))
        return _cancel_queued(con, dataset, by, now)


@_routed
def schedule_resume(dataset: str) -> None:
    with _open() as con:
        con.execute("UPDATE dataset_schedule SET paused_at = NULL, paused_by = NULL WHERE dataset = ?", (dataset,))


@_routed
def schedule_get(dataset: str) -> dict | None:
    with _open() as con:
        row = con.execute("SELECT * FROM dataset_schedule WHERE dataset = ?", (dataset,)).fetchone()
    return dict(row) if row else None


def _kind_mode(mode: str) -> str:
    if mode not in KIND_MODES:
        raise ValueError(f"unknown mode {mode!r}; known: {', '.join(KIND_MODES)}")
    return mode


@_routed
def kind_mode_set(kind: str, mode: str, *, by: str) -> None:
    with _open() as con:
        con.execute("INSERT INTO kind_automation (kind, mode, set_at, set_by) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (kind) DO UPDATE SET mode = excluded.mode, set_at = excluded.set_at, "
                    "set_by = excluded.set_by", (kind, _kind_mode(mode), time.time(), by))


@_routed
def kind_mode_get(kind: str) -> dict | None:
    with _open() as con:
        row = con.execute("SELECT * FROM kind_automation WHERE kind = ?", (kind,)).fetchone()
    return dict(row) if row else None


def kind_mode(kind: str) -> str:
    """The kind's ``KIND_MODES`` value; ``off`` when it has no row."""
    return (kind_mode_get(kind) or {}).get("mode") or "off"


#: What a source's own Automated select sets (#527): any ``KIND_MODES`` value. ``file`` (the old "as the file") still
#: drops the row, which is ``off`` now: the hourly pass no longer reads the kind's mode (avo, 10 Nov 2026).
AS_THE_FILE = "file"
DATASET_MODES = (AS_THE_FILE, *KIND_MODES)


@_routed
def dataset_mode_set(dataset: str, mode: str, *, by: str) -> None:
    if mode == AS_THE_FILE:
        with _open() as con:
            con.execute("DELETE FROM dataset_automation WHERE dataset = ?", (dataset,))
        return
    with _open() as con:
        con.execute("INSERT INTO dataset_automation (dataset, mode, set_at, set_by) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (dataset) DO UPDATE SET mode = excluded.mode, set_at = excluded.set_at, "
                    "set_by = excluded.set_by", (dataset, _kind_mode(mode), time.time(), by))


@_routed
def dataset_mode_get(dataset: str) -> dict | None:
    with _open() as con:
        row = con.execute("SELECT * FROM dataset_automation WHERE dataset = ?", (dataset,)).fetchone()
    return dict(row) if row else None


def dataset_mode(dataset: str, kind: str) -> str:
    """What the hourly pass runs for ``dataset``: its own row's mode, ``off`` when it has none. ``kind`` is no longer
    read (avo, 10 Nov 2026: the file-level setting is gone); the argument stays so the builder's call is unchanged."""
    return (dataset_mode_get(dataset) or {}).get("mode") or "off"


def copy_kind_modes(datasets: dict, *, by: str) -> list[tuple[str, str]]:
    """The one-time step of the deploy that drops the file-level setting: each dataset with no row of its own gets
    its kind's mode as its own, so the hourly pass runs exactly what it ran before. ``datasets`` is ``{name: object
    with .kind}`` (``rcdata.build.DATASETS``). Returns ``[(dataset, mode)]`` it wrote; ``off`` kinds write nothing,
    and running it twice writes nothing the second time."""
    out = []
    for name, ds in datasets.items():
        if dataset_mode_get(name) is not None:
            continue
        mode = kind_mode(ds.kind)
        if mode != "off":
            dataset_mode_set(name, mode, by=by)
            out.append((name, mode))
    return out


def _switch(name: str) -> str:
    if name not in SWITCHES + FILE_SWITCHES:
        raise ValueError(f"unknown switch {name!r}; known: {', '.join(SWITCHES + FILE_SWITCHES)}")
    return name


@_routed
def switch_set(name: str, on: bool, *, by: str) -> None:
    """Turn a board switch on (``off_at`` NULL) or off (who and when)."""
    off_at, off_by = (None, None) if on else (time.time(), by)
    with _open() as con:
        con.execute("INSERT INTO switch (name, off_at, off_by) VALUES (?, ?, ?) "
                    "ON CONFLICT (name) DO UPDATE SET off_at = excluded.off_at, off_by = excluded.off_by",
                    (_switch(name), off_at, off_by))


@_routed
def switch_get(name: str) -> dict | None:
    with _open() as con:
        row = con.execute("SELECT * FROM switch WHERE name = ?", (_switch(name),)).fetchone()
    return dict(row) if row else None


def switch_off(name: str) -> dict | None:
    """The switch's row while it is off, else None."""
    row = switch_get(name)
    return row if row and row.get("off_at") is not None else None


def switch_words(name: str, row: dict) -> str:
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(row["off_at"]))
    what = f"{name.removeprefix('file-')}.duckdb" if name in FILE_SWITCHES else name
    return f"the {what} switch is off ({row.get('off_by') or '?'} at {when})"


# ── Read helpers for the Settings page (PR 2) ──────────────────────────────────────────────────────────────────
#
# The page only reads (F7 A): these never create ops.sqlite or a table. No file yet, or a table an older file does
# not have, reads as no rows. Each call opens its own connection and closes it before it returns.

def _select(sql: str, args: tuple = ()) -> list[dict]:
    path = ops_path()
    if not path.exists():
        return []
    with connect(path) as con:
        try:
            return [dict(r) for r in con.execute(sql, args)]
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return []
            raise


@_routed
def schema_version() -> int | None:
    rows = _select("SELECT max(version) AS v FROM ops_schema")
    return rows[0]["v"] if rows else None


@_routed
def recent_checks(dataset: str, limit: int = 20) -> list[dict]:
    """The newest ``limit`` ``dataset_check`` rows of ``dataset``, newest first."""
    return _select("SELECT * FROM dataset_check WHERE dataset = ? ORDER BY id DESC LIMIT ?", (dataset, int(limit)))


@_routed
def read_latest_check(dataset: str) -> dict | None:
    """``latest_check`` for a reader: the same row, without creating the file or a table (#336).

    ``latest_check`` opens through ``_open``, which ensures the file; that is right for the builder, which writes it
    anyway, and wrong for a page render (F7 A). ``read.dataset_status`` and the boards' status reads use this one.
    """
    rows = _select("SELECT * FROM dataset_check WHERE dataset = ? ORDER BY id DESC LIMIT 1", (dataset,))
    return rows[0] if rows else None


@_routed
def latest_unskipped_check(dataset: str) -> dict | None:
    """The newest ``dataset_check`` row of ``dataset`` that is not ``skipped``, however many skips came after it."""
    rows = _select("SELECT * FROM dataset_check WHERE dataset = ? AND result != 'skipped' ORDER BY id DESC LIMIT 1",
                   (dataset,))
    return rows[0] if rows else None


@_routed
def latest_good_check(dataset: str) -> dict | None:
    """The newest ``changed`` or ``unchanged`` ``dataset_check`` row of ``dataset``: the last run that built."""
    rows = _select("SELECT * FROM dataset_check WHERE dataset = ? AND result IN ('changed', 'unchanged') "
                   "ORDER BY id DESC LIMIT 1", (dataset,))
    return rows[0] if rows else None


def _prune_cutoff(days: int) -> str:
    return (date.fromisoformat(_today()) - timedelta(days=days)).isoformat()


@_routed
def prune_issues(days: int = RESOLVED_ISSUE_DAYS) -> int:
    """Delete the ``build_issues`` rows resolved more than ``days`` days ago; an open row is never touched. Returns
    how many went."""
    with _open() as con, _tx(con):
        return con.execute("DELETE FROM build_issues WHERE resolved_on < ?", (_prune_cutoff(days),)).rowcount


@_routed
def open_issues(dataset: str, limit: int = 20) -> tuple[int, list[dict]]:
    """``(count, newest rows)`` of the unresolved ``build_issues`` of ``dataset``."""
    n = _select("SELECT count(*) AS n FROM build_issues WHERE dataset = ? AND resolved_on IS NULL", (dataset,))
    rows = _select("SELECT * FROM build_issues WHERE dataset = ? AND resolved_on IS NULL ORDER BY id DESC LIMIT ?",
                   (dataset, int(limit)))
    return (n[0]["n"] if n else 0), rows


@_routed
def ever_built(dataset: str) -> bool:
    """True once ``dataset`` has a ``changed`` or ``unchanged`` check (it has built at least once)."""
    return bool(_select("SELECT 1 AS one FROM dataset_check WHERE dataset = ? AND result IN ('changed', 'unchanged') "
                        "LIMIT 1", (dataset,)))


@_routed
def open_issue_kinds(dataset: str) -> list[dict]:
    """``[{kind, count}]`` of the unresolved ``build_issues`` of ``dataset``, the most first."""
    return _select("SELECT kind, count(*) AS count FROM build_issues WHERE dataset = ? AND resolved_on IS NULL "
                   "GROUP BY kind ORDER BY count(*) DESC, kind", (dataset,))


@_routed
def read_queue(finished: int = 20) -> dict:
    """``queue_view`` for a reader: the same rows, without creating the file."""
    active = _select("SELECT * FROM build_requests WHERE status IN ('queued','running') ORDER BY id")
    done = _select("SELECT * FROM build_requests WHERE status NOT IN ('queued','running') ORDER BY id DESC LIMIT ?",
                   (int(finished),))
    return {"active": [_request(r) for r in active], "finished": [_request(r) for r in done]}


@_routed
def read_schedule(dataset: str) -> dict | None:
    rows = _select("SELECT * FROM dataset_schedule WHERE dataset = ?", (dataset,))
    return rows[0] if rows else None


@_routed
def read_switches() -> dict[str, dict]:
    """``{name: switch row}`` for a reader, without creating the file."""
    return {r["name"]: r for r in _select("SELECT * FROM switch")}


@_routed
def read_dataset_modes() -> dict[str, dict]:
    """``{dataset: dataset_automation row}`` for a reader, without creating the file."""
    return {r["dataset"]: r for r in _select("SELECT * FROM dataset_automation")}


@_routed
def read_kind_modes() -> dict[str, dict]:
    """``{kind: kind_automation row}`` for a reader, without creating the file."""
    return {r["kind"]: r for r in _select("SELECT * FROM kind_automation")}


@_routed
def calls_by_day(first_day: str, last_day: str) -> list[dict]:
    """``provider_calls_daily`` rows with ``first_day <= ny_day <= last_day``."""
    return _select("SELECT * FROM provider_calls_daily WHERE ny_day BETWEEN ? AND ? ORDER BY provider, ny_day",
                   (first_day, last_day))


@_routed
def latest_uw_sample() -> dict | None:
    rows = _select("SELECT sampled_at, run_id, daily_req_count, daily_cap, daily_remaining_computed, "
                   "per_minute_remaining, token_req_limit FROM uw_remaining "
                   "ORDER BY sampled_at DESC LIMIT 1")
    return rows[0] if rows else None


@_routed
def reads_since(first_day: str, tables) -> dict[str, int]:
    """Reads of each of ``tables`` in ``read_log`` since ``first_day`` (every process together)."""
    names = list(tables)
    if not names:
        return {}
    rows = _select(f"SELECT table_name, sum(reads) AS n FROM read_log WHERE day >= ? AND table_name IN "
                   f"({', '.join('?' * len(names))}) GROUP BY table_name", (first_day, *names))
    got = {r["table_name"]: int(r["n"]) for r in rows}
    return {t: got.get(t, 0) for t in names}


#: The prefix ``rcdata.needs`` counts a need under in ``read_log``, beside the table name (``serve.ask``/``summary``).
NEED_READ_PREFIX = "need:"


@_routed
def need_reads(first_day: str) -> list[dict]:
    """``[{need, process, day, reads}]``: every ``need:<name>`` row of ``read_log`` since ``first_day``.

    The Settings Needs tab's request log (#331). Day granularity, because that is what the log holds; the table rows
    beside them are left out, and the name comes back without its prefix. Never creates the file.
    """
    rows = _select("SELECT table_name, process, day, reads FROM read_log WHERE day >= ? AND table_name LIKE 'need:%' "
                   "ORDER BY table_name, process, day", (first_day,))
    return [{"need": r["table_name"][len(NEED_READ_PREFIX):], "process": r["process"], "day": r["day"],
             "reads": int(r["reads"])} for r in rows]


@_routed
def run_started_at(run_id: str) -> float | None:
    """``job_runs2.started_at`` of ``run_id`` (epoch seconds), None when there is no such run."""
    rows = _select("SELECT started_at FROM job_runs2 WHERE run_id = ?", (run_id,))
    return rows[0]["started_at"] if rows else None


@_routed
def runs_between(start: float, end: float) -> list[dict]:
    """The builder's runs that started in ``[start, end)`` (epoch seconds), oldest first: ``dataset``,
    ``started_at``, ``status``. The Grafana RC Fetch board's heatmap over its time picker's range (avo, 10 Nov 2026)."""
    return _select("SELECT dataset, started_at, status FROM job_runs2 WHERE started_at >= ? AND started_at < ? "
                   "ORDER BY started_at, run_id", (float(start), float(end)))


@_routed
def last_builder_run() -> dict | None:
    rows = _select("SELECT run_id, dataset, trigger, pid, started_at, finished_at, status, error FROM job_runs2 "
                   "ORDER BY started_at DESC LIMIT 1")
    return rows[0] if rows else None


@_routed
def watch_count(kind: str) -> int:
    rows = _select(f"SELECT count(*) AS n FROM watchlist_{_watch_kind(kind)}")
    return rows[0]["n"] if rows else 0


@_routed
def watch_last_change() -> float | None:
    """The newest add or remove on either watch list (epoch seconds), or None; never creates the file."""
    rows = _select(" UNION ALL ".join(f"SELECT max(at) AS at FROM watchlist_{k}_changes" for k in WATCH_KINDS))
    stamps = [r["at"] for r in rows if r["at"] is not None]
    return max(stamps) if stamps else None


@_routed
def watch_rows(kind: str) -> list[dict]:
    """The rows of one watch list, by ticker: ``ticker``, ``added_at`` (epoch seconds), ``added_by``, ``note``."""
    return _select(f"SELECT ticker, added_at, added_by, note FROM watchlist_{_watch_kind(kind)} ORDER BY ticker")


# ── Whole tables, either backend (the one-time copy, the backup and the tests) ─────────────────────────────────

def _primary_key(table: str) -> list[str]:
    with closing(sqlite3.connect(":memory:")) as con:
        con.execute(_DDL[table])
        cols = sorted((r[5], r[1]) for r in con.execute(f"PRAGMA table_info({table})") if r[5])
    return [c for _, c in cols]


@_routed
def rows(table: str) -> list[dict]:
    """Every row of ``table`` (one of ``TABLES``), each a dict in column order, by primary key; [] without a file."""
    if table not in _DDL:
        raise ValueError(f"unknown ops table {table!r}")
    return _select(f"SELECT * FROM {table} ORDER BY {', '.join(_primary_key(table))}")


@_routed
def put_rows(table: str, new_rows: Iterable[dict]) -> int:
    """Write ``new_rows`` as given (ids included), replacing a row with the same primary key; how many were written.

    For copying rows between backends and for tests that set a row up; the functions above are how the app writes.
    """
    if table not in _DDL:
        raise ValueError(f"unknown ops table {table!r}")
    new_rows = list(new_rows)
    with _open() as con, _tx(con):
        for row in new_rows:
            con.execute(f"INSERT OR REPLACE INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                        tuple(row.values()))
    return len(new_rows)
