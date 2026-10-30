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
- ``uw_remaining``: UW's rate headers, sampled once a run: the daily count UW reports, what is left of our own cap
  (computed), the per-minute window, and the token limit as sent (not a daily cap).
- ``provider_calls_daily``: calls per provider and New York day; the day cap (``caps``) reads it.
- ``watchlist_option`` / ``watchlist_stock``: the two watch lists (D2), one row per ticker with when, by which board
  and an optional note; ``watchlist_*_changes`` is their history, one row per add or remove. The dashboard's toggle
  routes write them (PR 4); the option list is what the options build will cover besides SPX.
- ``dataset_schedule``: cadence and pause state per dataset.

``ensure()`` creates the tables and stamps ``ops_schema``; it upgrades an older file in place and refuses a file
stamped by a newer version.
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
from typing import Any, Callable, Iterable, Iterator

from ..core import calendar, config

OPS_SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5000
FILE_NAME = "ops.sqlite"

REQUEST_STATUSES = ("queued", "running", "done", "failed", "skipped")
CHECK_RESULTS = ("changed", "unchanged", "failed", "skipped")
WATCH_KINDS = ("option", "stock")
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

def enqueue(dataset: str, params: dict | None = None, *, by: str) -> int:
    """Queue one build of ``dataset``; returns the request id. The only write a page ever makes (F7 A)."""
    with _open() as con:
        cur = con.execute("INSERT INTO build_requests (dataset, requested_at, requested_by, params, status) "
                          "VALUES (?, ?, ?, ?, 'queued')", (dataset, time.time(), by, _json(params)))
        return int(cur.lastrowid)


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


def queue_view(finished: int = 20) -> dict:
    """``{"active": queued and running rows, oldest first; "finished": the newest ``finished`` others}``."""
    with _open() as con:
        active = con.execute("SELECT * FROM build_requests WHERE status IN ('queued','running') ORDER BY id").fetchall()
        done = con.execute("SELECT * FROM build_requests WHERE status NOT IN ('queued','running') "
                           "ORDER BY id DESC LIMIT ?", (int(finished),)).fetchall()
    return {"active": [_request(r) for r in active], "finished": [_request(r) for r in done]}


