"""``ops.sqlite``: the market store's operational state, and every SQL statement that touches it (D6).

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
- ``uw_remaining``: UW's rate headers, sampled once a run.
- ``provider_calls_daily``: calls per provider and New York day; the day cap (``caps``) reads it.
- ``watchlist_option`` / ``watchlist_stock`` and their ``*_changes`` history; ``dataset_schedule``. Tables and
  helpers only here; the routes and the cards come later (PR 2, PR 4).

``ensure()`` creates the tables and stamps ``ops_schema``; it refuses a file stamped by a newer version.
"""
from __future__ import annotations

import atexit
import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from ..core import calendar, config

OPS_SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
FILE_NAME = "ops.sqlite"

REQUEST_STATUSES = ("queued", "running", "done", "failed", "skipped")
CHECK_RESULTS = ("changed", "unchanged", "failed", "skipped")
WATCH_KINDS = ("option", "stock")
# The UW response headers sampled into uw_remaining: only UW's own, never a cookie or anything else the answer had.
UW_HEADER_PREFIX = "x-uw-"
UW_LIMIT_HEADER = "x-uw-token-req-limit"
UW_REMAINING_HEADER = "x-uw-req-per-minute-remaining"
READ_LOG_FLUSH_S = 60.0

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
        sampled_at REAL PRIMARY KEY, run_id TEXT, token_req_limit INTEGER, req_per_minute_remaining INTEGER,
        raw_headers TEXT)""",
    "provider_calls_daily": """CREATE TABLE IF NOT EXISTS provider_calls_daily (
        provider TEXT NOT NULL, ny_day TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0, http_429 INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (provider, ny_day))""",
    **{f"watchlist_{k}": f"""CREATE TABLE IF NOT EXISTS watchlist_{k} (
        ticker TEXT PRIMARY KEY, added_at REAL NOT NULL, added_by TEXT, note TEXT)""" for k in WATCH_KINDS},
    **{f"watchlist_{k}_changes": f"""CREATE TABLE IF NOT EXISTS watchlist_{k}_changes (
        ticker TEXT NOT NULL, action TEXT NOT NULL CHECK (action IN ('add','remove')), at REAL NOT NULL, by TEXT)"""
       for k in WATCH_KINDS},
    "dataset_schedule": """CREATE TABLE IF NOT EXISTS dataset_schedule (
        dataset TEXT PRIMARY KEY, cadence TEXT, next_due TEXT, paused_at REAL, paused_by TEXT)""",
}
TABLES = tuple(_DDL)
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS build_requests_status ON build_requests (status, id)",
    "CREATE INDEX IF NOT EXISTS dataset_check_dataset ON dataset_check (dataset, id)",
)


class OpsSchemaTooNew(RuntimeError):
    """ops.sqlite was stamped by a newer rcdata: this checkout must not write to it."""


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


@contextmanager
def _tx(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One write transaction, taken at once (``BEGIN IMMEDIATE``), so a read-then-write cannot race."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


_ENSURED: set[str] = set()
_ENSURE_LOCK = threading.Lock()


def ensure(path: Path | None = None) -> None:
    """Create every table (idempotent) and stamp ``ops_schema``; raise ``OpsSchemaTooNew`` on a newer stamp.

    Version 1 is the first, so there is nothing to upgrade yet; a later version adds its step here, keyed on the
    stamp it finds.
    """
    path = Path(path or ops_path())
    with _ENSURE_LOCK, connect(path) as con, _tx(con):
        for ddl in _DDL.values():
            con.execute(ddl)
        for ddl in _INDEXES:
            con.execute(ddl)
        found = con.execute("SELECT max(version) FROM ops_schema").fetchone()[0]
        if found is not None and found > OPS_SCHEMA_VERSION:
            raise OpsSchemaTooNew(f"{path} is ops schema {found}; this checkout knows {OPS_SCHEMA_VERSION}")
        if found is None:
            con.execute("INSERT INTO ops_schema (version, applied_at) VALUES (?, ?)", (OPS_SCHEMA_VERSION, time.time()))
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

def enqueue(dataset: str, params: dict | None = None, *, by: str) -> int:
    """Queue one build of ``dataset``; returns the request id. The only write a page ever makes (F7 A)."""
    with _open() as con:
        cur = con.execute("INSERT INTO build_requests (dataset, requested_at, requested_by, params, status) "
                          "VALUES (?, ?, ?, ?, 'queued')", (dataset, time.time(), by, _json(params)))
        return int(cur.lastrowid)


def _request(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["params"] = json.loads(out["params"]) if out["params"] else None
    return out


def next_request(dataset: str | None = None) -> dict | None:
    """Take the oldest queued request (of ``dataset``, when given) and mark it running; None when none is queued."""
    with _open() as con, _tx(con):
        sql = "SELECT id FROM build_requests WHERE status = 'queued'"
        args: tuple = ()
        if dataset is not None:
            sql += " AND dataset = ?"
            args = (dataset,)
        row = con.execute(sql + " ORDER BY id LIMIT 1", args).fetchone()
        if row is None:
            return None
        con.execute("UPDATE build_requests SET status = 'running', started_at = ? WHERE id = ?", (time.time(), row[0]))
        return _request(con.execute("SELECT * FROM build_requests WHERE id = ?", (row[0],)).fetchone())


def finish_request(request_id: int, status: str, note: str | None = None) -> None:
    if status not in REQUEST_STATUSES:
        raise ValueError(f"unknown request status {status!r}")
    with _open() as con:
        con.execute("UPDATE build_requests SET status = ?, ended_at = ?, note = ? WHERE id = ?",
                    (status, time.time(), note, request_id))


# ── Checks, issues, runs ───────────────────────────────────────────────────────────────────────────────────────

CHECK_FIELDS = ("dataset", "checked_on", "result", "source_as_of", "rows", "added", "removed", "coverage", "calls",
                "next_check_due", "run_id", "note")


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


def latest_check(dataset: str) -> dict | None:
    """The newest ``dataset_check`` row of ``dataset`` (by insertion), or None."""
    with _open() as con:
        row = con.execute("SELECT * FROM dataset_check WHERE dataset = ? ORDER BY id DESC LIMIT 1",
                          (dataset,)).fetchone()
    return dict(row) if row else None


def record_issue(dataset: str, kind: str, key: str | None, detail: str | None, run_id: str | None = None) -> int:
    with _open() as con:
        cur = con.execute("INSERT INTO build_issues (dataset, found_on, kind, key, detail, run_id) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (dataset, _today(), kind, key, detail, run_id))
        return int(cur.lastrowid)


def start_run(run_id: str, dataset: str, trigger: str, params: dict | None = None) -> None:
    import socket
    with _open() as con:
        con.execute("INSERT INTO job_runs2 (run_id, dataset, trigger, pid, host, started_at, status, params) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
                    (run_id, dataset, trigger, os.getpid(), socket.gethostname(), time.time(), _json(params)))


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
    with _open() as con:
        con.execute("INSERT OR REPLACE INTO uw_remaining VALUES (?, ?, ?, ?, ?)",
                    (time.time(), run_id, _int(kept.get(UW_LIMIT_HEADER)), _int(kept.get(UW_REMAINING_HEADER)),
                     _json(kept)))
    return True


def count_calls(provider: str, n: int = 1, failures: int = 0, http_429: int = 0) -> None:
    """Add to ``provider``'s counter for today (New York day)."""
    with _open() as con:
        con.execute("INSERT INTO provider_calls_daily (provider, ny_day, calls, failures, http_429) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (provider, ny_day) DO UPDATE SET "
                    "calls = calls + excluded.calls, failures = failures + excluded.failures, "
                    "http_429 = http_429 + excluded.http_429", (provider, _today(), n, failures, http_429))