def _request(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["params"] = json.loads(out["params"]) if out["params"] else None
    return out


def next_request(dataset: str | None = None, *, skip: Iterable[str] = (),
                 only: Iterable[str] | None = None, by: str | None = None) -> dict | None:
    """Take the oldest queued request (of ``dataset``, when given) and mark it running; None when none is queued.

    ``skip`` leaves those datasets' requests in the queue, and ``only`` claims nothing else: that is how a request
    waits for the runner that may run it instead of being consumed by the one that may not. The hourly builder
    skips the attended datasets (a person logs in to WRDS), the attended runner claims only them. The two are
    complements, so asking for both at once is a mistake, not an intersection.

    ``by`` claims only what that requester queued. A person's ``backfill <attended>`` uses it so their run is
    their own: a request a page left for the attended runner is not theirs to spend the WRDS login and the day
    cap on (review SHOULD-2).
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


def queued(dataset: str, *, by: str | None = None) -> bool:
    """True while ``dataset`` has a request waiting; with ``by``, one that requester queued."""
    sql = "SELECT count(*) FROM build_requests WHERE status = 'queued' AND dataset = ?"
    args: tuple = (dataset,)
    if by is not None:
        sql += " AND requested_by = ?"
        args += (by,)
    with _open() as con:
        return con.execute(sql, args).fetchone()[0] > 0


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


def fail_orphans() -> tuple[int, int]:
    """Reclaim every request and run a dead builder left running: ``(requests, runs)`` failed.

    Called by the builder at start, while it holds the ``rcdata_build`` lock. A request's pid is its run's (the
    ``job_runs2`` row of the same dataset that started first at or after the request was taken). A request or run
    whose builder is still alive is left alone; any other becomes ``failed`` with the note ``lost: pid <n> gone``
    (``lost: pid unknown`` when the builder died before its run row), and each reclaimed request gets a ``failed``
    ``dataset_check`` row with that note, so the dataset's card shows the lost run.
    """
    me = os.getpid()
    now, tail = time.time(), f"builder died; found running by the builder started as pid {me}"
    with _open() as con, _tx(con):
        runs = [dict(r) for r in con.execute("SELECT run_id, dataset, pid, started_at FROM job_runs2 "
                                             "WHERE status = 'running' AND pid IS NOT ? ORDER BY started_at",
                                             (me,))]
        alive = {r["run_id"]: _builder_alive(r["pid"]) for r in runs}
        n_reqs = 0
        for req in con.execute("SELECT id, dataset, started_at FROM build_requests WHERE status = 'running' "
                               "ORDER BY id").fetchall():
            run = next((r for r in runs if r["dataset"] == req["dataset"]
                        and (r["started_at"] or 0) >= (req["started_at"] or 0)), None)
            if run is not None and alive[run["run_id"]]:
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


def sync_issues(dataset: str, kinds, found, run_id: str | None = None) -> tuple[int, int]:
    """Make the open ``build_issues`` of ``dataset`` and ``kinds`` the rows ``found`` (``(kind, key, detail)``).

    A found row that is already open stays as it is (its first ``found_on`` and ``run_id``); a new one is added;
    an open row of those kinds that is no longer found gets ``resolved_on`` today. Rows of other kinds or datasets
    are not touched. One transaction. Returns ``(added, resolved)``.
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
        have = {(r["kind"], r["key"]): r["id"] for r in open_rows}
        gone = [i for k, i in have.items() if k not in want]
        con.executemany("UPDATE build_issues SET resolved_on = ? WHERE id = ?", [(today, i) for i in gone])
        new = [(dataset, today, k, key, detail, run_id) for (k, key), detail in sorted(want.items())
               if (k, key) not in have]
        con.executemany("INSERT INTO build_issues (dataset, found_on, kind, key, detail, run_id) "
                        "VALUES (?, ?, ?, ?, ?, ?)", new)
    return len(new), len(gone)


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
    from . import caps                              # caps reads ops: imported here, not at module level
    count, cap = _int(kept.get(UW_DAILY_COUNT_HEADER)), caps.cap_for("uw")
    left = cap - count if (cap is not None and count is not None) else None
    with _open() as con:
        con.execute("INSERT OR REPLACE INTO uw_remaining (sampled_at, run_id, daily_req_count, daily_cap, "
                    "daily_remaining_computed, per_minute_remaining, per_minute_reset_ms, token_req_limit, "
                    "raw_headers) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (time.time(), run_id, count, cap, left, _int(kept.get(UW_MINUTE_REMAINING_HEADER)),
                     _int(kept.get(UW_MINUTE_RESET_HEADER)), _int(kept.get(UW_TOKEN_LIMIT_HEADER)), _json(kept)))
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


def watch_add(kind: str, ticker: str, *, by: str, note: str | None = None) -> bool:
    """Add ``ticker`` (upper-cased) to a watch list and its history; False when it was already there."""
    kind, ticker = _watch_kind(kind), ticker.strip().upper()
    with _open() as con, _tx(con):
        return _watch_write(con, kind, ticker, "add", by, note)


def watch_remove(kind: str, ticker: str, *, by: str) -> bool:
    kind, ticker = _watch_kind(kind), ticker.strip().upper()
    with _open() as con, _tx(con):
        return _watch_write(con, kind, ticker, "remove", by, None)


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


def schema_version() -> int | None:
    rows = _select("SELECT max(version) AS v FROM ops_schema")
    return rows[0]["v"] if rows else None


def recent_checks(dataset: str, limit: int = 20) -> list[dict]:
    """The newest ``limit`` ``dataset_check`` rows of ``dataset``, newest first."""
    return _select("SELECT * FROM dataset_check WHERE dataset = ? ORDER BY id DESC LIMIT ?", (dataset, int(limit)))


def latest_unskipped_check(dataset: str) -> dict | None:
    """The newest ``dataset_check`` row of ``dataset`` that is not ``skipped``, however many skips came after it."""
    rows = _select("SELECT * FROM dataset_check WHERE dataset = ? AND result != 'skipped' ORDER BY id DESC LIMIT 1",
                   (dataset,))
    return rows[0] if rows else None


def latest_good_check(dataset: str) -> dict | None:
    """The newest ``changed`` or ``unchanged`` ``dataset_check`` row of ``dataset``: the last run that built."""
    rows = _select("SELECT * FROM dataset_check WHERE dataset = ? AND result IN ('changed', 'unchanged') "
                   "ORDER BY id DESC LIMIT 1", (dataset,))
    return rows[0] if rows else None