def calls_today(provider: str) -> int:
    with _open() as con:
        row = con.execute("SELECT calls FROM provider_calls_daily WHERE provider = ? AND ny_day = ?",
                          (provider, _today())).fetchone()
    return int(row[0]) if row else 0


# ── The read log ───────────────────────────────────────────────────────────────────────────────────────────────

def _process_name() -> str:
    return os.getenv("RC_PROCESS_NAME") or Path(sys.argv[0] or "python").stem or "python"


class ReadLog:
    """Reads per table, counted in this process and written at most once a minute (and at exit)."""

    def __init__(self, process: str | None = None, clock: Callable[[], float] = time.monotonic):
        self.process = process
        self._clock = clock
        self._counts: dict[tuple[str, str], int] = {}
        self._last = clock()
        self._lock = threading.Lock()

    def bump(self, table: str) -> None:
        with self._lock:
            key = (_today(), table)
            self._counts[key] = self._counts.get(key, 0) + 1
            due = self._clock() - self._last >= READ_LOG_FLUSH_S
        if due:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            counts, self._counts = self._counts, {}
            self._last = self._clock()
        if not counts:
            return
        process = self.process or _process_name()
        with _open() as con, _tx(con):
            con.executemany("INSERT INTO read_log (day, process, table_name, reads) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT (day, process, table_name) DO UPDATE SET reads = reads + excluded.reads",
                            [(day, process, table, n) for (day, table), n in counts.items()])