def open_issues(dataset: str, limit: int = 20) -> tuple[int, list[dict]]:
    """``(count, newest rows)`` of the unresolved ``build_issues`` of ``dataset``."""
    n = _select("SELECT count(*) AS n FROM build_issues WHERE dataset = ? AND resolved_on IS NULL", (dataset,))
    rows = _select("SELECT * FROM build_issues WHERE dataset = ? AND resolved_on IS NULL ORDER BY id DESC LIMIT ?",
                   (dataset, int(limit)))
    return (n[0]["n"] if n else 0), rows


def ever_built(dataset: str) -> bool:
    """True once ``dataset`` has a ``changed`` or ``unchanged`` check (it has built at least once)."""
    return bool(_select("SELECT 1 AS one FROM dataset_check WHERE dataset = ? AND result IN ('changed', 'unchanged') "
                        "LIMIT 1", (dataset,)))


def open_issue_kinds(dataset: str) -> list[dict]:
    """``[{kind, count}]`` of the unresolved ``build_issues`` of ``dataset``, the most first."""
    return _select("SELECT kind, count(*) AS count FROM build_issues WHERE dataset = ? AND resolved_on IS NULL "
                   "GROUP BY kind ORDER BY count(*) DESC, kind", (dataset,))


def read_queue(finished: int = 20) -> dict:
    """``queue_view`` for a reader: the same rows, without creating the file."""
    active = _select("SELECT * FROM build_requests WHERE status IN ('queued','running') ORDER BY id")
    done = _select("SELECT * FROM build_requests WHERE status NOT IN ('queued','running') ORDER BY id DESC LIMIT ?",
                   (int(finished),))
    return {"active": [_request(r) for r in active], "finished": [_request(r) for r in done]}


def read_schedule(dataset: str) -> dict | None:
    rows = _select("SELECT * FROM dataset_schedule WHERE dataset = ?", (dataset,))
    return rows[0] if rows else None


def calls_by_day(first_day: str, last_day: str) -> list[dict]:
    """``provider_calls_daily`` rows with ``first_day <= ny_day <= last_day``."""
    return _select("SELECT * FROM provider_calls_daily WHERE ny_day BETWEEN ? AND ? ORDER BY provider, ny_day",
                   (first_day, last_day))


def latest_uw_sample() -> dict | None:
    rows = _select("SELECT sampled_at, run_id, daily_req_count, daily_cap, daily_remaining_computed, "
                   "per_minute_remaining, token_req_limit FROM uw_remaining "
                   "ORDER BY sampled_at DESC LIMIT 1")
    return rows[0] if rows else None


def reads_since(first_day: str, tables) -> dict[str, int]:
    """Reads of each of ``tables`` in ``read_log`` since ``first_day`` (every process together)."""
    names = list(tables)
    if not names:
        return {}
    rows = _select(f"SELECT table_name, sum(reads) AS n FROM read_log WHERE day >= ? AND table_name IN "
                   f"({', '.join('?' * len(names))}) GROUP BY table_name", (first_day, *names))
    got = {r["table_name"]: int(r["n"]) for r in rows}
    return {t: got.get(t, 0) for t in names}


def last_builder_run() -> dict | None:
    rows = _select("SELECT run_id, dataset, trigger, pid, started_at, finished_at, status, error FROM job_runs2 "
                   "ORDER BY started_at DESC LIMIT 1")
    return rows[0] if rows else None


def watch_count(kind: str) -> int:
    rows = _select(f"SELECT count(*) AS n FROM watchlist_{_watch_kind(kind)}")
    return rows[0]["n"] if rows else 0


def watch_last_change() -> float | None:
    """The newest add or remove on either watch list (epoch seconds), or None; never creates the file."""
    rows = _select(" UNION ALL ".join(f"SELECT max(at) AS at FROM watchlist_{k}_changes" for k in WATCH_KINDS))
    stamps = [r["at"] for r in rows if r["at"] is not None]
    return max(stamps) if stamps else None


def watch_rows(kind: str) -> list[dict]:
    """The rows of one watch list, by ticker: ``ticker``, ``added_at`` (epoch seconds), ``added_by``, ``note``."""
    return _select(f"SELECT ticker, added_at, added_by, note FROM watchlist_{_watch_kind(kind)} ORDER BY ticker")