read_log = ReadLog()


def _flush_at_exit() -> None:
    try:
        read_log.flush()
    except Exception:  # noqa: BLE001 - a lost minute of read counts is not worth a traceback at exit
        pass


atexit.register(_flush_at_exit)


# ── Watch lists and the schedule (tables and helpers; the routes come in PR 2 and PR 4) ────────────────────────

def _watch_kind(kind: str) -> str:
    if kind not in WATCH_KINDS:
        raise ValueError(f"unknown watch list {kind!r}; one of {WATCH_KINDS}")
    return kind


def watch_add(kind: str, ticker: str, *, by: str, note: str | None = None) -> bool:
    """Add ``ticker`` (upper-cased) to a watch list and its history; False when it was already there."""
    kind, ticker, now = _watch_kind(kind), ticker.strip().upper(), time.time()
    with _open() as con, _tx(con):
        cur = con.execute(f"INSERT OR IGNORE INTO watchlist_{kind} VALUES (?, ?, ?, ?)", (ticker, now, by, note))
        if not cur.rowcount:
            return False
        con.execute(f"INSERT INTO watchlist_{kind}_changes VALUES (?, 'add', ?, ?)", (ticker, now, by))
        return True


def watch_remove(kind: str, ticker: str, *, by: str) -> bool:
    kind, ticker = _watch_kind(kind), ticker.strip().upper()
    with _open() as con, _tx(con):
        cur = con.execute(f"DELETE FROM watchlist_{kind} WHERE ticker = ?", (ticker,))
        if not cur.rowcount:
            return False
        con.execute(f"INSERT INTO watchlist_{kind}_changes VALUES (?, 'remove', ?, ?)", (ticker, time.time(), by))
        return True


def watch_list(kind: str) -> list[str]:
    with _open() as con:
        return [r[0] for r in con.execute(f"SELECT ticker FROM watchlist_{_watch_kind(kind)} ORDER BY ticker")]


def schedule_set(dataset: str, cadence: str, next_due: str | None) -> None:
    with _open() as con:
        con.execute("INSERT INTO dataset_schedule (dataset, cadence, next_due) VALUES (?, ?, ?) "
                    "ON CONFLICT (dataset) DO UPDATE SET cadence = excluded.cadence, next_due = excluded.next_due",
                    (dataset, cadence, next_due))


def schedule_pause(dataset: str, *, by: str) -> None:
    with _open() as con:
        con.execute("INSERT INTO dataset_schedule (dataset, paused_at, paused_by) VALUES (?, ?, ?) "
                    "ON CONFLICT (dataset) DO UPDATE SET paused_at = excluded.paused_at, paused_by = excluded.paused_by",
                    (dataset, time.time(), by))


def schedule_resume(dataset: str) -> None:
    with _open() as con:
        con.execute("UPDATE dataset_schedule SET paused_at = NULL, paused_by = NULL WHERE dataset = ?", (dataset,))


def schedule_get(dataset: str) -> dict | None:
    with _open() as con:
        row = con.execute("SELECT * FROM dataset_schedule WHERE dataset = ?", (dataset,)).fetchone()
    return dict(row) if row else None
