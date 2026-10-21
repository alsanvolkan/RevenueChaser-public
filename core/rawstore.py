"""Single-file raw-data store backed by DuckDB.

All raw caches and derived data live in one ``.store/raw.duckdb`` file instead
of thousands of per-ticker JSON/CSV files (and three SQLite DBs). Every
read/write is ``connect -> do work -> close`` per operation — DuckDB is a
single-writer store (no SQLite-style WAL), so a handle is never held across a
job or a request.

- Writes raise on failure AND are atomic (``write_rows`` / ``replace_all`` /
  ``transaction`` each run in one transaction), so a failed refresh never
  leaves a table half-emptied. Writes retry on lock contention within a stated
  budget (``_WRITE_DELAYS``).
- Reads stay lenient: callers decide what a missing/stale row means; this layer
  raises only on I/O errors. Readers retry on lock with a longer budget
  (``_READ_DELAYS``) so they out-wait a chunked ingest. A lock held by another
  *RevenueChaser* process (dashboard, scheduled job, backup) gets that full
  budget too; only an unknown holder (a VS Code window, a notebook) fails fast.
- The schema manifest (``MANIFEST``) is the single source of truth: the
  migration script (``scripts/migrate_rawdata_to_duckdb.py``) and the coverage
  test (``core/tests/test_db_persistence.py``) both iterate it. The first store
  call in a process applies it (``ensure_all_tables``: renames, merges, views,
  column additions, compressed-column rebuilds), so a fresh or restored store
  never serves silently-empty views.
- Speed: opening the 500 MB file costs ~100 ms, so ``session()`` lets a burst
  of operations (one dashboard request) share one handle; big batches are
  inserted through a registered DataFrame instead of ``executemany`` (~75x).
  Large text/JSON cells DuckDB would store uncompressed are zlib-compressed
  (MANIFEST ``"compress"``), transparently for ``get``/``get_all``/``write_rows``.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import zlib
from contextlib import contextmanager
from pathlib import Path

import duckdb

from RevenueChaser.core import procs
from RevenueChaser.core.config import DATA_DIR

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "raw.duckdb"


def edgar_db_path() -> Path:
    """The EDGAR store file, beside the raw store (follows ``DB_PATH`` redirects)."""
    return Path(DB_PATH).parent / "edgar.duckdb"


def discord_db_path() -> Path:
    """The Discord archive file (#72), beside the raw store (follows ``DB_PATH`` redirects)."""
    return Path(DB_PATH).parent / "discord.duckdb"


def history_db_path() -> Path:
    """The result-history file (#287), beside the raw store (follows ``DB_PATH`` redirects).

    The 17 result-history tables of schema 11 live there; the first ``core/history.record_run`` creates the file
    (``ensure_history``), the first store call does not.
    """
    return Path(DB_PATH).parent / "history.duckdb"


def prices_db_path() -> Path:
    """The daily price file (#156, avo 2026-10-09), beside the raw store (follows ``DB_PATH`` redirects).

    ``price_bars_daily``, ``price_actions`` and their watermarks ``price_checks`` live there, so the price history
    (~2.4 GiB after the fill) stays out of ``raw.duckdb`` and its weekly backups; so do the SPX daily levels
    ``index_levels_daily`` (D03, #293, avo 2026-10-10). The first store call creates it.
    """
    return Path(DB_PATH).parent / "prices.duckdb"


# Table -> database file. Default is "raw"; EDGAR-content tables declare
# ``"db": "edgar"``, the Discord archive ``"db": "discord"``, (from #278) result history ``"db": "history"`` and
# (#156) the daily prices ``"db": "prices"`` in their MANIFEST entry. One writer per file, same
# configuration, is fine (DuckDB allows concurrent connections in one process);
# the split is by write pattern (hot structured vs write-once EDGAR), not per
# domain. See docs/data_architecture.md. Resolved dynamically so a test that
# monkeypatches ``DB_PATH`` redirects both files.
def _db_of(name: str) -> str:
    return (MANIFEST.get(name) or {}).get("db", "raw")


def _db_path(db: str) -> Path:
    if db == "edgar":
        return edgar_db_path()
    if db == "discord":
        return discord_db_path()
    if db == "history":
        return history_db_path()
    if db == "prices":
        return prices_db_path()
    return Path(DB_PATH)

# Write lock-wait budget ~6.9s — the first, tiny delays absorb the in-process
# attach race; the tail absorbs another process holding the lock. Plenty for the
# small per-ticker writes. A long ingest must stay chunked so it never holds the
# file lock for minutes (see module_sec_13f.engine.ingest_zips, which deletes a
# zip's rows before re-inserting so chunking stays crash-safe).
_WRITE_DELAYS = (0.01, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 3.0)
# Reader budget is longer so a reader out-waits a chunked ingest.
_READ_DELAYS = (0.01, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0)

# Serializes only the attach/detach of the database file within one process.
# duckdb.connect() on the SAME path from two threads at the same instant races
# on the library's global instance cache (BinderException "unique file handle
# conflict"), and on Windows a thread's close() can still hold the file lock a
# beat after returning, so a neighbouring thread's reopen raises a lock error.
# Both disappear if the connect() and close() calls themselves cannot overlap —
# the lock is NEVER held around the transaction/statements, so operations still
# overlap exactly as documented (a read during a held write handle works).
_ATTACH_LOCK = threading.Lock()
_SCHEMA_VERSION = "12"


# A manifest "column" entry is either a column definition (``name TYPE ...``) or
# an inline table constraint. Match on the FIRST TOKEN only, so a real column
# named e.g. ``checksum`` or ``uniqueness`` is never mistaken for a constraint.
_CONSTRAINT_KEYWORDS = {"PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT"}


def _columns_of(spec: dict) -> list[str]:
    """Column names from a manifest spec (skipping inline constraint clauses).

    Names are returned UNQUOTED (``'"group" TEXT'`` -> ``'group'``) so they can
    be used both as dict keys on row objects and as SQL identifiers (write_rows
    quotes them for SQL).
    """
    names = []
    for c in spec["columns"]:
        head = re.split(r"[\s(]", c.strip(), maxsplit=1)[0]
        if head.upper() in _CONSTRAINT_KEYWORDS:
            continue
        names.append(head.strip('"'))
    return names


# Every way DuckDB says "someone else has this file right now". All are
# transient and must be retried, not raised:
#   - another PROCESS holds the file lock (IOException);
#   - a same-process handle opened with a different configuration
#     (ConnectionException) — see ``connect``;
#   - two threads racing to attach the file at the same instant
#     (BinderException "unique file handle conflict"), which the in-process
#     dashboard/job-worker split hits regularly.
_CONFLICT_MARKERS = (
    "could not set lock on file",
    "could not open database",
    "being used by another process",
    "locked",
    "conflict on transaction",
    "different configuration",
    "unique file handle conflict",
    "already attached",
)


def _conflict(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _CONFLICT_MARKERS)


# A lock held by a DIFFERENT process. DuckDB phrases this differently per OS:
# Unix "Conflicting lock is held in ... (PID n)", Windows "Cannot open file ...
# being used by another process". Both mean the same 503-able condition.
_FOREIGN_LOCK_MARKERS = (
    "conflicting lock is held",
    "could not set lock on file",
    "being used by another process",
)


class RawStoreError(RuntimeError):
    pass


class StoreLockedError(RawStoreError):
    """The store is held by another process — the one failure with a real fix.

    DuckDB allows a single writer process at a time, so any other program that
    has ``raw.duckdb`` open (a DuckDB CLI, a notebook, the VS Code DuckDB
    extension) blocks the whole app. This carries the holder so the message can
    name it instead of surfacing as an unexplained 500 on every board.
    """

    def __init__(self, path, holder_pid: int | None, holder_name: str,
                 holder_path: str, detail: str):
        self.path = str(path)
        self.holder_pid = holder_pid
        self.holder_name = holder_name
        self.holder_path = holder_path
        self.detail = detail
        super().__init__(self.headline() + " " + self.remedy())

    def holder_label(self) -> str:
        if self.holder_name and self.holder_pid:
            return f"{self.holder_name} (PID {self.holder_pid})"
        if self.holder_pid:
            return f"PID {self.holder_pid}"
        return "another process"

    def headline(self) -> str:
        return (f"The database is currently locked by another process: "
                f"{self.holder_label()}.")

    def remedy(self) -> str:
        """Concrete next step, specialised for the holders we actually see."""
        blob = f"{self.holder_name} {self.holder_path}".lower()
        if "code" in blob and ("helper" in blob or "visual studio" in blob):
            return ('This is VS Code: its DuckDB extension attaches the file for '
                    'the life of the window. Set "attached": false for this '
                    'database in .vscode/settings.json (or detach it in the '
                    'extension), then reload the window (Cmd+Shift+P -> '
                    '"Reload Window").')
        if "duckdb" in blob:
            return ("Quit that DuckDB session (or close the notebook holding it) "
                    "and retry.")
        return (f"Close it and retry. On macOS/Linux: `lsof {self.path}` lists "
                f"every holder"
                + (f"; `kill {self.holder_pid}` if it is safe to stop."
                   if self.holder_pid else "."))


_LOCK_HOLDER_RE = re.compile(
    r"Conflicting lock is held in\s+(?P<path>.+?)\s*\(PID\s*(?P<pid>\d+)\)")


def _process_name(pid: int | None) -> str:
    """Friendly name for a PID, best effort — never raises, never blocks long."""
    if not pid:
        return ""
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                             capture_output=True, text=True, timeout=2)
        name = (out.stdout or "").strip().splitlines()
        if name:
            return Path(name[0].strip()).name
    except Exception:  # pragma: no cover - ps missing or refused
        pass
    return ""


def _lock_error(detail: str, path: Path | None = None) -> StoreLockedError:
    """Build a StoreLockedError from DuckDB's message, enriched via ``ps``."""
    path = Path(path if path is not None else DB_PATH)
    m = _LOCK_HOLDER_RE.search(detail)
    holder_path = m.group("path").strip() if m else ""
    pid = int(m.group("pid")) if m else None
    # Prefer the live process name; fall back to the binary DuckDB named.
    name = _process_name(pid) or (Path(holder_path).name if holder_path else "")
    return StoreLockedError(path, pid, name, holder_path, detail)


def _open_failure_hint(msg: str) -> str:
    """Actionable next step for non-lock open failures."""
    if "different configuration" in msg.lower():
        return (" A handle to this file is already open in this process with a"
                " different configuration — every rawstore handle must be opened"
                " read-write (see docs/data_architecture.md).")
    return ""


# A lock held by an UNKNOWN process is not transient contention — a VS Code
# window or an open notebook holds it for as long as it stays open. Waiting out
# the full read budget (~29s) for that turns every board into a multi-minute
# stall and still fails. Try just long enough to ride out a momentary CLI query,
# then report it. The in-process races keep the full budget: those really do
# clear in milliseconds.
_FOREIGN_LOCK_DELAYS = (0.05, 0.1, 0.2)
# Another RevenueChaser process (the dashboard, a scheduled off-peak job, the
# nightly backup) only ever holds the file for one operation or one chunk, so
# it WILL release it: wait as long as a reader would.
_PEER_LOCK_DELAYS = _READ_DELAYS

# Every RevenueChaser process that touches a store drops ``.rc-proc-<pid>`` next
# to it (removed at exit; a leftover from a crash is ignored once its PID is
# gone). That is how a lock holder is recognised as a peer — on Windows DuckDB's
# lock error does not even name the holder PID. A marker records the holder's
# process start time too, so a reused PID does not read as a live peer (#355);
# and registering sweeps the directory's stale markers, because ``stop.ps1``
# kills the dashboard and ``atexit`` never runs, so leftovers pile up (#368).
_PRESENCE_PREFIX = ".rc-proc-"
_PRESENT_IN: set[str] = set()
_PRESENCE_LOCK = threading.Lock()


def _presence_file(directory: Path, pid: int) -> Path:
    return directory / f"{_PRESENCE_PREFIX}{pid}"


def _read_marker(f: Path) -> tuple[int | None, str | None]:
    """``(pid, start)`` from a marker; ``start`` is ``None`` for the old PID-only format."""
    try:
        parts = f.read_text(encoding="ascii").split()
    except OSError:
        return None, None
    if not parts:
        return None, None
    try:
        pid = int(parts[0])
    except ValueError:
        return None, None
    return pid, (parts[1] if len(parts) > 1 else None)


def _marker_is_stale(pid: int, start: str | None) -> bool:
    """True when the marker's process is gone or its PID was reused by another one.

    An old-format marker (no start time) of a live PID is kept: the PID alone decides.
    A start time that cannot be read (access denied, an unsupported OS) also keeps it.
    """
    if not procs.pid_alive(pid):
        return True
    if start is None:
        return False
    live = procs.process_start_time(pid)
    return live is not None and live != start


def _remove_marker(f: Path) -> None:
    try:
        f.unlink(missing_ok=True)
    except OSError:  # best-effort: a marker we cannot remove is ignored
        logger.debug("could not remove presence marker %s", f, exc_info=True)


def _sweep_presence(directory: Path) -> None:
    """Remove stale ``.rc-proc-*`` markers in a store directory (#368).

    Only marker files are considered; a ``.duckdb``, its ``.wal`` or a lock file
    in the same folder is never touched.
    """
    me = os.getpid()
    for f in Path(directory).glob(f"{_PRESENCE_PREFIX}*"):
        pid, start = _read_marker(f)
        if pid is None or pid == me:
            continue
        if _marker_is_stale(pid, start):
            _remove_marker(f)


def _register_presence(path: Path) -> None:
    d = Path(path).parent
    key = str(d)
    if key in _PRESENT_IN:
        return
    with _PRESENCE_LOCK:
        if key in _PRESENT_IN:
            return
        _sweep_presence(d)
        f = _presence_file(d, os.getpid())
        start = procs.process_start_time(os.getpid()) or ""
        try:
            f.write_text(f"{os.getpid()} {start}".strip(), encoding="ascii")
            atexit.register(f.unlink, missing_ok=True)
        except OSError:  # read-only dir etc.: presence is best-effort
            logger.debug("could not write presence file %s", f, exc_info=True)
        _PRESENT_IN.add(key)


def _peer_pids(path: Path) -> list[int]:
    """Live RevenueChaser processes (other than this one) using ``path``'s dir."""
    me = os.getpid()
    out = []
    for f in Path(path).parent.glob(f"{_PRESENCE_PREFIX}*"):
        pid, start = _read_marker(f)
        if pid is None or pid == me:
            continue
        if _marker_is_stale(pid, start):
            _remove_marker(f)  # a crashed process's leftover, or a reused PID
        else:
            out.append(pid)
    return out


def _holder_is_peer(msg: str, path: Path) -> bool:
    """True when the process holding the lock is another RevenueChaser process."""
    peers = _peer_pids(path)
    m = _LOCK_HOLDER_RE.search(msg)
    if m:  # POSIX names the holder: it must be one of ours
        return int(m.group("pid")) in peers
    return bool(peers)  # Windows does not: any live peer is the likely holder


def _foreign_lock(msg: str) -> bool:
    """True when the message says a *different* process owns the file lock."""
    low = msg.lower()
    if not any(k in low for k in _FOREIGN_LOCK_MARKERS):
        return False
    m = _LOCK_HOLDER_RE.search(msg)
    return not m or int(m.group("pid")) != os.getpid()


def _connect_with_retry(delays: tuple[float, ...], path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open the DuckDB file with retry semantics and attach serialization.

    Combines process-wide attach serialization (_ATTACH_LOCK) with foreign-lock
    detection and actionable StoreLockedError enrichment. The attach/detach are
    serialized; the lock is not held across transactions. A foreign lock held by
    a RevenueChaser peer keeps (at least) the full reader budget; any other
    foreign holder fails fast.
    """
    path = Path(path if path is not None else DB_PATH)
    last: Exception | None = None
    i = 0
    judged = False
    while True:
        try:
            with _ATTACH_LOCK:
                conn = duckdb.connect(str(path))
            _register_presence(path)
            return conn
        except duckdb.Error as exc:
            last = exc
            if not judged and _foreign_lock(str(exc)):
                judged = True
                if _holder_is_peer(str(exc), path):
                    if sum(delays) < sum(_PEER_LOCK_DELAYS):
                        delays = _FOREIGN_LOCK_DELAYS if getattr(_LOCAL, "brief", False) else _PEER_LOCK_DELAYS
                else:
                    delays = _FOREIGN_LOCK_DELAYS
            if not _conflict(exc) or i >= len(delays):
                break
        logger.debug("connect contention, retrying in %.2fs: %s", delays[i] if i < len(delays) else 0, last)
        time.sleep(delays[i] if i < len(delays) else 0)
        i += 1
    detail = str(last).strip().replace("\n", " ") if last else ""
    if _foreign_lock(detail):
        raise _lock_error(detail, path) from last
    raise RawStoreError(
        f"could not open {path}: {detail}{_open_failure_hint(detail)}") from last


@contextmanager
def connect(read_only: bool = False, db: str = "raw"):
    """Open a connection for one operation; always closes. Retries on lock.

    ``db`` selects the store file (``"raw"``, ``"edgar"``, ``"discord"``, ``"history"`` or ``"prices"``). ``read_only`` is
    ADVISORY: it only selects the retry budget, and every handle is opened
    read-write. DuckDB refuses a second connection to the same file with a
    *different* configuration in the same process::

        ConnectionException: Can't open a connection to same database file
        with a different configuration than existing connections

    and this app reads and writes the store concurrently in one process — the
    dashboard's request threads read while ``app/jobs.py``'s in-process worker
    thread runs a module writer. Opening every handle the same way makes those
    coexist; cross-process contention is still handled by the lock retry.

    Inside ``session()`` the operation gets a cursor on the session's handle
    instead of opening the file again (its own connection, so transactions
    stay independent).
    """
    path = _db_path(db)
    held = _session_handle(path, read_only)
    if held is not None:
        cur = held.cursor()
        try:
            yield cur
        finally:
            cur.close()
        return
    conn = _connect_with_retry(delays=_READ_DELAYS if read_only else _WRITE_DELAYS,
                               path=path)
    try:
        yield conn
    finally:
        # Detach is serialized too (see _ATTACH_LOCK): on Windows a close() can
        # leave the file lock held a beat after it returns, which would make a
        # neighbouring thread's connect() fail spuriously.
        with _ATTACH_LOCK:
            conn.close()


# ── sessions: one handle for a burst of operations ──────────────────────────
# Opening the file is the dominant cost of a small operation (~100 ms on the
# 500 MB store: DuckDB loads the catalog and checkpoints on close), while an
# operation on an already-open database costs < 1 ms. A session keeps ONE handle
# per file open for the duration of a ``with`` block on this thread — a
# dashboard request, a render — and closes it at the end, so the file lock is
# still released between bursts and never held across an idle period. While a
# handle is open, other threads' ``connect()`` also re-use the cached database
# instance, so they get cheap too. Handles are opened lazily: a session that
# never touches edgar.duckdb never locks it.
_LOCAL = threading.local()


def _sessions() -> dict:
    s = getattr(_LOCAL, "sessions", None)
    if s is None:
        s = _LOCAL.sessions = {}
    return s


def _session_handle(path: Path, read_only: bool):
    entry = _sessions().get("depth")
    if not entry:
        return None
    handles = _sessions().setdefault("handles", {})
    key = str(path)
    conn = handles.get(key)
    if conn is None:
        conn = _connect_with_retry(delays=_READ_DELAYS if read_only else _WRITE_DELAYS,
                                   path=path)
        handles[key] = conn
    return conn


@contextmanager
def session():
    """Share one handle per store file across every operation in the block.

    Re-entrant (nested sessions share the outer one). Thread-local: other
    threads keep their own semantics. Keep the block short — a held handle
    blocks OTHER processes (a scheduled job waits for it), so wrap one request
    or one render, never a long-running job.
    """
    s = _sessions()
    s["depth"] = s.get("depth", 0) + 1
    try:
        yield
    finally:
        s["depth"] -= 1
        if s["depth"] == 0:
            handles = s.pop("handles", {})
            for conn in handles.values():
                with _ATTACH_LOCK:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001 - closing is best-effort
                        logger.debug("session close failed", exc_info=True)


# DuckDB uses optimistic concurrency, so two transactions that touch the SAME
# row both start fine and the loser fails LATER — after connect, which is why
# the connect-level retry above cannot help and the whole operation is retried.
# It loses in two different ways:
#   - at COMMIT ("Failed to commit: ... duplicate key");
#   - mid-statement, because INSERT ... ON CONFLICT cannot see a row the other
#     transaction has not committed yet, so both attempt a plain insert and one
#     gets "Duplicate key ... violates primary key constraint".
# Reachable whenever two request threads write one row (two rapid
# /api/watchlist/toggle calls) or a request races the app/jobs.py worker.
#
# A *genuine* duplicate-key bug produces the same message; it simply raises
# after the budget instead of immediately.
_COMMIT_CONFLICT_MARKERS = (
    "failed to commit",
    "conflict on transaction",
    "transactioncontext error",
    "violates primary key constraint",
    "violates unique constraint",
)


def _commit_conflict(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _COMMIT_CONFLICT_MARKERS)


def _retry_write(op, delays: tuple[float, ...] = _WRITE_DELAYS):
    """Run a self-contained write, retrying a lost commit race.

    Safe to repeat: a conflicting transaction is rolled back whole, so the
    retry starts from the same state the first attempt saw. A missing table
    (the store was reset/restored under us, invalidating the ``_TABLE_READY``
    memo) is re-created once.
    """
    healed = False
    for i, delay in enumerate(delays):
        try:
            return op()
        except duckdb.CatalogException:
            if healed:
                raise
            healed = True
            _TABLE_READY.clear()
            continue
        except duckdb.Error as exc:
            if not _commit_conflict(exc) or i == len(delays) - 1:
                raise
            logger.debug("write conflict, retrying in %.2fs: %s", delay, exc)
            time.sleep(delay)
    raise RawStoreError("unreachable")  # pragma: no cover


@contextmanager
def _tx(conn):
    """One all-or-nothing unit of work. DuckDB autocommits each statement
    otherwise, so a DELETE+INSERT refresh that fails halfway would commit the
    DELETE and destroy the previous good rows."""
    conn.execute("BEGIN TRANSACTION")
    ready = set(_TABLE_READY)
    try:
        yield conn
    except BaseException:
        _TABLE_READY.difference_update(_TABLE_READY - ready)   # its CREATE rolled back too
        try:
            conn.execute("ROLLBACK")
        except Exception:  # pragma: no cover - rollback of an aborted tx
            logger.debug("rollback failed", exc_info=True)
        raise
    conn.execute("COMMIT")


# ── schema manifest ───────────────────────────────────────────────────────────
# kind: "json_payload" = exact-hash comparable at migration; "tabular" =
# count + key-set + numeric-tolerance comparable.
# write: "upsert" = ON CONFLICT key DO UPDATE; "append" = plain INSERT;
#        "replace" = DELETE rows matching keys, then INSERT.
#
# Column types: a manifest column is written as ``name TYPE``. The storage type
# matters — see docs/plan_store_schema_evolution.md for the full rationale:
#   - scalar columns (TEXT/DOUBLE/...): SELECT-able, filterable in SQL.
#   - ``JSON`` columns: DuckDB's native JSON type, serialized once on write
#     (DuckDB's own Python converter mangles floats in mixed-type arrays, so
#     values are pre-serialized), read back as dicts/lists/scalars by
#     get()/get_all(), and reachable from SQL with path expressions
#     (``payload.market_cap::DOUBLE``).
#   - ``STRUCT(...)`` columns: read back as Python dicts natively.
#   - ``BLOB`` columns listed in ``"compress"`` (``{column: "json"|"text"}``):
#     zlib-compressed on write, decompressed (and JSON-decoded) on read by
#     get()/get_all(). For cells > a few KB, which DuckDB stores uncompressed.
#     Raw SQL sees bytes — read them through get()/get_all().
# group_by: the columns a "replace" batch clears before inserting (default: the
# key). A table written one whole run/entity at a time names that grain here,
# so rows that disappeared from the new batch are removed too.
# history: True marks a table that holds the PAST (the forecast ledger, session
# snapshots, change history; #286). rawstore then refuses to rewrite it:
# ``replace_all``, ``update_columns``, ``delete`` and ``clear`` raise, raw SQL that
# deletes, updates, drops, truncates, merges into or replaces it raises (see
# ``_refuse_history_sql``), and ``write_rows`` becomes first-write-wins whatever the
# ``write`` mode. It needs a ``key`` that is the table's PRIMARY KEY. The flag
# rides the built guard: ``forecast_ledger`` carries it (#286) and so do the schema-11
# change and history tables.
# history_mode: how a table treats the past (data backbone review ADR-1, #281). A label for now:
# rawstore does not read it, ``history`` above is what enforces. Every entry declares one.
#   event            dated facts, one row per event, kept (first write wins)
#   session_snapshot one row per key per NY session, appended
#   change_history   every change of a slow-moving value, with ``valid_from``
#   latest_cache     latest value only, for data that can be fetched or rebuilt again
#   operational      run stamps, pointers and logs of the store itself
HISTORY_MODES = ("event", "session_snapshot", "change_history", "latest_cache", "operational")
_JSON_TYPES = {"JSON"}
_STRUCT_TYPES = {"STRUCT", "UNION"}
_NESTED_TYPES = _JSON_TYPES | _STRUCT_TYPES
MANIFEST: dict[str, dict] = {
    # ── raw caches (json_payload) ────────────────────────────────────────────
    # JSON value columns are DuckDB's native ``JSON`` type (see
    # docs/plan_store_schema_evolution.md): written once as JSON, read back as
    # dicts/list/scalars by get()/get_all(), and reachable from SQL with path
    # expressions (e.g. provenance.py's json_extract on leaps_cache). Key and
    # timestamp columns stay scalar so filters run on real columns.
    "ticker_payload_cache": {
        "history_mode": "session_snapshot",
        "key": ["ticker"],
        "columns": ["ticker TEXT PRIMARY KEY", "payload JSON", "fetched_at DOUBLE", "fetched_at_iso TEXT"],
        "kind": "json_payload",
        "write": "upsert",
        "sources": ["tickers/{TICKER}_raw.json"],
    },
    "edgar_financials": {
        "history_mode": "event",
        # ``annual``/``quarterly`` are ~20 KB JSON lists per ticker; DuckDB stores
        # strings that size uncompressed, so they are zlib-compressed (8x) —
        # transparent for get()/write_rows(). See "compress" below.
        "key": ["ticker"],
        "columns": ["ticker TEXT PRIMARY KEY", "annual BLOB", "quarterly BLOB", "meta JSON",
                    "fetched_at DOUBLE"],
        "compress": {"annual": "json", "quarterly": "json"},
        "kind": "json_payload",
        "write": "upsert",
        "db": "edgar",
        "sources": ["sec_facts/{TICKER}.json"],
    },
    "edgar_documents": {
        "history_mode": "event",
        # Metadata only — the raw HTML lives as files under edgar_docs/
        # (core/edgar_docs.py). Keyed by accession; CIK + accession identify the
        # file. See docs/classification_design.md Phase 4. D16 (#299): append, the
        # first write of an accession wins (a filed document does not change);
        # ``source`` 'sec_archives' and ``run_at`` are added to an existing store by
        # ``_ensure_table`` (ALTER TABLE ADD COLUMN), no schema bump.
        "key": ["accession"],
        "columns": [
            "accession TEXT PRIMARY KEY", "cik TEXT", "tickers TEXT", "form TEXT",
            "filed TEXT", "fiscal_year TEXT", "doc_url TEXT", "path TEXT",
            "sha256 TEXT", "bytes_raw BIGINT", "bytes_gzip BIGINT",
            "fetched_at DOUBLE", "source TEXT", "run_at DOUBLE",
        ],
        "kind": "tabular",
        "write": "append",
        "db": "edgar",
        "sources": [],
    },
    "edgar_facts": {
        "history_mode": "event",
        # XBRL fact spine from the bulk ``companyfacts.zip`` ingest (Phase 4,
        # option A): typed grain ``(cik, concept, period, form)``. Separate from
        # ``edgar_financials`` (per-ticker JSON the financials boards read).
        "key": ["cik", "concept", "period", "form"],
        "columns": [
            "cik TEXT", "concept TEXT", "period TEXT", "form TEXT",
            "value DOUBLE", "unit TEXT",
            "PRIMARY KEY (cik, concept, period, form)",
        ],
        "kind": "tabular",
        "write": "upsert",
        "db": "edgar",
        "sources": [],
    },
    "swing_info": {
        "history_mode": "session_snapshot",
        "key": ["ticker"],
        "columns": ["ticker TEXT PRIMARY KEY", "payload JSON", "cached_at DOUBLE"],
        "kind": "json_payload",
        "write": "upsert",
        "sources": ["swing_potential/info/{TICKER}.json"],
    },
    "leaps_cache": {
        "history_mode": "latest_cache",
        "key": ["ticker"],
        "columns": ["ticker TEXT PRIMARY KEY", "payload JSON"],
        "kind": "json_payload",
        "write": "upsert",
        "sources": ["leaps/cache/{TICKER}.json"],
    },
    "rotation_holdings": {
        "history_mode": "latest_cache",
        "key": ["ticker"],
        "columns": ["ticker TEXT PRIMARY KEY", "date TEXT", "rows JSON"],
        "kind": "json_payload",
        "write": "upsert",
        "sources": ["sector_rotation/holdings/{TICKER}.json"],
    },
    "equity_universe": {
        "history_mode": "change_history",
        # Fully typed from read_json_auto inference of
        # asymmetric_growth_universe.json (see docs/plan_store_schema_evolution.md).
        # Selectable columns: ticker / market_cap / sector / price / name / quote
        # (quote is the 69-field Yahoo screener struct as a JSON column — stored
        # columnar, queryable with path expressions). The universe provenance
        # (generated_at / refreshed_by) is NOT a universe row — it lives in
        # ``equity_universe_provenance`` (one keyed row).
        "key": ["ticker"],
        "columns": [
            "ticker TEXT PRIMARY KEY",
            "market_cap DOUBLE", "sector TEXT", "price DOUBLE", "name TEXT",
            "industry TEXT", "industry_group TEXT", "sub_industry TEXT",
            "quote JSON",
            # Phase 1 (#265): UW's next earnings date, copied by core/identity/apply.py. Added to an existing
            # store by ``_reconcile_table`` (ALTER TABLE ADD COLUMN) on the first store call.
            "next_earnings_date TEXT",
        ],
        "kind": "json_payload",
        "write": "upsert",
        "sources": ["asymmetric_growth_universe.json"],
    },
    "equity_universe_provenance": {
        "history_mode": "latest_cache",
        # Which board last refreshed the shared universe + when, and how the last pull ended. Keys: ``__meta__``
        # (membership), ``__stats__`` (cap backfill), ``__fetch__`` (refreshed_by = full | partial); written by core.universe_fetch.
        "key": ["key"],
        "columns": ["key TEXT PRIMARY KEY", "generated_at TEXT", "refreshed_by TEXT"],
        "kind": "tabular",
        "write": "replace",
        "sources": ["asymmetric_growth_universe_meta.json"],
    },
    "sec_company_tickers": {
        "history_mode": "change_history",
        "key": ["ticker"],
        "columns": ["ticker TEXT PRIMARY KEY", "cik TEXT", "name TEXT"],
        "kind": "json_payload",
        "write": "upsert",
        "sources": ["sec_company_tickers.json"],
    },
    "sec_company_sic": {
        "history_mode": "change_history",
        # SIC per CIK, from data.sec.gov/submissions/CIK{10}.json. The sector
        # source for the shared universe (Yahoo's screener has no sector).
        # See core/sic_registry.py + docs/data_architecture.md.
        "key": ["ticker"],
        "columns": [
            "ticker TEXT PRIMARY KEY", "cik TEXT", "sic TEXT",
            "sic_description TEXT", "fetched_at DOUBLE",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    # ── Co-movement board (module_comovement) ────────────────────────────────
    "comov_runs": {
        "history_mode": "event",
        # One dated clustering run. ``config_hash`` covers window/linkage/cuts
        # plus horizon/benchmark, so a config change produces a new as_of
        # instead of reusing members.
        "key": ["as_of"],
        "columns": [
            "as_of TEXT PRIMARY KEY", "generated_at TEXT", "window_days INTEGER",
            "n_tickers INTEGER", "linkage TEXT", "config_hash TEXT",
            "horizon INTEGER", "benchmark TEXT",
            "benchmark_status TEXT", "prices_through TEXT",
            # n_eff: legacy participation ratio, no longer written (runs
            # before 2026-10-01). n_factors: eigenvalues above the MP edge.
            # null_json: grid point -> shuffled-returns null (Refresh reuses it).
            "n_eff DOUBLE", "n_factors INTEGER", "null_json TEXT",
            # cut -> {"held", "total"}: lagged groups that held out-of-sample.
            "oos_json TEXT",
            # First session of the clustered window, last session of the lagged
            # (formation) window, and the universe funnel the header states.
            "window_start TEXT", "oos_start TEXT", "funnel_json TEXT",
            # #379 D2 (card "Kolon + header"): the Fetch that stored the run (its
            # precompute windows share the id; ``result_runs`` holds one
            # header-only row per Fetch), the run's session (rule B on as_of)
            # and its drop lists {dropped, no_prices, flat_prints}, which a
            # reload cannot rebuild. Added to an existing store by
            # ``_reconcile_table`` (ALTER TABLE ADD COLUMN), no schema bump;
            # older runs read NULL.
            "run_id TEXT", "session_date DATE", "dropped_json TEXT",
        ],
        "kind": "tabular",
        "write": "replace",
        "sources": [],
    },
    "comov_nodes": {
        "history_mode": "event",
        "key": ["as_of", "cut", "cluster_id"],
        # A run is written whole: "replace" clears the run's old clusters.
        "group_by": ["as_of"],
        "columns": [
            "as_of TEXT", "cut INTEGER", "cluster_id TEXT", "label TEXT",
            "n INTEGER", "mean_corr DOUBLE", "last_residual_pct DOUBLE",
            # The shuffled-returns null (q99 of the groups the same pipeline
            # selects from pure noise) this cluster's mean_corr is judged
            # against. NULL → significance unknown (pre-S8 run, or a Refresh
            # with no persisted null for its config hash).
            "baseline_r DOUBLE",
            # Legacy S9 bootstrap stability: no longer written (removed
            # 2026-09-30, replaced by out-of-sample persistence below).
            "stability DOUBLE",
            # Out-of-sample persistence (module_comovement/persistence.py):
            # best Jaccard to a group formed a quarter earlier, whether that is
            # below the match bar (new), and whether the matched group held on
            # the held-out quarter (its r there vs random names). NULL = no
            # lagged test for this run.
            "persist_jaccard DOUBLE", "persist_new BOOLEAN", "persist_held BOOLEAN",
            "oos_r DOUBLE", "oos_base DOUBLE",
        ],
        "kind": "tabular",
        "write": "replace",
        "sources": [],
    },
    "comov_members": {
        "history_mode": "event",
        "key": ["as_of", "cut", "ticker"],
        "group_by": ["as_of"],
        "columns": ["as_of TEXT", "cut INTEGER", "cluster_id TEXT", "ticker TEXT",
                    # ``core``: the name was in the matching group a quarter
                    # ago (persistence.core_members); False = joined since;
                    # NULL = no lagged match. ``stability`` is the legacy S9
                    # bootstrap value, no longer written.
                    "stability DOUBLE", "core BOOLEAN"],
        "kind": "tabular",
        "write": "replace",
        "sources": [],
    },
    "comov_lag": {
        "history_mode": "event",
        # Groups formed on the window ending a quarter before the run, with
        # their held-out-quarter test (Fetch-only). A Refresh re-matches its
        # groups against the latest run's rows with the same config hash.
        "key": ["as_of", "cut", "ticker"],
        "group_by": ["as_of"],
        "columns": ["as_of TEXT", "cut INTEGER", "lag_id TEXT", "ticker TEXT",
                    "oos_r DOUBLE", "oos_base DOUBLE", "held BOOLEAN"],
        "kind": "tabular",
        "write": "replace",
        "sources": [],
    },
    # ── Value Chain board (module_value_chain) ───────────────────────────────
    "chain_filings": {
        "history_mode": "latest_cache",
        # One row per issuer: the latest 10-K (else 20-F) we sliced, with the
        # Item 1/4 text and a quality flag. ``accession`` lets a re-run skip
        # unchanged filings.
        # ``item_text`` (~50 KB of prose per issuer) is zlib-compressed (3x):
        # DuckDB stores strings that size uncompressed. It is re-derivable from
        # the durable edgar_docs/ HTML, so this is a cache of the slice.
        "key": ["ticker"],
        "columns": [
            "ticker TEXT PRIMARY KEY", "cik TEXT", "form TEXT", "filed TEXT",
            "accession TEXT", "primary_doc TEXT", "item_text BLOB", "quality TEXT",
            "fetched_at DOUBLE",
        ],
        "compress": {"item_text": "text"},
        "kind": "tabular",
        "write": "upsert",
        "db": "edgar",
        "sources": [],
    },
    "chain_mentions": {
        "history_mode": "latest_cache",
        # src = filing issuer, dst_name = the mentioned company, relation in
        # supplier / customer / competitor / other. dst_ticker is empty when the
        # name is private/unresolved.
        "key": ["src", "dst_name", "relation"],
        "columns": [
            "src TEXT", "dst_name TEXT", "dst_ticker TEXT", "relation TEXT",
            "snippet TEXT", "filed TEXT",
            # #387 D7 (DF57): the share of the filer's sales or purchases a concentration sentence states
            # ("Apple accounted for 29.8% of our net sales" -> 29.8); NULL on a keyword edge. Added to an
            # existing store, and to the value_chain_edges_history mirror, by ``_reconcile_table``
            # (ALTER TABLE ADD COLUMN) on the first store call.
            "share_pct DOUBLE",
        ],
        "kind": "tabular",
        "write": "replace",
        "db": "edgar",
        "sources": [],
    },
    "chain_meta": {
        "history_mode": "latest_cache",
        "key": ["key"],
        "columns": ["key TEXT PRIMARY KEY", "value TEXT"],
        "kind": "tabular",
        "write": "replace",
        "db": "edgar",
        "sources": [],
    },
    "watchlist": {
        "history_mode": "change_history",
        "key": ["ticker"],
        "columns": ["ticker TEXT PRIMARY KEY"],
        "kind": "json_payload",
        "write": "upsert",
        "sources": ["watchlist_tickers.json"],
    },
    "sec13f_security_meta": {
        "history_mode": "change_history",
        # One shared ``{ticker: {name, sector[, type]}}`` map, merged (union)
        # from the legacy ``universe_meta`` + ``sec13f_universe_meta``.
        "key": ["ticker"],
        "columns": ["ticker TEXT PRIMARY KEY", "entry JSON"],
        "kind": "json_payload",
        "write": "upsert",
        "sources": ["universe_meta.json", "sec_13f/universe_meta.json"],
    },
    "sec13f_enrichment": {
        "history_mode": "session_snapshot",
        "key": ["ticker"],
        "columns": ["ticker TEXT PRIMARY KEY", "entry JSON"],
        "kind": "json_payload",
        "write": "upsert",
        "sources": ["sec_13f/enrichment.json"],
    },
    "etf_holdings": {
        "history_mode": "change_history",       # #303: the latest top 10 per ETF; history in etf_holdings_changes
        "key": ["ticker"],
        "columns": ["ticker TEXT PRIMARY KEY", "entry JSON", "provider TEXT", "fetched_at DOUBLE"],
        "kind": "json_payload",
        "write": "upsert",
        "sources": ["sector_rotation/etf_holdings_cache.json"],
    },
    # (tradewhisperer_price_snapshot / tradewhisperer_closes: the TradeWhisperer
    # board was removed 2026-09-30. Their rows stay in raw.duckdb untouched by
    # user decision — no code reads or writes them, rawstore never drops a table
    # it does not manage, and backups still include them.)
    "cusip_ticker": {
        "history_mode": "change_history",
        "key": ["cusip"],
        "columns": ["cusip TEXT PRIMARY KEY", "entry JSON"],
        "kind": "json_payload",
        "write": "upsert",
        "sources": ["sec_13f/cusip_ticker.json"],
    },
    "sec13f_ticker_names": {
        "history_mode": "change_history",
        "key": ["ticker"],
        "columns": ["ticker TEXT PRIMARY KEY", "entry JSON"],
        "kind": "json_payload",
        "write": "upsert",
        # OpenFIGI-resolved {ticker: {name, figi}}. Read by run_sec13f to seed
        # the CUSIP->ticker name match; nothing in the repo writes it, so it is
        # durable input data, not a regenerable artifact.
        "sources": ["sec_13f/ticker_name.json"],
    },
    "sec13f_totals": {
        "history_mode": "latest_cache",
        "key": ["ticker"],
        "columns": ["ticker TEXT PRIMARY KEY", "entry JSON"],
        "kind": "json_payload",
        "write": "upsert",
        "sources": ["sec_13f/totals.json"],
    },
    # NOTE: append-only calibration logs (decisions/outcomes/tuning) are
    # deliberately NOT stored here — they stay as jsonl files, not DuckDB rows.
    "store_meta": {
        "history_mode": "latest_cache",
        "key": ["key"],
        "columns": ["key TEXT PRIMARY KEY", "value TEXT"],
        "kind": "json_payload",
        "write": "upsert",
        "sources": [],
    },
    "run_registry": {
        "history_mode": "latest_cache",
        # One place for the runtime pins the classification work needs: the
        # profile mode, the current NMF theme run id, migration markers. It holds
        # POINTERS only — `theme_runs` owns the theme catalog itself.
        "key": ["key"],
        "columns": ["key TEXT PRIMARY KEY", "value TEXT"],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    "migration_manifest": {
        "history_mode": "event",
        "key": ["dataset"],
        "columns": [
            "dataset TEXT PRIMARY KEY", "migrated_at TEXT", "schema_version TEXT",
            "source_count INTEGER", "source_bytes INTEGER", "source_hash TEXT", "verify TEXT",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    # ── tabular stores ───────────────────────────────────────────────────────
    "ticker_themes": {
        "history_mode": "event",
        # Runtime theme SSoT. ``theme_id`` is the join key (stable slug), ``theme``
        # is the display label. ``theme_run_id`` separates the legacy keyword
        # themes (Phase 1) from each NMF run (Phase 8); the same theme in a later
        # run is a different row. See core/ticker_themes.py.
        "key": ["ticker", "theme_id", "theme_run_id"],
        "columns": [
            "ticker TEXT", "theme_id TEXT", "theme TEXT", "level TEXT",
            "theme_run_id TEXT",
            "PRIMARY KEY (ticker, theme_id, theme_run_id)",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    "fundamentals_scores": {
        "history_mode": "latest_cache",
        # Per-ticker OWN metrics only (no identity/GICS — those live on
        # equity_universe). Single writer: module_fundamentals/scores.py
        # (replace_all full refresh).
        "key": ["ticker"],
        "columns": [
            "ticker TEXT PRIMARY KEY", "period TEXT", "multiplier INTEGER",
            "distorted INTEGER", "quality_pass INTEGER", "momentum_pass INTEGER",
            "ro40 DOUBLE", "rev_growth_ttm DOUBLE", "fcf_margin DOUBLE",
            "ttm_revenue DOUBLE", "gross_margin DOUBLE", "roic DOUBLE",
            "net_debt_ebitda DOUBLE", "pe DOUBLE", "pb DOUBLE",
            "dist_52w_pct DOUBLE", "rs_6m DOUBLE", "above_200d INTEGER",
            # #386 F1: additive (the history mirror follows through ``_mirror_entry``)
            "ro40_basis TEXT", "anchor_period_end TEXT", "price DOUBLE", "market_cap DOUBLE",
        ],
        "kind": "tabular",
        "write": "replace",
        "sources": [],
    },
    "fundamentals_gates": {
        "history_mode": "latest_cache",
        # One typed row per (ticker, gate, rule) — filterable.
        "key": ["ticker", "gate", "rule_id"],
        "columns": [
            "ticker TEXT", "gate TEXT", "rule_id TEXT", "passed INTEGER",
            "actual DOUBLE", "unit TEXT",
            "PRIMARY KEY (ticker, gate, rule_id)",
        ],
        "kind": "tabular",
        "write": "replace",
        "sources": [],
    },
    "swing_scores": {
        "history_mode": "latest_cache",
        "key": ["ticker"],
        "columns": [
            "ticker TEXT PRIMARY KEY", "multiplier INTEGER", "quality DOUBLE",
            "rs_rating DOUBLE", "price DOUBLE", "change_pct DOUBLE",
            "change_1w DOUBLE", "change_1m DOUBLE", "entry DOUBLE",
            "target DOUBLE", "stop DOUBLE", "rr DOUBLE", "atr DOUBLE",
            "ma_alignment TEXT",
            "direction_ok INTEGER", "m_pass INTEGER", "w_pass INTEGER", "q_pass INTEGER",
        ],
        "kind": "tabular",
        "write": "replace",
        "sources": [],
    },
    "swing_gates": {
        "history_mode": "latest_cache",
        "key": ["ticker", "gate", "rule_id"],
        "columns": [
            "ticker TEXT", "gate TEXT", "rule_id TEXT", "passed INTEGER",
            "actual DOUBLE", "unit TEXT",
            "PRIMARY KEY (ticker, gate, rule_id)",
        ],
        "kind": "tabular",
        "write": "replace",
        "sources": [],
    },
    # ── Relatedness (Phase 6) ────────────────────────────────────────────────
    "llm_cache": {
        "history_mode": "event",
        # Structured cache key over (model, thinking, effort, prompt_version,
        # ticker, input_hash). Response is the raw JSON. Durable (raw.duckdb).
        "key": ["cache_key"],
        "columns": [
            "cache_key TEXT PRIMARY KEY", "ticker TEXT", "cik TEXT", "model TEXT",
            "thinking INTEGER", "effort TEXT", "prompt_version TEXT",
            "input_hash TEXT", "response JSON", "fetched_at DOUBLE", "status TEXT",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    "relatedness_profiles": {
        "history_mode": "latest_cache",
        # One row per ticker; profile job grain is CIK (dual-class shares one
        # item_text), so cache_key is keyed on cik+input_hash.
        "key": ["ticker"],
        "columns": [
            "ticker TEXT PRIMARY KEY", "cik TEXT", "model TEXT",
            "prompt_version TEXT", "fetched_at DOUBLE", "cache_key TEXT",
            "status TEXT",
            # LLM tries that ended in an empty profile (retried up to 3 runs).
            "attempts INTEGER",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    "relatedness_attributes": {
        "history_mode": "latest_cache",
        "key": ["ticker", "field"],
        # A profile is written whole: "replace" clears the ticker's old fields.
        "group_by": ["ticker"],
        "columns": [
            "ticker TEXT", "field TEXT", "value TEXT", "value_num DOUBLE",
            "PRIMARY KEY (ticker, field)",
        ],
        "kind": "tabular",
        "write": "replace",
        "sources": [],
    },
    "relatedness_scores": {
        "history_mode": "latest_cache",
        "key": ["ticker", "peer"],
        "columns": [
            "ticker TEXT", "peer TEXT", "score DOUBLE",
            "PRIMARY KEY (ticker, peer)",
        ],
        "kind": "tabular",
        "write": "replace",
        "sources": [],
    },
    "theme_runs": {
        "history_mode": "event",
        # One NMF theme run (Phase 8). ``is_current`` pins the cut-over run.
        "key": ["id"],
        "columns": [
            "id TEXT PRIMARY KEY", "created_at TEXT", "k_fine INTEGER",
            "k_medium INTEGER", "k_coarse INTEGER", "jaccard DOUBLE",
            "is_current INTEGER", "session_date DATE", "run_at DOUBLE",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    "relatedness_themes": {
        "history_mode": "event",
        "key": ["theme_id"],
        "columns": [
            "theme_id TEXT PRIMARY KEY", "level TEXT", "parent_id TEXT",
            "label TEXT", "size INTEGER",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    "relatedness_theme_tags": {
        "history_mode": "event",
        "key": ["theme_id", "tag"],
        "columns": [
            "theme_id TEXT", "tag TEXT", "rank INTEGER",
            "PRIMARY KEY (theme_id, tag)",
        ],
        "kind": "tabular",
        "write": "replace",
        "sources": [],
    },
    "relatedness_theme_members": {
        "history_mode": "event",
        "key": ["ticker", "theme_id"],
        "columns": [
            "ticker TEXT", "theme_id TEXT", "weight DOUBLE",
            "PRIMARY KEY (ticker, theme_id)",
        ],
        "kind": "tabular",
        "write": "replace",
        "sources": [],
    },
    "relatedness_theme_etfs": {
        "history_mode": "event",
        # Top ETFs whose holdings overlap a theme node (fine/medium/coarse).
        # Written by module_relatedness.themes.match_theme_etfs (store-only).
        "key": ["theme_id", "etf"],
        "columns": [
            "theme_id TEXT", "etf TEXT", "etf_name TEXT", "group_name TEXT",
            "overlap DOUBLE", "rank INTEGER",
            "PRIMARY KEY (theme_id, etf)",
        ],
        "kind": "tabular",
        "write": "replace",
        "group_by": ["theme_id"],
        "sources": [],
    },
    "swing_ohlc": {
        "history_mode": "latest_cache",
        "key": ["ticker", "ts"],
        "group_by": ["ticker"],
        "columns": [
            "ticker TEXT", "ts TEXT", "open DOUBLE", "high DOUBLE", "low DOUBLE",
            "close DOUBLE", "volume DOUBLE", "cached_at DOUBLE",
        ],
        "kind": "tabular",
        "write": "replace",
        "sources": ["swing_potential/ohlc/{TICKER}.csv"],
    },
    "rotation_snapshots": {
        "history_mode": "session_snapshot",
        "key": ["as_of_date", "ticker"],
        "group_by": ["as_of_date"],
        "columns": [
            "as_of_date TEXT", "ticker TEXT", "name TEXT", '"group" TEXT',
            "close_price DOUBLE", "rs_1w DOUBLE", "rs_1m DOUBLE", "rs_3m DOUBLE",
            "rs_ratio DOUBLE", "rs_momentum DOUBLE", "rs_ratio_z DOUBLE",
            "rs_momentum_z DOUBLE", "quadrant TEXT",
            "benchmark TEXT", "generated_at TEXT", "source TEXT",    # source: live (Rotation Fetch) / backfill (#374)
        ],
        "kind": "tabular",
        "write": "replace",
        "sources": ["sector_rotation/snapshots/{ASOF}.json"],
    },
    "index_constituents": {
        "history_mode": "change_history",
        "key": ["slug", "ticker"],
        "group_by": ["slug"],
        "columns": ["slug TEXT", "ticker TEXT"],
        "kind": "tabular",
        "write": "replace",
        "sources": ["indexes/{slug}.json"],
    },
    "leaps_flow_prints": {
        "history_mode": "event",
        "key": ["id"],
        "columns": [
            "id TEXT PRIMARY KEY", "ticker TEXT", "strike DOUBLE", "expiry TEXT",
            "premium DOUBLE", "size INTEGER", "option_type TEXT", "delta DOUBLE",
            "next_earnings_date TEXT", "executed_at TEXT", "discovered_at TEXT",
            "ask_vol DOUBLE", "bid_vol DOUBLE", "source TEXT", "fetched_at DOUBLE",
            "session_date DATE", "run_at DOUBLE",
        ],
        "kind": "tabular",
        "write": "append",
        "sources": ["leaps/radar.db"],
    },
    "leaps_daily_scores": {
        "history_mode": "session_snapshot",
        "key": ["ticker", "date"],
        "columns": [
            "ticker TEXT", "date TEXT", "frequency TEXT", "notional DOUBLE",
            "concentration DOUBLE", "concentration_mult DOUBLE", "final_score DOUBLE",
            "PRIMARY KEY (ticker, date)",
        ],
        "kind": "tabular",
        "write": "append",
        "sources": ["leaps/radar.db"],
    },
    "ticker_scores": {
        "history_mode": "latest_cache",
        "key": ["ticker"],
        "columns": [
            "ticker TEXT PRIMARY KEY", "name TEXT", "sector TEXT", "industry TEXT",
            "industry_group TEXT", "sub_industry TEXT",
            "price DOUBLE", "market_cap DOUBLE", "ps_ratio DOUBLE", "rev_growth DOUBLE",
            "gross_margin DOUBLE", "fcf DOUBLE", "runway_years DOUBLE",
            "rd_intensity DOUBLE", "dilution_yoy DOUBLE", "earnings_date DOUBLE",
            "total_points INTEGER", "tech_breakthrough INTEGER",
            "dilution_warning INTEGER", "us_listed INTEGER", "source TEXT",
            "inst_holders INTEGER", "inst_new INTEGER", "inst_exited INTEGER",
            "inst_buy_flow DOUBLE", "inst_sell_flow DOUBLE", "inst_shares DOUBLE",
            "inst_shares_delta_pct DOUBLE", "inst_ownership_pct DOUBLE",
            "themes TEXT", "signals TEXT",
            # #380 AG W1 (additive): S2 the tier, the C18 flags (JSON list) and the model version that scored the row;
            # S3 the fiscal years and period ends the card read (JSON object)
            "tier INTEGER", "flags TEXT", "model_version TEXT",
            "anchor_fy INTEGER", "prior_fy INTEGER", "period_ends TEXT",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": ["ticker_store/tickers.db"],
    },
    "sec13f_submissions": {
        "history_mode": "event",
        "key": ["accession"],
        "columns": [
            "accession TEXT PRIMARY KEY", "cik TEXT", "period TEXT",
            "filing_date TEXT", "type TEXT",
            "report_type TEXT", "is_amendment BOOLEAN", "amendment_type TEXT",
            "other_managers_count INTEGER",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": ["sec_13f/sec13f.db"],
    },
    "sec13f_holdings": {
        "history_mode": "event",
        "key": ["accession", "line_no"],
        "columns": [
            "accession TEXT", "line_no INTEGER", "cusip TEXT", "cik TEXT",
            "shares DOUBLE", "value DOUBLE", "period TEXT", "putcall TEXT",
            "sshprnamttype TEXT", "title_of_class TEXT", "investment_discretion TEXT",
            "other_manager TEXT", "voting_sole DOUBLE", "voting_shared DOUBLE",
            "voting_none DOUBLE",
            "PRIMARY KEY (accession, line_no)",
        ],
        "kind": "tabular",
        "write": "append",
        "sources": ["sec_13f/sec13f.db"],
    },
    "sec13f_cusip_names": {
        "history_mode": "latest_cache",
        "key": ["cusip"],
        "columns": ["cusip TEXT PRIMARY KEY", "name_norm TEXT", "name_raw TEXT", "count INTEGER"],
        "kind": "tabular",
        "write": "upsert",
        "sources": ["sec_13f/sec13f.db"],
    },
    "sec13f_ingested_zips": {
        "history_mode": "event",
        "key": ["filename"],
        "columns": ["filename TEXT PRIMARY KEY", "ingested_at TEXT"],
        "kind": "tabular",
        "write": "upsert",
        "sources": ["sec_13f/sec13f.db"],
    },
    "sec13f_edgar_backfill": {
        "history_mode": "event",
        "key": ["cik", "period"],
        "columns": ["cik TEXT", "period TEXT", "accession TEXT", "fetched_at TEXT",
                    "PRIMARY KEY (cik, period)"],
        "kind": "tabular",
        "write": "upsert",
        "sources": ["sec_13f/sec13f.db"],
    },
    # Implied Probability (OIPD) board: the UW option chain for one ticker,
    # normalised so the risk-neutral surface can be re-fit on Refresh with zero
    # network. One ticker's chain is replaced per fetch (group_by ticker).
    "oipd_chain": {
        "history_mode": "latest_cache",
        "key": ["ticker", "option_symbol"],
        "group_by": ["ticker"],
        "columns": [
            "ticker TEXT", "option_symbol TEXT", "expiry TEXT", "option_type TEXT",
            "strike DOUBLE", "bid DOUBLE", "ask DOUBLE", "last_price DOUBLE",
            "volume DOUBLE", "open_interest DOUBLE", "iv DOUBLE", "delta DOUBLE",
            "tape_time TEXT", "fetched_at TEXT",
        ],
        "kind": "tabular",
        "write": "replace",
        "sources": [],
    },
    # One row per fetched ticker: the download context (asof/spot/rate/horizon)
    # the fit needs, plus provenance for /api/data_asof.
    "oipd_meta": {
        "history_mode": "latest_cache",
        "key": ["ticker"],
        "columns": [
            "ticker TEXT PRIMARY KEY", "as_of TEXT", "spot DOUBLE",
            "risk_free DOUBLE", "expiry_count INTEGER", "horizon_days INTEGER",
            "fetched_at TEXT",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    # ── Identity master (issue #85, schema v6) ─────────────────────────────
    # One row per ticker: the single answer to "what is this security and what
    # sector is it". Every field carries ``<field>_source`` so a wrong answer
    # can be traced to the provider or rule that produced it. Part 1 only
    # creates the table; the rules that fill it arrive in Part 2.
    "security_master": {
        "history_mode": "change_history",
        "key": ["ticker"],
        "columns": [
            "ticker TEXT PRIMARY KEY", "cik TEXT", "cik_source TEXT",
            "exchange TEXT", "name TEXT", "name_source TEXT",
            "security_type TEXT", "type_source TEXT", "type_asof TEXT",
            "sector TEXT", "sector_source TEXT",
            "industry_group TEXT", "group_source TEXT",
            "industry TEXT", "industry_source TEXT",
            "sub_industry TEXT", "sub_source TEXT", "gics_asof TEXT",
            "updated_at TEXT",
            # Phase 1 (#267): OpenFIGI identifiers, copied from the last good ``openfigi`` row. Added to an existing
            # store by ``_reconcile_table`` (ALTER TABLE ADD COLUMN) on the first store call.
            "figi TEXT", "composite_figi TEXT", "share_class_figi TEXT", "exch_code TEXT",
            "security_description TEXT", "figi_source TEXT",
            # Schema v9 (#272): the raw SIC group, copied from the SEC submissions / ``sec_company_sic`` caches by the
            # identity job: the 4-digit code, the SEC's text, the 2-digit major group, and the cache it came from.
            "sic TEXT", "sic_description TEXT", "sic_group2 TEXT", "sic_source TEXT",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    # Disagreements between two sources (or a source and a rule) on one field
    # of one ticker. first_seen survives re-detection; last_seen moves.
    "identity_conflicts": {
        "history_mode": "change_history",
        "key": ["ticker", "kind", "field"],
        "columns": [
            "ticker TEXT", "kind TEXT", "field TEXT", "side_a TEXT", "side_b TEXT",
            "note TEXT", "first_seen TEXT", "last_seen TEXT",
            "PRIMARY KEY (ticker, kind, field)",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    # Last GOOD answer per (source, ticker). An errored or skipped source never
    # overwrites or deletes these rows (core/identity/cache.py), which is what
    # keeps a field from going blank when a provider is down. Fields readers
    # filter or join on are typed columns; the rest of the answer is ``payload``.
    # ``ticker`` holds the CIK for the per-CIK ``sec_submissions`` source when
    # no ticker is known.
    "identity_source_cache": {
        "history_mode": "change_history",
        "key": ["source", "ticker"],
        "columns": [
            "source TEXT", "ticker TEXT", "fetched_at TEXT", "status TEXT",
            "security_type TEXT", "security_type2 TEXT",
            "sector TEXT", "industry TEXT", "sub_industry TEXT",
            "cik TEXT", "sic TEXT", "is_etf BOOLEAN",
            "forms TEXT", "shell_closed BOOLEAN", "payload JSON",
            "PRIMARY KEY (source, ticker)",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    # One row per identity refresh: per-source status/counts/error.
    "identity_runs": {
        "history_mode": "event",
        "key": ["run_id"],
        "columns": [
            "run_id TEXT PRIMARY KEY", "started_at TEXT", "finished_at TEXT",
            "status TEXT", "sources JSON", "counts JSON",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    # One row per job run (#124 PL5, schema v8). Manifest only: the writers and
    # readers arrive with PL5 / the Automations panel (#130). Times are epoch DOUBLEs.
    "job_runs": {
        "history_mode": "event",
        "key": ["run_id"],
        "columns": [
            "run_id TEXT PRIMARY KEY", "job_id TEXT", "module TEXT", "lane TEXT",
            "trigger TEXT", "source TEXT", "pid BIGINT", "registry_name TEXT",
            "params JSON", "queued_at DOUBLE", "started_at DOUBLE", "finished_at DOUBLE",
            "status TEXT", "error TEXT", "message TEXT", "counts JSON",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    # Daily bars, one row per (ticker, New York session date) (#156; key session_date since v11).
    # Complete sessions only: the writer (core/prices.ensure_daily_bars) drops a session that is not
    # complete at the run (core/calendar.latest_complete_session). OHLC are nominal as served at
    # fetched_at (split-adjusted up to then); later splits and dividends are rows of price_actions,
    # applied on read by core/prices.daily_bars. split_ratio stays NULL. append: the first write of
    # a key wins and a bar is never rewritten (History C review S4). In prices.duckdb (#156, avo 2026-10-09),
    # with price_actions and price_checks, so one chunk commits all three in one transaction (R5).
    "price_bars_daily": {
        "history_mode": "event",
        "key": ["ticker", "session_date"],
        "columns": [
            "ticker TEXT", "session_date DATE", "open DOUBLE", "high DOUBLE", "low DOUBLE", "close DOUBLE",
            "adj_close DOUBLE", "volume DOUBLE", "split_ratio DOUBLE", "adj_source TEXT", "source TEXT",
            "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE",
            "PRIMARY KEY (ticker, session_date)",
        ],
        "kind": "tabular",
        "write": "append",
        "db": "prices",
        "sources": [],
    },
    # Biotech catalysts (#3 B3, schema v8). Keyed on the source's own event id, so a
    # slipped date updates the row instead of adding a second one. Manifest only:
    # the ClinicalTrials.gov / FDA / 8-K fetchers (#186-#192) write it later.
    "catalyst_events": {
        "history_mode": "change_history",
        "key": ["source", "source_event_id"],
        "columns": [
            "ticker TEXT", "event_date DATE", "date_precision TEXT", "kind TEXT",
            "title TEXT", "source TEXT", "source_event_id TEXT", "source_url TEXT",
            "confidence TEXT", "nct_id TEXT", "fetched_at DOUBLE",
            "PRIMARY KEY (source, source_event_id)",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    # FDA orphan designations per ticker (#190, schema v8; source: the FDA Orphan Drug
    # Products database, OOPD). OOPD lists one record per marketing approval:
    # ``approvals`` holds every {marketing_approval_date, trade_name}, the typed
    # columns the earliest one. A NULL ``date_designated`` aborts the batch, so the
    # writer skips and reports such rows. Manifest only: the writer is #190's.
    "orphan_designations": {
        "history_mode": "change_history",
        "key": ["ticker", "generic_name", "designation", "date_designated"],
        "columns": [
            "ticker TEXT", "generic_name TEXT", "designation TEXT", "date_designated DATE",
            "designation_status TEXT", "approval_status TEXT",
            "marketing_approval_date DATE", "trade_name TEXT", "approvals JSON",
            "sponsor TEXT", "fetched_at DOUBLE",
            "PRIMARY KEY (ticker, generic_name, designation, date_designated)",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    # The outcome of each ticker's OOPD lookup (#190): matched | unmatched | no_hits |
    # failed, so "nothing found" differs from "never searched". Readers of
    # orphan_designations gate on ``match``.
    "orphan_lookups": {
        "history_mode": "event",
        "key": ["ticker", "session_date"],
        "columns": [
            "ticker TEXT", "session_date DATE", "sec_name TEXT", "query TEXT", "match TEXT",
            "provider_status TEXT", "n_rows INTEGER", "candidates JSON", "error TEXT", "source TEXT",
            "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE",
            "PRIMARY KEY (ticker, session_date)",
        ],
        "kind": "tabular",
        "write": "append",
        "sources": [],
    },
    # The shared forecast ledger (#128 / #62 D-3, schema v8): every board's frozen
    # forecasts in one table. Forecast-only and append-only (decision 5-B): ``append``
    # is ON CONFLICT DO NOTHING, so the first write for a key wins and nothing is
    # ever updated; outcomes go to the derived ``forecast_outcomes`` table below
    # (62b-1). ``target_date`` (the session the forecast resolves
    # on) is in the key so a 0DTE and a weekly SPX pin on one day both persist; for
    # picks it is the forecast date. Manifest only: the writer is core/ledger.py (#128).
    # ``history: True`` (#286) makes rawstore enforce the append-only rule, so the
    # frozen picks can no longer be rewritten by a cleanup script or a refactor.
    "forecast_ledger": {
        "history": True,
        "history_mode": "session_snapshot",
        "key": ["source", "forecast_date", "subject", "target_type", "target_date"],
        "columns": [
            "source TEXT", "forecast_date DATE", "subject TEXT", "target_type TEXT",
            "target_date DATE NOT NULL", "frozen_at DOUBLE", "run_id TEXT",
            "model_version TEXT", "rank INTEGER", "score DOUBLE", "bucket TEXT",
            "probs JSON", "market_cap DOUBLE", "sector TEXT", "shown_price DOUBLE",
            "stop DOUBLE", "target DOUBLE",
            "PRIMARY KEY (source, forecast_date, subject, target_type, target_date)",
        ],
        "kind": "tabular",
        "write": "append",
        "sources": [],
    },
    # What happened after each frozen forecast (#62 62b-1, #380 S0; decision 5-B): derived from
    # ``forecast_ledger`` and the D01 bars, recomputed, never a fact of its own, so it is a keyed upsert
    # (``latest_cache``) beside the append-only ledger. Key = the ledger's key + ``horizon_days``: a pick
    # gets one row per horizon (30 and 90 calendar days); a dated target (an SPX pin, its own writer
    # later) gets one row with ``horizon_days`` = target_date - forecast_date, so no key column is NULL.
    # ``ref_price``/``mfe``/``mae`` are on the D01 nominal basis; ``status`` is open / closed / no_bars / split_check.
    # Additive (no schema bump). Writer: core/outcomes.py.
    "forecast_outcomes": {
        "history_mode": "latest_cache",
        "key": ["source", "forecast_date", "subject", "target_type", "target_date", "horizon_days"],
        "columns": [
            "source TEXT", "forecast_date DATE", "subject TEXT", "target_type TEXT",
            "target_date DATE NOT NULL", "horizon_days INTEGER", "ref_price DOUBLE", "mfe DOUBLE",
            "mae DOUBLE", "sessions_seen INTEGER", "status TEXT", "computed_at DOUBLE",
            "model_version TEXT", "run_id TEXT",
            "PRIMARY KEY (source, forecast_date, subject, target_type, target_date, horizon_days)",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    # Per-board editable settings (issue #40, schema v6). Only values a user
    # OVERRODE are stored; a default is read from the board's config at call time
    # and Reset deletes the rows (core/board_settings.py). ``value`` is numeric:
    # every editable setting today is an int or a float.
    "board_settings": {
        "history_mode": "change_history",
        "key": ["board", "key"],
        "columns": [
            "board TEXT", "key TEXT", "value DOUBLE", "updated_at TEXT",
            "PRIMARY KEY (board, key)",
        ],
        "kind": "tabular",
        "write": "upsert",
        "sources": [],
    },
    # ── Discord archive (#72 S1) — its own file, .store/discord.duckdb ──────────
    # Third-party message text. Stored verbatim; escaping is the report's job.
    # ``text_verbatim``: get/get_all never re-parse a TEXT cell that looks like a
    # JSON object/array (a message ``{"a": 1}`` or a channel named ``[]``).
    "discord_channels": {
        "history_mode": "change_history",
        "key": ["channel_id"],
        "columns": [
            "channel_id TEXT PRIMARY KEY", "guild_id TEXT", "guild_name TEXT",
            "name TEXT", "category TEXT", "topic TEXT", "kind TEXT",
            "enabled BOOLEAN", "discovered_at TEXT", "last_export_at TEXT",
        ],
        "kind": "tabular",
        "write": "upsert",
        "text_verbatim": True,
        "db": "discord",
        "sources": [],
    },
    "discord_messages": {
        "history_mode": "event",
        "key": ["message_id"],
        "columns": [
            "message_id TEXT PRIMARY KEY", "channel_id TEXT", "author_id TEXT",
            "author_name TEXT", "is_bot BOOLEAN", "kind TEXT", "ts TEXT",
            "edited_ts TEXT", "content TEXT", "is_pinned BOOLEAN",
            "reply_to_id TEXT", "reference_id TEXT", "reference_kind TEXT",
            "attachments JSON", "embeds JSON", "reactions JSON", "mentions JSON",
            "raw BLOB",
        ],
        # The full DCE message object: what Discord may delete is not recoverable.
        "compress": {"raw": "json"},
        "kind": "tabular",
        "write": "upsert",
        "text_verbatim": True,
        "db": "discord",
        "sources": [],
    },
    "discord_export_runs": {
        "history_mode": "event",
        "key": ["run_id"],
        "columns": [
            "run_id TEXT PRIMARY KEY", "channel_id TEXT", "started_at TEXT",
            "finished_at TEXT", "after_id TEXT", "message_count BIGINT",
            "status TEXT", "detail TEXT",
        ],
        "kind": "tabular",
        "write": "upsert",
        "text_verbatim": True,
        "db": "discord",
        "sources": [],
    },
    # G1 bump (#292, schema v10): 22 raw/edgar tables, manifest only, no writers yet
    "price_actions": {
        "key": ["ticker", "ex_date", "action"],
        "columns": [
            "ticker TEXT", "ex_date DATE", "action TEXT", "value DOUBLE", "session_date DATE",
            "source TEXT", "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE",
            "PRIMARY KEY (ticker, ex_date, action)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "event", "db": "prices", "sources": [],
    },
    # The price watermarks (#156): per ticker, when the actions were last downloaded (price_actions, yfinance) and
    # when the period="max" fill was tried (price_bars_daily, yfinance_max). The shape of source_checks, which stays
    # in raw for the identity stamps (D22); this one sits in prices.duckdb beside the bars it describes.
    "price_checks": {
        "key": ["dataset", "source", "key"],
        "columns": [
            "dataset TEXT", "source TEXT", "key TEXT", "checked_at DOUBLE", "run_id TEXT",
            "PRIMARY KEY (dataset, source, key)",
        ],
        "kind": "tabular", "write": "upsert", "history_mode": "operational", "db": "prices", "sources": [],
    },
    # D03 (#293, avo 2026-10-10): beside D01 in prices.duckdb, decided before its writer; a store from before has the
    # empty G1 table moved over by _PRICE_MOVES. No _SCHEMA_VERSION bump.
    "index_levels_daily": {
        "key": ["symbol", "session_date"],
        "columns": [
            "symbol TEXT", "session_date DATE", "open DOUBLE", "high DOUBLE", "low DOUBLE",
            "close DOUBLE", "volume DOUBLE", "source TEXT", "fetched_at DOUBLE", "run_id TEXT",
            "run_at DOUBLE", "PRIMARY KEY (symbol, session_date)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "event", "db": "prices", "sources": [],
    },
    "quote_snapshot": {
        "key": ["ticker", "session_date", "source"],
        "columns": [
            "ticker TEXT", "session_date DATE", "source TEXT", "market_cap DOUBLE", "price DOUBLE",
            "shares_outstanding DOUBLE", "week_52_high DOUBLE", "week_52_low DOUBLE", "payload BLOB",
            "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE",
            "PRIMARY KEY (ticker, session_date, source)",
        ],
        "compress": {"payload": "json"},
        "kind": "tabular", "write": "append", "history_mode": "session_snapshot", "sources": [],
    },
    "short_interest": {
        "key": ["ticker", "session_date"],
        "columns": [
            "ticker TEXT", "session_date DATE", "market_date DATE", "short_interest DOUBLE",
            "si_float DOUBLE", "total_float DOUBLE", "days_to_cover DOUBLE", "fee_rate DOUBLE",
            "rebate_rate DOUBLE", "short_shares_available DOUBLE", "payload JSON",
            "source TEXT", "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE",
            "PRIMARY KEY (ticker, session_date)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "session_snapshot", "sources": [],
    },
    "vendor_financials": {
        "key": ["ticker", "source", "statement", "report_type", "period_end", "run_at"],
        "columns": [
            "ticker TEXT", "source TEXT", "statement TEXT", "report_type TEXT", "period_end DATE",
            "valid_from DATE", "status TEXT", "revenue DOUBLE", "gross_profit DOUBLE",
            "operating_income DOUBLE", "ebitda DOUBLE", "net_income DOUBLE", "operating_cash_flow DOUBLE",
            "total_assets DOUBLE", "inventory DOUBLE", "receivables DOUBLE", "deferred_revenue DOUBLE",
            "rpo DOUBLE", "total_debt DOUBLE", "interest_expense DOUBLE", "payload JSON",
            "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE",
            "PRIMARY KEY (ticker, source, statement, report_type, period_end, run_at)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "change_history", "sources": [],
    },
    "vendor_valuation": {
        "key": ["ticker", "source", "session_date"],
        "columns": [
            "ticker TEXT", "source TEXT", "session_date DATE", "enterprise_value DOUBLE",
            "ebitda DOUBLE", "total_revenue DOUBLE", "net_income_to_common DOUBLE", "trailing_pe DOUBLE",
            "beta DOUBLE", "currency TEXT", "long_business_summary TEXT", "payload BLOB",
            "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE",
            "PRIMARY KEY (ticker, source, session_date)",
        ],
        "compress": {"payload": "json"},
        "kind": "tabular", "write": "append", "history_mode": "session_snapshot", "sources": [],
    },
    "earnings_history": {
        "key": ["ticker", "endpoint", "report_date", "run_at"],
        "columns": [
            "ticker TEXT", "endpoint TEXT", "report_date DATE", "valid_from DATE", "status TEXT",
            "report_time TEXT", "actual_eps DOUBLE", "eps_estimate DOUBLE", "surprise_pct DOUBLE",
            "expected_move_perc DOUBLE", "post_earnings_move_1d DOUBLE", "fiscal_period_end DATE",
            "report_type TEXT", "payload JSON", "source TEXT", "fetched_at DOUBLE", "run_id TEXT",
            "run_at DOUBLE", "PRIMARY KEY (ticker, endpoint, report_date, run_at)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "change_history", "sources": [],
    },
    "estimate_snapshots": {
        "key": ["ticker", "session_date", "period"],
        "columns": [
            "ticker TEXT", "session_date DATE", "period TEXT",
            "eps_est_analysts DOUBLE", "eps_est_avg DOUBLE", "eps_est_low DOUBLE", "eps_est_high DOUBLE",
            "eps_est_year_ago DOUBLE", "eps_est_growth DOUBLE",
            "rev_est_analysts DOUBLE", "rev_est_avg DOUBLE", "rev_est_low DOUBLE", "rev_est_high DOUBLE",
            "rev_est_year_ago DOUBLE", "rev_est_growth DOUBLE",
            "eps_trend_current DOUBLE", "eps_trend_7d DOUBLE", "eps_trend_30d DOUBLE",
            "eps_trend_60d DOUBLE", "eps_trend_90d DOUBLE",
            "eps_rev_up_7d DOUBLE", "eps_rev_up_30d DOUBLE", "eps_rev_down_7d DOUBLE",
            "eps_rev_down_30d DOUBLE", "payload JSON",
            "source TEXT", "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE",
            "PRIMARY KEY (ticker, session_date, period)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "session_snapshot", "sources": [],
    },
    "identity_cusip": {
        "key": ["cusip", "run_at"],
        "columns": [
            "cusip TEXT", "valid_from DATE", "status TEXT", "ticker TEXT", "figi TEXT",
            "share_class_figi TEXT", "share_class TEXT", "name TEXT", "exch_code TEXT",
            "security_type TEXT", "source TEXT", "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE",
            "PRIMARY KEY (cusip, run_at)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "change_history", "sources": [],
    },
    "ownership_snapshot": {
        "key": ["ticker", "session_date", "source"],
        "columns": [
            "ticker TEXT", "session_date DATE", "source TEXT", "shares_outstanding DOUBLE",
            "insider_pct DOUBLE", "institutional_pct DOUBLE", "fetched_at DOUBLE", "run_id TEXT",
            "run_at DOUBLE", "PRIMARY KEY (ticker, session_date, source)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "session_snapshot", "sources": [],
    },
    "sec_mf_file": {
        "key": ["class_id", "run_at"],
        "columns": [
            "class_id TEXT", "valid_from DATE", "status TEXT", "cik TEXT", "series_id TEXT",
            "symbol TEXT", "source TEXT", "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE",
            "PRIMARY KEY (class_id, run_at)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "change_history", "sources": [],
    },
    "sec_exchange_file": {
        "key": ["ticker", "run_at"],
        "columns": [
            "ticker TEXT", "valid_from DATE", "status TEXT", "cik TEXT", "name TEXT", "exchange TEXT",
            "source TEXT", "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE",
            "PRIMARY KEY (ticker, run_at)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "change_history", "sources": [],
    },
    "earnings_calendar": {
        "key": ["session_date", "earnings_date", "ticker", "report_session"],
        "columns": [
            "session_date DATE", "earnings_date DATE", "ticker TEXT", "report_session TEXT",
            "sector TEXT", "full_name TEXT", "marketcap DOUBLE", "expected_move_perc DOUBLE",
            "street_mean_est DOUBLE", "pre_earnings_close DOUBLE", "post_earnings_close DOUBLE",
            "report_time TEXT", "item JSON", "source TEXT", "fetched_at DOUBLE", "run_id TEXT",
            "run_at DOUBLE", "PRIMARY KEY (session_date, earnings_date, ticker, report_session)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "session_snapshot", "sources": [],
    },
    "macro_series": {
        "key": ["series_id", "obs_date", "source"],
        "columns": [
            "series_id TEXT", "obs_date DATE", "source TEXT", "value DOUBLE", "frequency TEXT",
            "proxy_for TEXT", "session_date DATE", "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE",
            "PRIMARY KEY (series_id, obs_date, source)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "event", "sources": [],
    },
    "vol_index_daily": {
        "key": ["symbol", "session_date", "source"],
        "columns": [
            "symbol TEXT", "session_date DATE", "source TEXT", "open DOUBLE", "high DOUBLE",
            "low DOUBLE", "close DOUBLE", "operator TEXT", "fetched_at DOUBLE", "run_id TEXT",
            "run_at DOUBLE", "PRIMARY KEY (symbol, session_date, source)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "event", "sources": [],
    },
    "cftc_cot": {
        "key": ["report_type", "market", "report_date"],
        "columns": [
            "report_type TEXT", "market TEXT", "report_date DATE", "release_date DATE",
            "open_interest DOUBLE",
            "asset_mgr_long DOUBLE", "asset_mgr_short DOUBLE", "asset_mgr_change_long DOUBLE",
            "lev_money_long DOUBLE", "lev_money_short DOUBLE", "lev_money_change_long DOUBLE",
            "dealer_long DOUBLE", "dealer_short DOUBLE", "dealer_change_long DOUBLE",
            "raw_row JSON", "session_date DATE", "source TEXT", "fetched_at DOUBLE", "run_id TEXT",
            "run_at DOUBLE", "PRIMARY KEY (report_type, market, report_date)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "event", "sources": [],
    },
    "economic_calendar": {
        "key": ["session_date", "event_ts", "event", "type"],
        "columns": [
            "session_date DATE", "event_ts DOUBLE", "event TEXT", "type TEXT", "forecast TEXT",
            "prev TEXT", "reported_period TEXT", "raw_row JSON", "source TEXT", "fetched_at DOUBLE",
            "run_id TEXT", "run_at DOUBLE", "PRIMARY KEY (session_date, event_ts, event, type)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "session_snapshot", "sources": [],
    },
    "source_checks": {
        "key": ["dataset", "source", "key"],
        "columns": [
            "dataset TEXT", "source TEXT", "key TEXT", "checked_at DOUBLE", "run_id TEXT",
            "PRIMARY KEY (dataset, source, key)",
        ],
        "kind": "tabular", "write": "upsert", "history_mode": "operational", "sources": [],
    },
    "sec13f_filers": {
        "key": ["accession"],
        "columns": [
            "accession TEXT PRIMARY KEY", "cik TEXT", "period TEXT", "name TEXT", "source TEXT",
            "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE",
        ],
        "kind": "tabular", "write": "append", "history_mode": "event", "sources": [],
    },
    "edgar_companyfacts": {
        "key": ["cik", "taxonomy", "concept", "unit", "period_start", "period_end", "accn"],
        "columns": [
            "cik TEXT", "taxonomy TEXT", "concept TEXT", "unit TEXT", "period_start TEXT",
            "period_end DATE", "accn TEXT", "value DOUBLE", "form TEXT", "fy INTEGER", "fp TEXT",
            "filed DATE", "source TEXT", "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE",
            "PRIMARY KEY (cik, taxonomy, concept, unit, period_start, period_end, accn)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "event", "db": "edgar", "sources": [],
    },
    "sec_submissions": {
        "key": ["cik", "accession"],
        "columns": [
            "cik TEXT", "accession TEXT", "form TEXT", "filing_date DATE", "primary_document TEXT",
            "items TEXT", "source TEXT", "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE",
            "PRIMARY KEY (cik, accession)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "event", "db": "edgar", "sources": [],
    },
    "sec_submissions_cik": {
        "key": ["cik", "run_at"],
        "columns": [
            "cik TEXT", "valid_from DATE", "status TEXT", "name TEXT", "sic TEXT", "sic_description TEXT",
            "entity_type TEXT", "tickers JSON", "exchanges JSON", "source TEXT", "fetched_at DOUBLE",
            "run_id TEXT", "run_at DOUBLE", "PRIMARY KEY (cik, run_at)",
        ],
        "kind": "tabular", "write": "append", "history_mode": "change_history", "db": "edgar",
        "sources": [],
    },
}


# ── Schema 11: one history model (history redesign C, 2026-10-07) ────────────────────────────────────────────────
# Change history (raw.duckdb). A live table that declares ``CHANGES`` keeps its key, its writers and its readers.
# The store itself appends a rule-A row to ``<table>_changes`` when a tracked value of a key differs from that key's
# latest change row, and an 'absent' row when a key leaves the table, in the SAME transaction as the write: both
# tables live in raw.duckdb (``_capture_changes``). "Current" is the live table; "as of" reads the change table.
#   track:      compared columns (a difference appends a row)
#   carry:      copied into the row, never compared (a re-derivation stamp is not a change, rule A)
#   fetched_at: the live column holding the fetch time, written as rule A's ``fetched_at`` (epoch); else NULL
#   source:     rule A's ``source`` when the table says it: a constant ('manual'); without one it is the writer, the
#               dashboard job that wrote (app/jobs registers it) or the label of a sync. A table keyed on its own
#               ``source`` (the provider) has no second one.
#   rename:     live -> change column, where the live name is a rule-A column (identity_source_cache.status)
CHANGES: dict[str, dict] = {
    # Membership (presence) and the slow reference columns. market_cap, price and quote change every pull: D04
    # (quote_snapshot) keeps them per session.
    "equity_universe": {"track": ["name", "sector", "industry", "industry_group", "sub_industry",
                                  "next_earnings_date"]},
    # Every decided value and its source. updated_at, type_asof and gics_asof move on a re-derivation without a
    # change (core/identity/master.py:197-198), so the two as-of stamps are carried and updated_at is left out.
    "security_master": {"track": ["cik", "cik_source", "exchange", "name", "name_source", "security_type",
                                  "type_source", "sector", "sector_source", "industry_group", "group_source",
                                  "industry", "industry_source", "sub_industry", "sub_source", "figi",
                                  "composite_figi", "share_class_figi", "exch_code", "security_description",
                                  "figi_source", "sic", "sic_description", "sic_group2", "sic_source"],
                        "carry": ["type_asof", "gics_asof"]},
    # The source answers behind type, sector and CIK; none can be fetched again for a past date (06 §7, D22/D23).
    # The payload is carried, not compared: it holds values that move every pull (uw_sector market_cap and
    # next_earnings_date, core/identity/sources/uw_sector.py:79-81; SPDR weight, spdr.py:66-67; FDIC assets_k,
    # fdic.py:67-69; everything not lifted lands there, core/identity/cache.py:46).
    "identity_source_cache": {"track": ["status", "security_type", "security_type2", "sector", "industry",
                                        "sub_industry", "cik", "sic", "is_etf", "forms", "shell_closed"],
                              "carry": ["payload"], "fetched_at": "fetched_at", "rename": {"status": "row_status"}},
    "index_constituents": {"track": []},                             # membership per slug
    "watchlist": {"track": [], "source": "manual"},                  # membership (06 D36)
    "board_settings": {"track": ["value"], "source": "manual"},     # updated_at is a save stamp, not a change
    # Provider entities that change in place (06 D32, D33). Their writers (#186-#192, #190) upsert the current row,
    # as the v8 entries say; a slipped date or a new approval is recorded here, so neither table changes key.
    "catalyst_events": {"track": ["ticker", "event_date", "date_precision", "kind", "title", "source_url",
                                  "confidence", "nct_id"], "fetched_at": "fetched_at"},
    "orphan_designations": {"track": ["designation_status", "approval_status", "marketing_approval_date",
                                      "trade_name", "approvals", "sponsor"], "fetched_at": "fetched_at",
                            "source": "fda_orphan"},
    "etf_holdings": {"track": ["entry", "provider"], "fetched_at": "fetched_at"},  # D25 #303: no past list exists
}


def _spec_base_type(spec: dict, col: str) -> str:
    """The declared type of ``col`` without constraints (``"TEXT PRIMARY KEY"`` -> ``"TEXT"``)."""
    for c in spec["columns"]:
        head, _, rest = c.strip().partition(" ")
        if head.strip('"') == col:
            t = (rest.strip() or "TEXT").upper()
            for stop in (" PRIMARY", " NOT ", " DEFAULT", " UNIQUE", " CHECK", " REFERENCES"):
                i = t.find(stop)
                if i >= 0:
                    t = t[:i]
            return t.strip() or "TEXT"
    raise KeyError(f"{col} is not a column of this entry")


def _has_primary_key(spec: dict) -> bool:
    """Whether a MANIFEST entry declares a PRIMARY KEY (on a column or as a table constraint)."""
    return any("PRIMARY KEY" in c.upper() for c in spec["columns"])


def _change_stamp(live: str) -> list[str]:
    """Rule A columns of a change table (06 §2 A), in J6's order; no ``source`` when the live key has one."""
    has_source = "source" in MANIFEST[live]["key"]
    return ["valid_from DATE", "status TEXT", *([] if has_source else ["source TEXT"]), "fetched_at DOUBLE",
            "run_id TEXT", "run_at DOUBLE"]


def _changes_entry(live: str, cfg: dict) -> dict:
    spec = MANIFEST[live]
    key, ren = list(spec["key"]), cfg.get("rename", {})
    cols = [f'{ren.get(c, c)} {_spec_base_type(spec, c)}'
            for c in key + cfg["track"] + cfg.get("carry", [])]
    pk = [ren.get(c, c) for c in key] + ["run_at"]
    return {"key": pk, "columns": cols + _change_stamp(live) + [f"PRIMARY KEY ({', '.join(pk)})"],
            "kind": "tabular", "write": "append", "history": True, "history_mode": "change_history",
            "changes_of": live, "sources": []}


for _live, _cfg in CHANGES.items():
    MANIFEST[f"{_live}_changes"] = _changes_entry(_live, _cfg)


# Result history (history.duckdb, #287). One shape for every board: a header row per board run in ``result_runs``
# (board, run_id) carrying everything that is per run (session_date, run_at, quality, model_version, params, the
# funnel, the board payload), and run-keyed row tables (run_id + the row's own key) carrying only row values. Every
# Fetch run gets a header. A run board keeps every run's rows; a session board keeps one run per session, the first
# complete one (rule B), and marks it ``session_snapshot`` (``core/history.record_run``, which writes the header and
# its rows in ONE transaction on the history file).
_RUN = {"kind": "tabular", "write": "append", "history": True, "history_mode": "event", "db": "history",
        "sources": []}
# ADR-1 mode of a row table: a session board's rows are one run per session; a run (or quarter) board's are events.
_MODE = {"session": "session_snapshot", "run": "event", "quarter": "event"}


def _mirror_entry(latest: str, board: str, snapshot: str) -> dict:
    """History of a latest result table: its own columns and types, plus ``run_id`` first in the key.

    The board passes the same rows it gave the latest table, so the two cannot drift column by column."""
    spec = MANIFEST[latest]
    body = [f"{c.strip().split()[0]} {_spec_base_type(spec, c.strip().split()[0].strip(chr(34)))}"
            for c in spec["columns"] if c.strip().split()[0].upper() not in _CONSTRAINT_KEYWORDS]
    key, extra = ["run_id", *spec["key"]], []
    if not _has_primary_key(spec):
        # A latest table with no PRIMARY KEY (replace mode: chain_mentions, oipd_chain) may hold its key twice, and a
        # history write keeps the first row per key (#286). Its rows are keyed by their place in the run instead,
        # ``row_no``, which record_run numbers, so the history holds every row the latest table holds.
        key, extra = ["run_id", "row_no"], ["row_no INTEGER"]
    entry = {"key": key, "columns": ["run_id TEXT", *extra, *body, f"PRIMARY KEY ({', '.join(key)})"], **_RUN,
             "history_mode": _MODE[snapshot], "board": board, "snapshot": snapshot, "mirror_of": latest}
    if spec.get("compress"):
        entry["compress"] = dict(spec["compress"])
    return entry


def _run_entry(key: list[str], columns: list[str], board: str, snapshot: str, compress: dict | None = None) -> dict:
    entry = {"key": key, "columns": [*columns, f"PRIMARY KEY ({', '.join(key)})"], **_RUN,
             "history_mode": _MODE[snapshot], "board": board, "snapshot": snapshot}
    if compress:
        entry["compress"] = compress
    return entry


# ``board`` is the dashboard tab key of the board that writes the rows (app/dashboard.py TABS). Money Flow is a panel
# of the Sector Rotation tab with its own Fetch job (``moneyflow``, app/dashboard.py:178), so its runs get their own
# board id, module_money_flow, and never compete with Rotation runs for a session snapshot.
HISTORY: dict[str, dict] = {
    "result_runs": {
        "key": ["board", "run_id"],
        "columns": [
            "board TEXT", "run_id TEXT", "session_date DATE", "run_at DOUBLE", "quality TEXT", "quality_note TEXT",
            "session_snapshot BOOLEAN",
            "model_version TEXT", "params JSON", "context JSON", "funnel JSON", "row_counts JSON", "warnings JSON",
            "error TEXT", "payload BLOB",
            "PRIMARY KEY (board, run_id)",
        ],
        "compress": {"payload": "json"}, **_RUN,
    },
    # Card 4: every screener score and gate (mirrors of the latest tables).
    "ag_scores_history": _mirror_entry("ticker_scores", "module_asymmetric_growth", "session"),
    "swing_scores_history": _mirror_entry("swing_scores", "module_swing_potential", "session"),
    "swing_gates_history": _mirror_entry("swing_gates", "module_swing_potential", "session"),
    "fundamentals_scores_history": _mirror_entry("fundamentals_scores", "module_fundamentals", "session"),
    "fundamentals_gates_history": _mirror_entry("fundamentals_gates", "module_fundamentals", "session"),
    # Card 10: peer scores per run (mirror); the payload is result_runs.payload.
    "relatedness_scores_history": _mirror_entry("relatedness_scores", "module_relatedness", "run"),
    # Card 12: Value Chain edges per run (mirror of chain_mentions); the payload is result_runs.payload.
    "value_chain_edges_history": _mirror_entry("chain_mentions", "module_value_chain", "run"),
    # Card 6: the chain each OIPD fit used (mirror of oipd_chain; a partial chain is kept, its run says 'partial').
    "oipd_chain_history": _mirror_entry("oipd_chain", "module_oipd", "run"),
    # Card 5 + card 7: every Earnings card with the option inputs it was scored on.
    "earnings_cards_history": _run_entry(["run_id", "ticker"], [
        "run_id TEXT", "ticker TEXT", "earnings_date DATE", "earnings_time TEXT", "fiscal_quarter TEXT",
        "final_action TEXT", "direction_score DOUBLE", "volatility_edge_score DOUBLE", "confidence_score DOUBLE",
        "r2_score DOUBLE", "r3_score DOUBLE", "r4_score DOUBLE", "r5_score DOUBLE", "r7_score DOUBLE",
        "r8_score DOUBLE", "bias_label TEXT", "conviction TEXT", "macro_veto BOOLEAN", "expected_move_pct DOUBLE",
        "data_completeness DOUBLE", "pre_score DOUBLE", "filter_status TEXT", "filter_reasons TEXT",
        "trade_read TEXT", "attention_score DOUBLE", "edge_ratio DOUBLE", "nrc DOUBLE", "nrc_tier TEXT",
        "nrc_growth DOUBLE", "nrc_acceleration DOUBLE", "nrc_quality DOUBLE", "nrc_beat DOUBLE",
        "watchlist BOOLEAN", "card BLOB", "slots JSON", "inputs BLOB",
    ], "module_earnings_radar", "run", {"card": "json", "inputs": "json"}),
    # Card 6: LEAPS footprints, one per watched ticker per run.
    "leaps_footprint_history": _run_entry(["run_id", "ticker"], [
        "run_id TEXT", "ticker TEXT", "name TEXT", "label TEXT", "score INTEGER", "flow_score INTEGER",
        "far_call_oi DOUBLE", "far_put_oi DOUBLE", "far_call_share DOUBLE", "far_oi_growth_1d_pct DOUBLE",
        "far_oi_growth_q_pct DOUBLE", "far_call_sweep_pct DOUBLE", "iv_rank DOUBLE", "iv_rank_slope DOUBLE",
        "near_term_event BOOLEAN", "as_of DATE", "frequency INTEGER", "notional DOUBLE", "net_notional DOUBLE",
        "concentration DOUBLE", "concentration_mult DOUBLE", "ask_share DOUBLE", "itm_conc DOUBLE",
        "otm_conc DOUBLE", "fetch_status TEXT", "inputs BLOB",
    ], "module_leaps", "session", {"inputs": "json"}),
    # Card 6: every OIPD fitted distribution, one row per expiry.
    "oipd_fit_history": _run_entry(["run_id", "ticker", "expiry"], [
        "run_id TEXT", "ticker TEXT", "expiry DATE", "as_of TEXT", "spot DOUBLE", "risk_free DOUBLE",
        "horizon_days INTEGER", "n_contracts INTEGER", "dte INTEGER", "t_years DOUBLE", "forward DOUBLE",
        "n_strikes INTEGER", "atm_iv DOUBLE", "g_min DOUBLE", "g_min_window DOUBLE", "clipped_pct DOUBLE",
        "min_var DOUBLE", "density_flag BOOLEAN", "density_note TEXT", "density_g_min_shown DOUBLE",
        "rmse DOUBLE", "mean DOUBLE", "p05 DOUBLE",
        "p10 DOUBLE", "p25 DOUBLE", "median DOUBLE", "p75 DOUBLE", "p90 DOUBLE", "p95 DOUBLE",
        "prob_below_spot DOUBLE", "prob_above_spot DOUBLE", "p_up10 DOUBLE", "p_down10 DOUBLE", "greeks JSON",
        "svi JSON", "curve BLOB", "smile BLOB",
    ], "module_oipd", "run", {"curve": "json", "smile": "json"}),
    # Card 9: the Rotation panels and the money-flow leaderboard per session.
    "rotation_holdings_history": _run_entry(["run_id", "etf", "holding"], [
        "run_id TEXT", "etf TEXT", "holding TEXT", "group_name TEXT", "position INTEGER", "name TEXT",
        "weight DOUBLE", "close DOUBLE", "rs_1w DOUBLE", "rs_1m DOUBLE", "rs_3m DOUBLE",
    ], "module_sector_rotation", "session"),
    # The RRG points a live Rotation Fetch saved (avo's card, #374), keyed by row_no: rotation_snapshots has no PK.
    "rotation_snapshots_history": _mirror_entry("rotation_snapshots", "module_sector_rotation", "session"),
    "sector_tree_history": _run_entry(["run_id", "sector", "industry", "ticker"], [
        "run_id TEXT", "sector TEXT", "industry TEXT", "ticker TEXT", "etf TEXT", "name TEXT", "market_cap DOUBLE",
        "weight DOUBLE",
    ], "module_sector_rotation", "session"),
    "money_flow_history": _run_entry(["run_id", "side", "rank"], [
        "run_id TEXT", "side TEXT", "rank INTEGER", "etf TEXT", "name TEXT", "flow_score DOUBLE", "as_of DATE",
    ], "module_money_flow", "session"),
    # Card 11: the 13F board metrics per run; a quarter's snapshot is its last run (view).
    "sec13f_metrics_history": _run_entry(["run_id", "ticker"], [
        "run_id TEXT", "ticker TEXT", "quarter DATE", "name TEXT", "sector TEXT", "shares DOUBLE", "value DOUBLE",
        "holders INTEGER", "top10_pct DOUBLE", "hhi DOUBLE", "new_holders INTEGER", "exited_holders INTEGER",
        "buy_flow DOUBLE", "sell_flow DOUBLE", "streak INTEGER", "accel DOUBLE", "pct_out DOUBLE", "price DOUBLE",
        "price_delta DOUBLE", "shares_outstanding DOUBLE", "float_shares DOUBLE", "short_pct DOUBLE",
        "insider_pct DOUBLE", "shares_by_quarter JSON", "chg_by_quarter JSON", "cusips JSON",
    ], "module_sec_13f", "quarter"),
    # Card 12: every Financials report.
    "financials_report_history": _run_entry(["run_id", "ticker"], [
        "run_id TEXT", "ticker TEXT", "sector TEXT", "modules_run JSON", "peers JSON", "assumptions JSON",
        "data_fetched_at TEXT", "warnings JSON", "quick_remarks JSON", "modules BLOB",
    ], "module_financials", "run", {"modules": "json"}),
}
MANIFEST.update(HISTORY)


def table_names() -> list[str]:
    return sorted(MANIFEST)


def _ddl(name: str) -> str:
    spec = MANIFEST[name]
    cols = ",\n    ".join(spec["columns"])
    return f"CREATE TABLE IF NOT EXISTS {name} (\n    {cols}\n)"


def _check(name: str) -> dict:
    spec = MANIFEST.get(name)
    if spec is None:
        raise RawStoreError(f"unknown table in manifest: {name}")
    return spec


def _refuse_history(name: str, spec: dict, action: str) -> None:
    """Raise when ``action`` would rewrite a ``"history": true`` table (#286)."""
    if spec.get("history"):
        raise RawStoreError(
            f"{name} is a history table (MANIFEST \"history\": true): {action} would rewrite the past. "
            f"History is only ever added to: write_rows keeps the first row written for a key.")


# Raw-SQL guard (#286, #308). DuckDB reads the text, not a copy of its tokenizer: ``extract_statements`` splits
# and types it with DuckDB's own parser, and ``json_serialize_plan`` binds it against this connection's catalog,
# so the operator and its target table are the ones DuckDB would run. Every review round found text that the
# hand-written scanner this replaced read differently from DuckDB (#284 findings 1 and 5, N1, N2, N5 and N6:
# qualified names, dollar and E'' literals, ONLY, name characters, the parser's unicode-space pre-pass, ``\r``
# ending a ``--`` comment). Measured on DuckDB 1.5.6 in scripts/research/308_sql_guard_parser.py and
# .hermes/plans_claude/arch/2026-10-04_308_sql_guard_parser.md.
#
# A rewrite is a plan node that deletes (TRUNCATE plans as a DELETE), updates, drops, creates over (``CREATE OR
# REPLACE``), or merges with an UPDATE or DELETE branch (``INSERT ... ON CONFLICT DO UPDATE`` and ``INSERT OR
# REPLACE`` plan as that merge), or an INSERT whose conflict action is neither THROW nor NOTHING. The target is
# matched by name in any catalog or schema. Fail closed: text that spells a flagged name and that DuckDB cannot
# parse or plan is refused. DuckDB plans every statement of the text before any runs, so a table the text
# creates first is unknown to the plan and such text is refused too; send it as separate calls.
_PLAN_SQL = "SELECT json_serialize_plan(?::VARCHAR)"
_PLAN_VERBS = {
    "LOGICAL_DELETE": "DELETE or TRUNCATE",
    "LOGICAL_UPDATE": "UPDATE",
    "LOGICAL_DROP": "DROP",
    "LOGICAL_CREATE_TABLE": "CREATE OR REPLACE TABLE",
    "LOGICAL_MERGE_INTO": "MERGE INTO, INSERT ... ON CONFLICT DO UPDATE or INSERT OR REPLACE",
    "LOGICAL_INSERT": "INSERT with a conflict action that rewrites",
}
_MERGE_KEEPS = {"MERGE_INSERT", "MERGE_DO_NOTHING", "MERGE_ERROR"}
_INSERT_KEEPS = {"THROW", "NOTHING"}


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _plan_rewrites(node) -> list[tuple[str, str | None]]:
    """``(operator, target)`` of every node in a ``json_serialize_plan`` tree that rewrites a table."""
    found: list[tuple[str, str | None]] = []
    if isinstance(node, list):
        for item in node:
            found += _plan_rewrites(item)
        return found
    if not isinstance(node, dict):
        return found
    # A part in a shape this DuckDB version does not use reads as empty or None, which refuses, never allows.
    op, table, info = node.get("type"), _as_dict(node.get("table_info")), _as_dict(node.get("info"))
    if op in ("LOGICAL_DELETE", "LOGICAL_UPDATE"):
        found.append((op, table.get("table")))
    elif op == "LOGICAL_MERGE_INTO":
        actions = set()
        for branch in node.get("actions") or []:
            todo = _as_dict(branch).get("value")
            actions |= {_as_dict(a).get("action_type") for a in (todo if isinstance(todo, list) else [None])}
        if not actions or actions - _MERGE_KEEPS:
            found.append((op, table.get("table")))
    elif op == "LOGICAL_INSERT" and node.get("action_type") not in _INSERT_KEEPS:
        found.append((op, table.get("table")))
    elif op == "LOGICAL_DROP":
        found.append((op, info.get("name")))
    elif op == "LOGICAL_CREATE_TABLE" and info.get("on_conflict") == "REPLACE_ON_CONFLICT":
        found.append((op, info.get("table")))
    for value in node.values():
        if isinstance(value, (dict, list)):
            found += _plan_rewrites(value)
    return found


# The canary (#308): True once this process has seen this DuckDB serialize the plan shapes `_plan_rewrites`
# reads, False when a shape it does not know means the walker cannot be trusted. None = not yet run.
_PLANNER_OK: bool | None = None
# One statement per rewriting shape, planned against the canary's scratch table ``{t}``. Each must plan as a rewrite
# of that table; a test pins this list, so dropping a shape from it fails.
_CANARY_SQL = (
    "DELETE FROM {t}",
    "TRUNCATE {t}",
    "UPDATE {t} SET v = 'x'",
    "DROP TABLE {t}",
    "CREATE OR REPLACE TABLE {t} AS SELECT 1 AS id, 'x' AS v",
    "INSERT OR REPLACE INTO {t} VALUES (1, 'x')",
    "INSERT INTO {t} VALUES (1, 'x') ON CONFLICT (id) DO UPDATE SET v = 'x'",
)


def _planner_understood() -> bool:
    """Whether ``_plan_rewrites`` reads this DuckDB's plan JSON, checked once per process.

    Plans one statement of each rewriting shape (``_CANARY_SQL``: DELETE, TRUNCATE, UPDATE, DROP, CREATE OR
    REPLACE, INSERT OR REPLACE, INSERT ... ON CONFLICT DO UPDATE) against a scratch table and requires a rewrite aimed at
    that table for each. Called only for text that names a flagged table and is not all-SELECT; any miss
    or exception is False, so an unknown plan shape refuses rather than reads as nothing to rewrite.
    """
    global _PLANNER_OK
    if _PLANNER_OK is not None:
        return _PLANNER_OK
    _PLANNER_OK = False
    try:
        with duckdb.connect(":memory:") as conn:
            scratch = "t_planner_canary"
            conn.execute(f"CREATE TABLE {scratch} (id INTEGER PRIMARY KEY, v VARCHAR)")
            for template in _CANARY_SQL:
                sql = template.format(t=scratch)
                plan = json.loads(conn.execute(_PLAN_SQL, [sql]).fetchone()[0])
                targets = {(t or "").casefold() for _, t in _plan_rewrites(plan.get("plans"))}
                if scratch.casefold() not in targets:
                    return False
        _PLANNER_OK = True
    except Exception:
        return False
    return _PLANNER_OK


def _history_rewrite(conn, sql: str) -> tuple[str, str, bool] | None:
    """``(table, what, checked)`` when ``sql`` would rewrite a history table on ``conn``, else None.

    ``checked`` is False when the text names a history table and DuckDB could not parse or plan it; ``what`` is
    then DuckDB's error."""
    flagged = {n.casefold(): n for n, spec in MANIFEST.items() if spec.get("history")}
    low = sql.casefold()
    named = [n for f, n in flagged.items() if f in low]
    if not named:
        # DuckDB matches a table name only as these ASCII letters in any case (no Unicode folding, and U&""
        # identifiers are not implemented: measured in #308), so text without the name cannot reach the table.
        return None
    try:
        statements = conn.extract_statements(sql)
        if all(s.type == duckdb.StatementType.SELECT for s in statements):
            return None
        if not _planner_understood():
            return named[0], "this DuckDB's plan JSON is not the shape the guard reads", False
        plan = json.loads(conn.execute(_PLAN_SQL, [sql]).fetchone()[0])
    except duckdb.Error as e:
        return named[0], f"{type(e).__name__}: {str(e).splitlines()[0]}", False
    if plan.get("error"):
        return named[0], str(plan.get("error_message")).splitlines()[0], False
    if not isinstance(plan.get("plans"), list) or not plan.get("plans"):
        return named[0], "this DuckDB's plan JSON has no statement list", False
    for op, target in _plan_rewrites(plan.get("plans")):
        if target is None:
            return named[0], _PLAN_VERBS[op], True
        if target.casefold() in flagged:
            return flagged[target.casefold()], _PLAN_VERBS[op], True
    return None


def _refuse_history_sql(conn, sql: str) -> None:
    """Raise when raw ``sql`` would rewrite a history table on ``conn``.

    Refused, however the table is spelled (``raw.main.t``, ``"T"``, ``ONLY (t)``) and whatever hides it from a
    text reader: whatever DuckDB plans as DELETE (and TRUNCATE), UPDATE, DROP, CREATE OR REPLACE TABLE, or a
    MERGE with an UPDATE or DELETE branch (and so ``INSERT ... ON CONFLICT DO UPDATE`` and ``INSERT OR
    REPLACE``), plus any text naming the table that DuckDB cannot parse or plan. Covers ``execute``,
    ``transaction().execute`` and ``query``. Outside it: ``ALTER``, and a caller's own ``connect()`` handle.
    """
    hit = _history_rewrite(conn, sql)
    if not hit:
        return
    table, what, checked = hit
    if checked:
        _refuse_history(table, MANIFEST[table], f"raw SQL {what}")
    raise RawStoreError(
        f"{table} is a history table (MANIFEST \"history\": true) and this raw SQL names it, but DuckDB could not "
        f"parse or plan it ({what}), so it could not be checked and is refused. DuckDB plans every statement of "
        f"one call before any runs: send a statement that needs an earlier one's table as its own call. A parameter "
        f"in a select list must be cast, for example ?::VARCHAR.")


def _ensure_table(conn, name: str) -> None:
    """DDL on an EXISTING connection — so a write needs one handle, not two.

    Two steps so a store that predates a MANIFEST column addition keeps
    working (see docs/plan_store_schema_evolution.md, "Still worth doing"):
      1. ``CREATE TABLE IF NOT EXISTS`` (no-op when the table already exists);
      2. ``ALTER TABLE ... ADD COLUMN`` for any manifest column the existing
         table is missing — a plain CREATE IF NOT EXISTS would silently skip
         the new column and the first write would fail with a Binder Error.
      3. a one-time rebuild of a table whose ``compress`` columns predate it.

    Memoised per (file, table) for the life of the process: the DDL plus the
    ``information_schema`` probe cost ~40 ms per write otherwise.
    """
    key = (str(_db_path(_db_of(name))), name)
    if key in _TABLE_READY:
        return
    existing = {r[0]: r[1] for r in conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = ?", [name]).fetchall()}
    _reconcile_table(conn, name, existing)
    _TABLE_READY.add(key)


# (store file, table) pairs already reconciled with MANIFEST in this process.
_TABLE_READY: set[tuple[str, str]] = set()


def _reconcile_table(conn, name: str, existing: dict[str, str],
                     live_pks: dict[str, list[str]] | None = None) -> None:
    """Create ``name`` or bring an existing table up to its MANIFEST spec.

    ``existing`` maps column -> DuckDB data type (empty = table missing). ``live_pks`` maps table ->
    PRIMARY KEY columns for the whole file when the caller already probed it (``_ensure_db_tables``).
    Only a missing column is fixed; every other difference is recorded by ``_note_drift`` (#285).
    """
    if not existing:
        conn.execute(_ddl(name))
        return
    stale = [c for c in (MANIFEST[name].get("compress") or {})
             if c in existing and existing[c].upper() != "BLOB"]
    _note_drift(conn, name, existing, stale, live_pks)
    for c in MANIFEST[name]["columns"]:
        head = c.strip().split(None, 1)[0].strip('"')
        if head in _CONSTRAINT_KEYWORDS:
            continue
        if head not in existing:
            conn.execute(f'ALTER TABLE {name} ADD COLUMN "{head}" {c.split(None, 1)[1]}')
    if stale:
        _rebuild_compressed(conn, name, stale)
        _forget_drift(name)


# ── schema drift (#285, B10) ─────────────────────────────────────────────────────────────────
# ``_reconcile_table`` fixes one difference: a MANIFEST column the live table lacks. A retyped column,
# a changed key or constraint, and a live column the MANIFEST dropped it used to skip without a word
# (and a ``compress`` rebuild then drops the extra column for good). They are recorded here and logged,
# once per (file, table, kind, column) per process. ``schema_drift()`` is what Settings / Data Health will
# list. Nothing migrates by itself. #312 item 1: ``ensure_all_tables`` also keeps what it saw in
# ``store_meta`` (key ``schema_drift``, see ``_persist_drift``), so the record outlives the process.
_SCHEMA_DRIFT: dict[tuple, dict] = {}
_DRIFT_LOCK = threading.Lock()
_TYPE_CANON: dict[str, str] = {}


def schema_drift() -> list[dict]:
    """Differences between the live store files and the MANIFEST that ``_ensure_table`` left alone.

    One dict per difference: ``db``, ``table``, ``kind`` (``type``, ``key`` or ``extra_column``),
    ``column`` (None for a key), ``live`` (the live type, or the live PRIMARY KEY columns) and
    ``manifest`` (what the MANIFEST declares). A copy, ordered by table; empty when the store matches.
    """
    with _DRIFT_LOCK:
        return [dict(d) for _, d in sorted(_SCHEMA_DRIFT.items(), key=lambda kv: tuple(map(str, kv[0])))]


def _record_drift(name: str, kind: str, column: str | None, live, manifest, rebuilt: bool = False) -> None:
    """Record one difference and log it once. ``rebuilt``: a ``compress`` rebuild of this table follows,
    which recreates it from the MANIFEST, so the difference is replaced, not skipped."""
    db = _db_of(name)
    k = (db, name, kind, column)
    seen = getattr(_LOCAL, "drift_seen", None)
    if seen is not None:                 # inside ensure_all_tables: what this store's apply saw (#312)
        seen.add(k)
    with _DRIFT_LOCK:
        if k in _SCHEMA_DRIFT:
            return
        _SCHEMA_DRIFT[k] = {"db": db, "table": name, "kind": kind, "column": column,
                            "live": live, "manifest": manifest}
    what = {"type": f"column {column!r} is {live}, the MANIFEST says {manifest}",
            "key": f"PRIMARY KEY is {live}, the MANIFEST declares {manifest}",
            "extra_column": f"column {column!r} ({live}) is not in the MANIFEST"}[kind]
    outcome = ("the one-time compress rebuild that follows recreates the table from the MANIFEST, "
               "so this difference is replaced (an extra column's data is dropped)" if rebuilt
               else "skipped, nothing migrates by itself")
    logger.warning("rawstore schema drift in %s.%s: %s; %s", db, name, what, outcome)


DRIFT_META_KEY = "schema_drift"


def _persist_drift(seen: set[tuple]) -> None:
    """Keep the drift ``ensure_all_tables`` saw in ``store_meta`` (#312 item 1); best effort, never raises.

    ``seen`` holds the keys recorded during this apply, so a difference another store file showed earlier in the
    same process (``schema_drift()`` is per process) is not written into this one. The row is
    ``{"recorded_at": <UTC, e.g. 2026-10-15T01:00:00Z>, "drift": [<schema_drift() dicts>]}`` and is written only when the list differs
    from the stored one: a store with drift gets it, a store whose drift is gone gets an empty list (the record
    clears), and a fresh store or an unchanged list writes nothing. ``store_meta`` is a latest table: the row says
    what the last apply saw; the history stays in the log. Runs only inside ``ensure_all_tables``, which opens
    every file read-write; with ``RC_STORE_AUTO_ENSURE=0`` (the read-only protocol) nothing calls it.
    """
    try:
        with _DRIFT_LOCK:
            drift = [dict(d) for k, d in sorted(_SCHEMA_DRIFT.items(), key=lambda kv: tuple(map(str, kv[0])))
                     if k in seen]
        row = get("store_meta", DRIFT_META_KEY)
        stored = row["value"] if row else None
        if (stored["drift"] if isinstance(stored, dict) else None) == drift or (stored is None and not drift):
            return
        write_rows("store_meta", [{"key": DRIFT_META_KEY, "value": {
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "drift": drift}}])
    except Exception:  # noqa: BLE001 - recording the drift must never fail the first store call
        logger.warning("rawstore: could not record the schema drift in store_meta", exc_info=True)


def _forget_drift(name: str) -> None:
    """Drop the records of ``name`` after its ``compress`` rebuild recreated it from the MANIFEST: what
    they describe is gone, and ``schema_drift()`` lists differences that exist. The log line stays."""
    db = _db_of(name)
    with _DRIFT_LOCK:
        for k in [k for k in _SCHEMA_DRIFT if k[:2] == (db, name)]:
            del _SCHEMA_DRIFT[k]


def _declared_pk(spec: dict) -> list[str]:
    """The PRIMARY KEY columns a MANIFEST entry declares (inline or as a clause), in order; [] if none."""
    for c in spec["columns"]:
        c = c.strip()
        if c.upper().startswith("PRIMARY KEY") and "(" in c:
            return [x.strip().strip('"') for x in c[c.index("(") + 1:c.rindex(")")].split(",")]
    for c in spec["columns"]:
        head = c.strip().split(None, 1)[0]
        if head.upper() not in _CONSTRAINT_KEYWORDS and " PRIMARY KEY" in c.upper():
            return [head.strip('"')]
    return []


def _live_primary_keys(conn, table: str | None = None) -> dict[str, list[str]]:
    """``{table: PRIMARY KEY columns}`` of one store file (or just ``table``)."""
    sql = ("SELECT table_name, constraint_column_names FROM duckdb_constraints() "
           "WHERE schema_name = 'main' AND constraint_type = 'PRIMARY KEY'")
    params: list = []
    if table:
        sql += " AND table_name = ?"
        params = [table]
    return {t: list(cols) for t, cols in conn.execute(sql, params).fetchall()}


# ── key rebuilds (schema 11) ─────────────────────────────────────────────────
# Two manifest-only tables (no writer, no reader at v10) change PRIMARY KEY in the bump. DuckDB cannot ALTER a key, so
# the first call rebuilds a table still at its old key: copy the rows through a transform, check the count, swap.
# Idempotent: a table already at its MANIFEST key is skipped; a live key that is neither the old nor the new one
# raises instead of guessing.
_REBUILD_RUN_ID = "schema-11"


def _migration_session(now: float):
    from RevenueChaser.core import calendar
    try:
        return calendar.session_for(now).session_date
    except ValueError:      # outside the holiday table: the rows keep run_at, the session stays unknown
        return None


def _rekey_price_bars(row: dict, now: float) -> dict:
    return {**{k: v for k, v in row.items() if k != "date"}, "session_date": row.get("date"),
            "run_id": _REBUILD_RUN_ID, "run_at": row.get("fetched_at") or now}


def _rekey_lookup(row: dict, now: float) -> dict:
    from RevenueChaser.core import calendar
    fetched = row.get("fetched_at")
    try:
        session = calendar.to_session_date(fetched) if fetched else None
    except ValueError:
        session = None
    return {**row, "session_date": session or _migration_session(now), "source": "fda_orphan",
            "run_id": _REBUILD_RUN_ID, "run_at": fetched or now}


# table -> (its v10 PRIMARY KEY, row transform to the v11 shape)
_KEY_REBUILDS: dict[str, tuple[list[str], object]] = {
    "price_bars_daily": (["ticker", "date"], _rekey_price_bars),
    "orphan_lookups": (["ticker"], _rekey_lookup),
}


def _apply_key_rebuilds(conn) -> list[str]:
    """Rebuild each table of ``_KEY_REBUILDS`` still at its old key; returns ``"<table> (<n> rows)"`` for each."""
    done: list[str] = []
    live = _live_primary_keys(conn)
    present = {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' "
        "AND table_type = 'BASE TABLE'").fetchall()}
    now = time.time()
    for name, (old_key, transform) in _KEY_REBUILDS.items():
        if name not in present:
            continue                                    # CREATE IF NOT EXISTS makes it with the new key
        want = _declared_pk(MANIFEST[name])
        have = live.get(name, [])
        if have == want:
            continue                                    # rebuilt earlier: the second call is a no-op
        if have != old_key:
            raise RawStoreError(f"{name}: live key {have} is neither v10 {old_key} nor v11 {want}")
        cur = conn.execute(f"SELECT * FROM {name}")
        cols_in = [d[0] for d in cur.description]
        rows = [transform(_decode_row(name, dict(zip(cols_in, r))), now) for r in cur.fetchall()]
        cols = _columns_of(MANIFEST[name])
        tmp = f"{name}__rekey"
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(f"DROP TABLE IF EXISTS {tmp}")
            conn.execute(_ddl(name).replace(f"EXISTS {name} (", f"EXISTS {tmp} (", 1))
            prepared = _json_rows(name, rows)
            if prepared:
                conn.executemany(f"INSERT INTO {tmp} ({_col_sql(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                 [[r.get(c) for c in cols] for r in prepared])
            n_old = conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            n_new = conn.execute(f"SELECT count(*) FROM {tmp}").fetchone()[0]
            if n_old != n_new:
                raise RawStoreError(f"{name}: copied {n_new} of {n_old} rows")
            conn.execute(f"DROP TABLE {name}")
            conn.execute(f"ALTER TABLE {tmp} RENAME TO {name}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        _TABLE_READY.difference_update({k for k in _TABLE_READY if k[1] == name})
        logger.info("rawstore: rekeyed %s %s -> %s (%d rows)", name, old_key, want, n_old)
        done.append(f"{name} ({n_old} rows)")
    return done


def _canon_type(conn, declared: str) -> str:
    """The name DuckDB itself reports for a declared type (``TEXT`` -> ``VARCHAR``), cached per process."""
    t = _TYPE_CANON.get(declared)
    if t is None:
        t = _TYPE_CANON[declared] = conn.execute(f"SELECT typeof(CAST(NULL AS {declared}))").fetchone()[0]
    return t


def _note_drift(conn, name: str, existing: dict[str, str], skip: list[str],
                live_pks: dict[str, list[str]] | None) -> None:
    """Record what differs between the live ``name`` and its MANIFEST entry (best effort: never a failed write).

    ``skip`` are the columns a ``compress`` rebuild is about to retype, which is a migration, not drift.
    A non-empty ``skip`` also means that rebuild runs next, and it recreates the whole table.
    """
    rebuilt = bool(skip)
    try:
        declared = {col: _base_type(name, col) for col in _columns_of(MANIFEST[name])}
        for col, live_type in existing.items():
            if col not in declared:
                _record_drift(name, "extra_column", col, live_type, None, rebuilt)
            elif col not in skip:
                want = _canon_type(conn, declared[col])
                if want.upper() != live_type.upper():
                    _record_drift(name, "type", col, live_type, want, rebuilt)
        pks = live_pks if live_pks is not None else _live_primary_keys(conn, name)
        live_pk, want_pk = pks.get(name, []), _declared_pk(MANIFEST[name])
        if live_pk != want_pk:
            _record_drift(name, "key", None, live_pk, want_pk, rebuilt)
    except Exception:  # noqa: BLE001 - a drift check must never break a write
        logger.warning("rawstore: schema drift check of %s failed", name, exc_info=True)


def _rebuild_compressed(conn, name: str, columns: list[str]) -> None:
    """One-time rebuild of a table whose ``compress`` columns are still text/JSON.

    Reads every row, decodes it (also undoing the old double-JSON-encoding of
    ``edgar_financials``), and rewrites it into a fresh table with the BLOB
    columns — a table rebuild instead of ALTER COLUMN TYPE, because the
    compression happens in Python. Runs in one transaction.
    """
    cols = _columns_of(MANIFEST[name])
    cur = conn.execute(f"SELECT * FROM {name}")
    names = [d[0] for d in cur.description]
    rows = [_decode_row(name, dict(zip(names, r))) for r in cur.fetchall()]
    logger.info("rawstore: compressing %s(%s) — %d rows", name, ", ".join(columns), len(rows))
    tmp = f"{name}__rebuild"
    try:
        conn.execute("BEGIN TRANSACTION")
        own_tx = True
    except duckdb.TransactionException:  # called inside atomic()/transaction()
        own_tx = False
    try:
        conn.execute(f"DROP TABLE IF EXISTS {tmp}")
        conn.execute(_ddl(name).replace(f"EXISTS {name} (", f"EXISTS {tmp} (", 1))
        prepared = _json_rows(name, rows)
        if prepared:
            _bulk_insert(conn, tmp, name, cols, prepared, conflict_sql="")
        conn.execute(f"DROP TABLE {name}")
        conn.execute(f"ALTER TABLE {tmp} RENAME TO {name}")
        if own_tx:
            conn.execute("COMMIT")
    except BaseException:
        if own_tx:
            conn.execute("ROLLBACK")
        raise


def ensure_table(name: str) -> None:
    _check(name)
    _auto_ensure(write=True)
    with connect(db=_db_of(name)) as conn:
        _ensure_table(conn, name)


_EDGAR_TABLES = [n for n, s in MANIFEST.items() if s.get("db") == "edgar"]


def _table_exists(db: str, name: str) -> bool:
    if not _db_path(db).exists():
        return False
    try:
        with connect(read_only=True, db=db) as conn:
            row = conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='main' AND table_name = ?", [name]).fetchone()
            return row is not None
    except Exception:  # noqa: BLE001 - treat an unreadable DB as "no table"
        return False


def _migrate_edgar_tables() -> list[str]:
    """One-time move of EDGAR tables still living in ``raw.duckdb``.

    Idempotent: a table absent from raw is skipped, so the second run is a no-op.
    """
    moved = []
    for t in _EDGAR_TABLES:
        if not _table_exists("raw", t):
            continue
        # Decode first: query() returns JSON cells as JSON *text*, and handing
        # that text to write_rows() would JSON-encode it a second time (that
        # is how every edgar_financials row ended up a JSON string).
        rows = [_decode_row(t, r) for r in query(f"SELECT * FROM {t}", db="raw")]
        if rows:
            write_rows(t, rows)  # routes to edgar.duckdb
        with connect(db="raw") as conn:
            conn.execute(f'DROP TABLE IF EXISTS "{t}"')
        moved.append(t)
    if moved:
        try:
            write_rows("migration_manifest", [{
                "dataset": "edgar_tables_to_edgar_db",
                "migrated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "schema_version": _SCHEMA_VERSION, "source_count": len(moved),
                "source_bytes": 0, "source_hash": ",".join(sorted(moved)),
                "verify": "PASS",
            }])
        except Exception:  # noqa: BLE001 - the marker is best-effort
            pass
    return moved


def _record_rekeys(done: list[str]) -> None:
    """Best-effort marker per rebuilt table in ``migration_manifest`` (the first call has no store of its own yet)."""
    for item in done:
        name, _, rest = item.partition(" (")
        try:
            write_rows("migration_manifest", [{
                "dataset": f"key_rebuild_v11:{name}", "migrated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "schema_version": _SCHEMA_VERSION, "source_count": int(rest.split()[0]), "source_bytes": 0,
                "source_hash": "", "verify": "PASS"}])
        except Exception:  # noqa: BLE001 - the marker is best-effort
            logger.debug("rawstore: key-rebuild marker for %s not written", name, exc_info=True)


# #156 (avo, 2026-10-09): the daily price tables moved from raw.duckdb to prices.duckdb before their fill. A store
# from before keeps them (and the price watermarks in source_checks) in raw until the first call moves them:
# (raw table, the rows that move or None for all, prices table). The live store holds no price rows yet, so there
# the move is two DROPs of empty tables. D03 (#293, avo 2026-10-10) follows the same way: ``index_levels_daily`` has no
# writer, so on the live store its move is one more DROP of an empty table.
_PRICE_MOVES: tuple[tuple[str, str | None, str], ...] = (
    ("price_bars_daily", None, "price_bars_daily"),
    ("price_actions", None, "price_actions"),
    ("source_checks", "dataset IN ('price_bars_daily', 'price_actions')", "price_checks"),
    ("index_levels_daily", None, "index_levels_daily"),
)
_PRICE_MOVE_MARKER = "price_tables_to_prices_db"


def _fingerprint(conn, select: str) -> tuple[int, int]:
    """``(row count, sum of row hashes)`` of ``select``: the check ``scripts/backup_store`` uses for a snapshot."""
    n, h = conn.execute(f"SELECT count(*), coalesce(sum(hash(s)::HUGEINT), 0) FROM ({select}) s").fetchone()
    return int(n), int(h)


def _sql_text(path: Path) -> str:
    return path.as_posix().replace("'", "''")


def _copy_into_prices(src: str, dst: str, cols: str, parquet: Path, expect: tuple[int, int]) -> int:
    """Insert the parquet copy of ``src`` into ``dst`` of prices.duckdb, in ONE transaction that commits only when
    the copy matches what raw held (``expect``) and every copied row is in ``dst`` with the same values.

    Returns the rows copied, or -1 when ``dst`` already holds a different row for one of the keys (a first write
    wins on both sides, so neither row may replace the other: that needs a human)."""
    p = _sql_text(parquet)
    with connect(db="prices") as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            got = _fingerprint(conn, f"SELECT {cols} FROM read_parquet('{p}')")
            if got != expect:
                raise RawStoreError(f"{src}: the parquet copy does not match raw.duckdb ({got} != {expect})")
            conn.execute(f"INSERT INTO {dst} ({cols}) SELECT {cols} FROM read_parquet('{p}') ON CONFLICT DO NOTHING")
            missing = conn.execute(f"SELECT count(*) FROM (SELECT {cols} FROM read_parquet('{p}') "
                                   f"EXCEPT SELECT {cols} FROM {dst})").fetchone()[0]
            if missing:
                conn.execute("ROLLBACK")
                return -1
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return expect[0]


def _in_raw(conn, src: str) -> bool:
    return conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = 'main' AND table_name = ?",
                        [src]).fetchone() is not None


def _moved_elsewhere(src: str) -> None:
    logger.info("rawstore: %s was already moved out of raw.duckdb (by another process); nothing left to move", src)
    return None


def _mark_price_move(conn, src: str, n: int) -> None:
    """Add ``src:n`` to the ``migration_manifest`` marker, in the raw transaction that removes those rows, so the
    marker commits with the removal or not at all and names every table moved, whichever call or process moved it."""
    row = conn.execute("SELECT source_hash FROM migration_manifest WHERE dataset = ?", [_PRICE_MOVE_MARKER]).fetchone()
    moved: dict[str, int] = {}
    for part in (row[0] or "").split(",") if row else ():
        table, _, rows = part.rpartition(":")
        if table and rows.isdigit():
            moved[table] = int(rows)
    moved[src] = moved.get(src, 0) + n
    order = [m[0] for m in _PRICE_MOVES]
    tables = sorted(moved, key=lambda t: order.index(t) if t in order else len(order))
    rows = [{"dataset": _PRICE_MOVE_MARKER, "migrated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "schema_version": _SCHEMA_VERSION, "source_count": sum(moved.values()), "source_bytes": 0,
             "source_hash": ",".join(f"{t}:{moved[t]}" for t in tables), "verify": "PASS"}]
    _apply_write(conn, "migration_manifest", MANIFEST["migration_manifest"], _json_rows("migration_manifest", rows))
    _capture_changes(conn, "migration_manifest")


def _move_to_prices(src: str, where: str | None, dst: str, have: list[str], cols: str,
                    before: tuple[int, int]) -> int | None:
    """Move ``src`` (all rows, or those ``where`` selects) from raw.duckdb into ``dst`` of prices.duckdb.

    ``have`` are the raw table's columns, ``cols`` the SQL list of the ones ``dst`` declares, ``before`` the
    fingerprint of the rows that move when the caller probed raw. Copy first (the copy and its fingerprint read one
    snapshot of raw), verify, and only then DROP the raw table (or DELETE the moved rows) in a raw transaction that
    re-checks the rows are the ones copied and adds them to the marker. A stop in between leaves the rows in both
    files; the next call finds them identical in prices and finishes. Another process running the same move at the
    same time (the dashboard and a job started together) is no alarm: a table or rows already gone from raw were
    moved by it (logged at INFO). Returns the rows moved (0 for an empty table dropped), or None when there was
    nothing to move or the move was refused (logged; the rows stay in raw).
    """
    select = f'SELECT {cols} FROM "{src}"' + (f" WHERE {where}" if where else "")
    if before[0] == 0:
        if where is not None:
            return None
        with connect(db="raw") as conn:
            _ensure_table(conn, "migration_manifest")
            with _tx(conn):
                if not _in_raw(conn, src):
                    return _moved_elsewhere(src)
                if conn.execute(f'SELECT count(*) FROM "{src}"').fetchone()[0]:
                    return None                       # a row landed meanwhile: the next call copies it
                conn.execute(f'DROP TABLE "{src}"')
                _mark_price_move(conn, src, 0)
        logger.info("rawstore: dropped the empty %s from raw.duckdb (it lives in prices.duckdb)", src)
        return 0
    want = _columns_of(MANIFEST[dst])
    extra = [c for c in have if c not in want]
    if extra:
        logger.warning("rawstore: %s in raw.duckdb has %d rows and column(s) %s that %s in prices.duckdb lacks; "
                       "not moved (move it by hand)", src, before[0], extra, dst)
        return None
    # ignore_cleanup_errors: on Windows a parquet handle can outlive the connection a beat; a leftover temp file
    # must not undo a verified copy (the drop below still needs to run).
    with tempfile.TemporaryDirectory(prefix="rc-prices-move-", ignore_cleanup_errors=True) as tmp:
        parquet = Path(tmp) / f"{src}.parquet"
        with connect(db="raw") as conn:
            with _tx(conn):
                now = _fingerprint(conn, select) if _in_raw(conn, src) else (0, 0)
                if now[0]:
                    conn.execute(f"COPY ({select}) TO '{_sql_text(parquet)}' (FORMAT parquet)")
        if not now[0]:
            return _moved_elsewhere(src)
        n = _copy_into_prices(src, dst, cols, parquet, now)
    if n < 0:
        logger.warning("rawstore: %s in prices.duckdb already holds a different row for a key of the %d rows of "
                       "%s in raw.duckdb; not moved, nothing overwritten (compare and move them by hand)",
                       dst, now[0], src)
        return None
    with connect(db="raw") as conn:
        _ensure_table(conn, "migration_manifest")
        with _tx(conn):
            after = _fingerprint(conn, select) if _in_raw(conn, src) else (0, 0)
            if after != now:
                if not after[0]:
                    return _moved_elsewhere(src)
                logger.warning("rawstore: %s in raw.duckdb changed during the move to prices.duckdb; kept, the "
                               "next call moves the rest", src)
                return None
            conn.execute(f'DROP TABLE "{src}"' if where is None else f'DELETE FROM "{src}" WHERE {where}')
            _mark_price_move(conn, src, n)
    logger.info("rawstore: moved %d rows of %s from raw.duckdb to %s in prices.duckdb", n, src, dst)
    return n


def _migrate_price_tables() -> list[str]:
    """The one-time move of ``_PRICE_MOVES``; idempotent (a second call finds nothing in raw). Returns
    ``"<raw table>:<rows>"`` per table this call moved; each move adds itself to the ``migration_manifest`` marker
    ``price_tables_to_prices_db`` in its own raw transaction."""
    moves = [m for m in _PRICE_MOVES if _db_of(m[2]) == "prices"]
    if not moves:
        return []
    found = []                                        # one raw open per call once the move is done
    with connect(db="raw") as conn:
        cols_of: dict[str, list[str]] = {}
        for t, c in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'main' "
                f"AND table_name IN ({', '.join('?' * len(moves))}) ORDER BY table_name, ordinal_position",
                [m[0] for m in moves]).fetchall():
            cols_of.setdefault(t, []).append(c)
        for src, where, dst in moves:
            have = cols_of.get(src)
            if not have:
                continue
            cols = _col_sql([c for c in _columns_of(MANIFEST[dst]) if c in have])
            select = f'SELECT {cols} FROM "{src}"' + (f" WHERE {where}" if where else "")
            found.append((src, where, dst, have, cols, _fingerprint(conn, select)))
    done = []
    for src, where, dst, have, cols, before in found:
        n = _move_to_prices(src, where, dst, have, cols, before)
        if n is not None:
            done.append(f"{src}:{n}")
    return done


def _ensure_db_tables(conn, db: str) -> None:
    """Reconcile every MANIFEST table of one file, with ONE catalog probe."""
    existing: dict[str, dict[str, str]] = {}
    for t, c, typ in conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main'").fetchall():
        existing.setdefault(t, {})[c] = typ
    path = str(_db_path(db))
    live_pks = _live_primary_keys(conn)
    for name in MANIFEST:
        if _db_of(name) != db:
            continue
        _reconcile_table(conn, name, existing.get(name, {}), live_pks)
        _TABLE_READY.add((path, name))


def ensure_all_tables() -> None:
    # Raw store: renames/merges/views are raw-table migrations, so they run only
    # on the raw file. Each DB gets its own tables created.
    prev = getattr(_LOCAL, "ensuring", False)
    prev_seen = getattr(_LOCAL, "drift_seen", None)
    _LOCAL.ensuring = True  # the steps below call the public API themselves
    _LOCAL.drift_seen = seen = set()   # the drift this apply sees, for _persist_drift (#312)
    try:
        with connect(db="raw") as conn:
            _apply_renames(conn)
            rekeyed = _apply_key_rebuilds(conn)
            _ensure_db_tables(conn, "raw")
            _apply_merges(conn)
            _apply_views(conn)
            _synced, sync_failed = _sync_tracked(conn, "store-sync")
        with connect(db="edgar") as conn:
            _ensure_db_tables(conn, "edgar")
        _migrate_edgar_tables()
        _record_rekeys(rekeyed)
        with connect(db="discord") as conn:   # tables only; no raw-file migrations apply
            _ensure_db_tables(conn, "discord")
        with connect(db="prices") as conn:    # #156: made eagerly, so Data Health counts 0 rows, not "no table"
            _ensure_db_tables(conn, "prices")
        _migrate_price_tables()
        if _db_path("history").exists():   # result history (#287); the first record_run makes the file
            ensure_history()
        _persist_drift(seen)                 # every file is reconciled now; never raises
        if sync_failed:                      # after the other files, so one table never holds them back
            raise RawStoreError(f"change capture could not sync {sync_failed}")
    finally:
        _LOCAL.ensuring = prev
        _LOCAL.drift_seen = prev_seen
    _ENSURED[str(Path(DB_PATH))] = 0.0


# ── apply the manifest once per process, on first use ───────────────────────
# Nothing used to call ensure_all_tables() outside tests and the migration
# script, so a fresh or restored store never got its renames, merges or VIEWs
# (``company_profile``/``index_tickers``) and those readers silently returned
# []. Now the first store call of every process (dashboard, CLI, scheduled job)
# applies it. A failure (e.g. the store is locked) is logged and retried at most
# every _ENSURE_RETRY_S seconds rather than on every call. The unit suite turns
# it off (RC_STORE_AUTO_ENSURE=0 in conftest.py) because every test gets a fresh
# DB and the full manifest costs ~1 s; tests that need it call it explicitly.
_ENSURED: dict[str, float] = {}  # raw DB path -> 0.0 done | time of last failure
_ENSURE_LOCK = threading.Lock()
_ENSURE_RETRY_S = 30.0


def _auto_ensure(write: bool) -> None:
    if os.getenv("RC_STORE_AUTO_ENSURE", "1") == "0" or getattr(_LOCAL, "ensuring", False):
        return
    key = str(Path(DB_PATH))
    state = _ENSURED.get(key)
    if state == 0.0 or (state and time.time() - state < _ENSURE_RETRY_S):
        return
    if not write and not Path(DB_PATH).exists():
        return  # a read of a store that does not exist yet must not create it
    with _ENSURE_LOCK:
        state = _ENSURED.get(key)
        if state == 0.0 or (state and time.time() - state < _ENSURE_RETRY_S):
            return
        try:
            ensure_all_tables()
        except Exception:  # noqa: BLE001 - never turn a read into a crash
            logger.warning("rawstore: applying the schema manifest failed; "
                           "retrying in %.0fs", _ENSURE_RETRY_S, exc_info=True)
            _ENSURED[key] = time.time()


# Old table name -> new. Applied idempotently on `ensure_all_tables()`, BEFORE any
# CREATE IF NOT EXISTS, so a rename is never shadowed by an empty new table.
# See docs/glossary.md + docs/classification_design.md (Phase 1).
_RENAMES: dict[str, str] = {
    "asymmetric_universe_meta": "equity_universe_provenance",
    "asymmetric_universe": "equity_universe",
    "ticker_store": "ticker_scores",
    "ticker_cache": "ticker_payload_cache",
    "sec_facts": "edgar_financials",
    "index_members": "index_constituents",
    "etf_holdings_cache": "etf_holdings",
}


def _apply_renames(conn) -> list[str]:
    """Rename tables per ``_RENAMES`` when the old name exists.

    Idempotent: a second run finds nothing to do. Returns what it renamed. If
    the new name ALSO exists but is empty (a write self-healed it into
    existence before the rename ran), the empty shell is dropped so the rename
    still happens instead of orphaning the old data. If both hold rows the
    rename is skipped and logged — that needs a human.
    """
    existing = {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main'").fetchall()}
    applied = []
    for old, new in _RENAMES.items():
        if old not in existing:
            continue
        if new in existing:
            n = conn.execute(f'SELECT count(*) FROM "{new}"').fetchone()[0]
            if n:
                logger.warning("rawstore: both %s and %s hold rows; not renaming "
                               "(merge %s by hand)", old, new, old)
                continue
            conn.execute(f'DROP TABLE "{new}"')
            _TABLE_READY.discard((str(Path(DB_PATH)), new))
        conn.execute(f'ALTER TABLE "{old}" RENAME TO "{new}"')
        applied.append(f"{old}->{new}")
    return applied


# 2:1 table migrations that ``_RENAMES`` cannot express. Idempotent: a source
# that no longer exists is skipped. Sources are listed richest-first and the
# INSERT uses DO NOTHING, so the richer table wins an overlapping key.
_MERGES: list[dict] = [
    {
        "new": "sec13f_security_meta",
        "sources": ["sec13f_universe_meta", "universe_meta"],
        "cols": ["ticker", "entry"],
        "conflict": ["ticker"],
    },
]


def _apply_merges(conn) -> list[str]:
    """Fold legacy tables into their merged home, then drop the source."""
    existing = {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main'").fetchall()}
    done = []
    for m in _MERGES:
        if m["new"] not in existing:
            continue
        cols = _col_sql(m["cols"])
        conflict = _col_sql(m["conflict"])
        for src in m["sources"]:
            if src not in existing:
                continue
            conn.execute(
                f'INSERT INTO "{m["new"]}" ({cols}) SELECT {cols} FROM "{src}" '
                f'ON CONFLICT ({conflict}) DO NOTHING')
            conn.execute(f'DROP TABLE "{src}"')
            done.append(f"{src}->{m['new']}")
    return done


# Derived, read-only VIEWs (never materialised). ``index_tickers`` replaces the
# old ``ticker_registry`` table: membership lives in ``index_constituents`` and
# name/sector are joined from ``equity_universe``.
_VIEWS: list[tuple[str, str]] = [
    ("index_tickers",
     "CREATE OR REPLACE VIEW index_tickers AS "
     "SELECT c.slug, c.ticker, u.name, u.sector "
     "FROM index_constituents c "
     "LEFT JOIN equity_universe u ON u.ticker = c.ticker"),
    ("company_profile",
     "CREATE OR REPLACE VIEW company_profile AS "
     "SELECT u.ticker, u.name, u.sector, u.industry, u.industry_group, "
     "u.sub_industry, u.market_cap, u.price, "
     "a.total_points, a.tech_breakthrough, a.dilution_warning, a.earnings_date, "
     "f.ro40, f.rev_growth_ttm, f.fcf_margin, f.gross_margin, f.roic, f.pe, f.pb, "
     "f.multiplier AS fundamentals_multiplier, "
     "s.quality AS swing_quality, s.rs_rating, s.ma_alignment, "
     "s.multiplier AS swing_multiplier "
     "FROM equity_universe u "
     "LEFT JOIN ticker_scores a ON a.ticker = u.ticker "
     "LEFT JOIN fundamentals_scores f ON f.ticker = u.ticker "
     "LEFT JOIN swing_scores s ON s.ticker = u.ticker"),
]


def _apply_views(conn) -> list[str]:
    made = []
    for name, sql in _VIEWS:
        conn.execute(sql)
        made.append(name)
    return made


def _col_type(name: str, col: str) -> str:
    """The base DuckDB type declared for ``col`` (uppercased), or "TEXT"."""
    spec = MANIFEST[name]
    for c in spec["columns"]:
        head, _, rest = c.strip().partition(" ")
        if head.strip('"') == col:
            rest = rest.strip() or "TEXT"
            return rest.upper()
    return "TEXT"


def _json_rows(name: str, rows: list[dict]) -> list[dict]:
    """Prepare dict/list-valued cells for storage.

    A cell targeting a native DuckDB nested type (``JSON``/``STRUCT``) is
    serialised to a JSON *string* first (DuckDB validates it on the way in and
    stores it as a proper JSON value — reading a Python dict/list straight into
    a JSON column mangles floats inside mixed-type arrays, e.g.
    ``[["2026-08-01", 100.0]]`` -> ``[["2026-08-01", "100.0"]]``). A scalar
    string is JSON-quoted so it stays a JSON string. TEXT-blob columns (legacy)
    keep the old behaviour.
    """
    spec = MANIFEST[name]
    compress = spec.get("compress") or {}
    out = []
    for r in rows:
        row = dict(r)
        for col in _columns_of(spec):
            v = row.get(col)
            if v is None:
                continue
            if col in compress:
                row[col] = _compress(v, compress[col])
                continue
            base = _col_type(name, col).split("(", 1)[0].rstrip("[]")
            if base in _NESTED_TYPES:
                row[col] = json.dumps(v, ensure_ascii=False, default=str)
            elif isinstance(v, (dict, list)):
                row[col] = json.dumps(v, ensure_ascii=False, default=str)
        out.append(row)
    return out


def _compress(v, kind: str) -> bytes:
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v)  # already compressed (e.g. copied row to row)
    text = json.dumps(v, ensure_ascii=False, default=str) if kind == "json" else str(v)
    return zlib.compress(text.encode("utf-8"), 6)


def _decompress(v, kind: str):
    """Inverse of ``_compress``; also accepts the pre-compression text form.

    A cell read from a table not yet rebuilt is still JSON text — possibly
    JSON-encoded twice (the old edgar_financials migration bug) — so a JSON
    string that itself holds a JSON array/object is decoded again.
    """
    if isinstance(v, (bytes, bytearray, memoryview)):
        v = zlib.decompress(bytes(v)).decode("utf-8")
    if kind != "json" or not isinstance(v, str):
        return v
    try:
        out = json.loads(v)
        while isinstance(out, str) and out[:1] in ("[", "{"):
            out = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return v
    return out


def _base_type(name: str, col: str) -> str:
    """``_col_type`` without constraints: ``"TEXT PRIMARY KEY"`` -> ``"TEXT"``."""
    t = _col_type(name, col)
    for stop in (" PRIMARY", " NOT ", " DEFAULT", " UNIQUE", " CHECK", " REFERENCES"):
        i = t.find(stop)
        if i >= 0:
            t = t[:i]
    return t.strip() or "TEXT"


def _col_sql(cols: list[str]) -> str:
    # quote every identifier — column names come from the manifest unquoted and
    # some (e.g. rotation_snapshots."group") are SQL reserved words
    return ", ".join(f'"{c}"' for c in cols)




# ── change capture (schema 11, history redesign C) ──────────────────────────────────────────────────────────────
# Who wrote a change: ``(run_id, source)`` of the running dashboard job when the app registered a provider
# (app/jobs: the job id and its module), else the scheduled task's run (``task_run``: its ``job_runs`` id and
# registry name), else a fresh id like ``ledger.new_run_id`` and no source. A forced label wins (the first-call sync).
_CHANGE_WRITER = None
_CHANGE_CLOCK_LOCK = threading.Lock()
_LAST_CHANGE_AT = [0.0]


# A scheduled task's run (#129 A1): ``core/automations.run_task`` records the run's ``job_runs`` row and hands its id
# and registry name to the command it starts in these variables.
RUN_ID_ENV, RUN_SOURCE_ENV = "RC_RUN_ID", "RC_RUN_SOURCE"


def task_run() -> tuple[str, str | None] | None:
    """``(run_id, source)`` of the scheduled task this process runs under, else None."""
    run_id = os.environ.get(RUN_ID_ENV, "").strip()
    return (run_id, os.environ.get(RUN_SOURCE_ENV, "").strip() or None) if run_id else None


def set_change_writer(fn) -> None:
    """Called once by app/jobs: ``fn()`` returns ``(run_id, source)`` of the job on this thread, or None."""
    global _CHANGE_WRITER
    _CHANGE_WRITER = fn


def _change_writer() -> tuple[str, str | None]:
    forced = getattr(_LOCAL, "change_writer", None)
    if forced:
        return forced
    got = _CHANGE_WRITER() if _CHANGE_WRITER else None
    if got and got[0]:
        return got[0], got[1]
    task = task_run()
    if task:
        return task
    import uuid
    return uuid.uuid4().hex[:12], None


def writer_run_id() -> str:
    """The ``run_id`` a core writer stamps on its rows: the running dashboard job's id, else the scheduled task's,
    else a fresh one.

    The same id the change capture records, so a row joins to its ``job_runs`` row. For core code that cannot import
    ``app.jobs.run_id`` (``core/quotes.py``, called from inside the universe pull).
    """
    return _change_writer()[0]


def _change_clock() -> float:
    """Epoch seconds, strictly increasing within the process, so (key, run_at) never collides."""
    with _CHANGE_CLOCK_LOCK:
        now = max(time.time(), _LAST_CHANGE_AT[0] + 1e-6)
        _LAST_CHANGE_AT[0] = now
        return now


def _session_of(run_at: float):
    from RevenueChaser.core import calendar
    try:
        return calendar.session_for(run_at).session_date
    except ValueError:      # outside the holiday table (2025-2028): the row keeps run_at, valid_from is unknown
        return None


@contextmanager
def change_label(label: str, run_id: str | None = None):
    """Stamp every change captured in the block with ``label`` as its source and ``run_id`` (default: ``label``)."""
    prev = getattr(_LOCAL, "change_writer", None)
    _LOCAL.change_writer = (run_id or label, label)
    try:
        yield
    finally:
        _LOCAL.change_writer = prev


def _q(c: str) -> str:
    return f'"{c}"'


def _live_columns(conn, catalog: str, name: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_catalog = ? AND table_schema = 'main' "
        "AND table_name = ?", [catalog, name]).fetchall()}


def _capture_changes(conn, name: str, touched: list[str] | None = None) -> int:
    """Append the rule-A rows that make ``<name>_changes`` agree with the live table ``name``; returns the count.

    Runs on the caller's connection, inside its transaction, right after the write. It compares the whole live
    table with the latest change row of each key, so it does not depend on which write ran: a key that is new, back
    after 'absent', or has a tracked value that differs gets a 'present' row; a key whose latest row is 'present'
    and that is no longer in the live table gets an 'absent' row. A second call with nothing changed appends 0.
    ``touched``: the columns an ``update_columns`` set; none tracked means nothing can have moved.
    """
    cfg = CHANGES.get(name)
    if cfg is None or name not in MANIFEST or f"{name}_changes" not in MANIFEST:
        return 0
    spec = MANIFEST[name]
    key, track, carry = spec["key"], cfg["track"], cfg.get("carry", [])
    if touched is not None and not set(touched) & set(key + track):
        return 0        # an update of untracked columns only (the cap backfill): no key and no tracked value moved
    ch, ren = f"{name}_changes", cfg.get("rename", {})
    live = key + track + carry
    catalog = conn.execute("SELECT current_database()").fetchone()[0]
    if not set(live) <= _live_columns(conn, catalog, name):
        # No live table, or one that predates a MANIFEST column (a legacy store before its first reconcile): nothing
        # to compare yet. The first-call sync runs after the MANIFEST is applied and records it.
        return 0
    _ensure_table(conn, ch)
    own = lambda t: f"{_q(catalog)}.main.{_q(t)}"   # this file's own table: a TEMP table of the name never stands in
    src_live = own(name)
    if not _has_primary_key(spec):
        # A key-less live table (index_constituents) may hold a repeated key or a NULL key part, which the change
        # table's key cannot: one row per key, NULL keys left out. Today's writes stay accepted.
        order = f" ORDER BY {', '.join(_q(c) for c in track)}" if track else ""
        src_live = (f"(SELECT * FROM {own(name)} WHERE {' AND '.join(f'{_q(c)} IS NOT NULL' for c in key)} "
                    f"QUALIFY row_number() OVER (PARTITION BY {', '.join(_q(c) for c in key)}{order}) = 1)")
    run_at = _change_clock()
    run_id, writer = _change_writer()
    own_source = "source" not in key            # a table keyed on its provider has no second source column
    fcol = cfg.get("fetched_at")
    fetched = ("NULL" if not fcol else f"l.{_q(fcol)}" if _spec_base_type(spec, fcol) == "DOUBLE"
               else f"epoch(TRY_CAST(l.{_q(fcol)} AS TIMESTAMPTZ))")
    into = (f"INSERT INTO {own(ch)} ({', '.join(_q(ren.get(c, c)) for c in live)}, valid_from, status, "
            f"{'source, ' if own_source else ''}fetched_at, run_id, run_at) ")
    params = [_session_of(run_at), *([cfg.get("source", writer)] if own_source else []), run_id, run_at]
    src = "?, " if own_source else ""
    cur = (f"SELECT * FROM {own(ch)} QUALIFY row_number() OVER "
           f"(PARTITION BY {', '.join(_q(ren.get(c, c)) for c in key)} ORDER BY run_at DESC) = 1")
    on = " AND ".join(f"l.{_q(c)} = cur.{_q(ren.get(c, c))}" for c in key)
    differs = " OR ".join(f"l.{_q(c)} IS DISTINCT FROM cur.{_q(ren.get(c, c))}" for c in track) or "FALSE"
    present = conn.execute(
        into + f"SELECT {', '.join('l.' + _q(c) for c in live)}, ?, 'present', {src}{fetched}, ?, ? FROM {src_live} l "
        f"LEFT JOIN ({cur}) cur ON {on} WHERE cur.run_at IS NULL OR cur.status = 'absent' OR ({differs})",
        params).fetchone()[0]
    absent = conn.execute(
        into + f"SELECT {', '.join('cur.' + _q(ren.get(c, c)) for c in live)}, ?, 'absent', {src}NULL, ?, ? "
        f"FROM ({cur}) cur WHERE cur.status = 'present' AND NOT EXISTS (SELECT 1 FROM {src_live} l WHERE {on})",
        params).fetchone()[0]
    return int(present) + int(absent)


def changes_as_of(name: str, at: float) -> list[dict]:
    """The rows ``name`` held at epoch ``at``, from its change table: each key's latest change at or before ``at``,
    when that change is 'present'. Key, tracked and carried columns only (live names)."""
    cfg = CHANGES[name]
    key, ren = MANIFEST[name]["key"], cfg.get("rename", {})
    cols = key + cfg["track"] + cfg.get("carry", [])
    sel = ", ".join(f"{_q(ren.get(c, c))} AS {_q(c)}" for c in cols)
    part = ", ".join(_q(ren.get(c, c)) for c in key)
    return query(f"SELECT {sel} FROM {name}_changes WHERE run_at <= ? QUALIFY row_number() OVER "
                 f"(PARTITION BY {part} ORDER BY run_at DESC) = 1 AND status = 'present' ORDER BY ALL", [at])


def _tracked_in(sql: str) -> list[str]:
    """Tracked live tables a raw SQL text names (a cheap over-approximation: a capture with no change appends 0)."""
    low = sql.casefold()
    return [n for n in CHANGES if n.casefold() in low]


# Statement kinds refused in raw SQL that names a tracked table: a COMMIT inside the text ends the capture's transaction
# before the capture runs, a PREPARE'd write runs later as an EXECUTE that names no table, and ATTACH reaches the raw
# file from another handle (the refutation pass, report §5.1).
_CAPTURE_REFUSED = {"TRANSACTION", "PREPARE", "EXECUTE", "ATTACH", "DETACH"}


def _tracked_writes(conn, sql: str, db: str) -> list[str]:
    """Tracked live tables a raw SQL text may change; each is captured after it runs, in its transaction.

    [] when the text names none or only reads. Raises for text naming one that the capture could not follow: one of
    ``_CAPTURE_REFUSED``, or a write sent to another file (an ATTACH there reaches raw.duckdb)."""
    named = _tracked_in(sql)
    if not named:
        return []
    kinds = {s.type.name for s in conn.extract_statements(sql)}
    if kinds <= {"SELECT"}:
        return []
    bad = sorted(kinds & _CAPTURE_REFUSED) or ([] if db == "raw" else [f"a write sent to the {db} file"])
    if bad:
        raise RawStoreError(
            f"raw SQL names {named}, whose every change the store records in the write's own transaction, with "
            f"{', '.join(bad)}, which that record cannot follow. Use the write API, or send each statement as its "
            f"own call on the raw file.")
    return named


class _Fetched:
    """A statement's result, read before the change capture reused its connection (``_Tx.execute``)."""

    def __init__(self, cur):
        self.description = cur.description
        self._rows = list(cur.fetchall()) if cur.description else []

    def fetchall(self) -> list:
        rows, self._rows = self._rows, []
        return rows

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


def _sync_tracked(conn, label: str) -> tuple[dict[str, int], dict[str, str]]:
    """Capture every tracked table, one transaction each, so a table that cannot sync leaves the others synced."""
    done, failed = {}, {}
    with change_label(label):
        for name in CHANGES:
            if name not in MANIFEST:
                continue
            try:
                with _tx(conn):
                    done[name] = _capture_changes(conn, name)
            except Exception as exc:  # noqa: BLE001 - the caller raises once the other tables and files are done
                failed[name] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
                logger.warning("rawstore: change capture could not sync %s", name, exc_info=True)
    return done, failed


def sync_changes(label: str = "store-sync") -> dict[str, int]:
    """Capture every tracked table once (the first call of each process, and on demand): any write that bypassed
    the API (a caller's own ``connect()``, the legacy migration) is recorded here, stamped ``label``."""
    with connect(db="raw") as conn:
        for name in CHANGES:
            if name in MANIFEST:
                _ensure_table(conn, name)
        done, failed = _sync_tracked(conn, label)
    if failed:
        raise RawStoreError(f"change capture could not sync {failed}")
    return done


# Result-history read views (history.duckdb). ``result_sessions``: per board and session, the one run whose rows are
# the session snapshot (``session_snapshot``, set by ``core/history.record_run``: the first complete run recorded;
# rule B). One ``<table>_by_session`` view per session board joins its rows to that run's session date;
# ``sec13f_metrics_by_quarter`` takes each quarter's last error-free run.
def _history_views() -> list[tuple[str, str]]:
    views = [("result_sessions",
              "CREATE OR REPLACE VIEW result_sessions AS "
              "SELECT board, session_date, run_id, run_at FROM result_runs WHERE session_snapshot")]
    for name, spec in MANIFEST.items():
        if spec.get("db") != "history" or "board" not in spec:
            continue
        stem = name[:-len("_history")] if name.endswith("_history") else name
        if spec["snapshot"] == "session":
            views.append((f"{stem}_by_session",
                          f"CREATE OR REPLACE VIEW {stem}_by_session AS SELECT s.session_date, h.* "
                          f"FROM {name} h JOIN result_sessions s "
                          f"ON s.board = '{spec['board']}' AND s.run_id = h.run_id"))
        elif spec["snapshot"] == "quarter":
            views.append((f"{stem}_by_quarter",
                          f"CREATE OR REPLACE VIEW {stem}_by_quarter AS SELECT h.* FROM {name} h "
                          f"JOIN result_runs r ON r.board = '{spec['board']}' AND r.run_id = h.run_id "
                          f"WHERE r.error IS NULL QUALIFY r.run_at = max(r.run_at) OVER (PARTITION BY h.quarter)"))
    return views


def _apply_history_views(conn) -> list[str]:
    made = []
    for name, sql in _history_views():
        conn.execute(sql)
        made.append(name)
    return made


_HISTORY_READY: set[str] = set()


def ensure_history() -> None:
    """Create the history file's tables and read views, once per process and file. ``core/history.record_run`` calls
    it before its first write; ``ensure_all_tables`` only when the file exists, so a store with no recorded run has
    no history file (J3: backup and restore treat a missing file as "no history")."""
    path = _db_path("history")
    if str(path) in _HISTORY_READY and path.exists():
        return
    with connect(db="history") as conn:
        _ensure_db_tables(conn, "history")
        _apply_history_views(conn)
    _HISTORY_READY.add(str(path))


def _apply_write(conn, name: str, spec: dict, rows: list[dict]) -> None:
    """Mode-specific INSERT for a prepared (JSON-serialized) batch.

    Caller supplies the connection and the surrounding transaction, so a
    multi-statement mode ("replace" = DELETE + INSERT) is all-or-nothing.
    """
    cols = _columns_of(spec)
    key_cols = spec.get("key") or []
    mode = spec.get("write", "upsert")

    if spec.get("history"):
        # A history table is only ever added to (#286): the first row written for a key stays, whatever
        # the table's write mode would otherwise do with a duplicate.
        if not key_cols:
            raise RawStoreError(f"history table {name} needs a key: first-write-wins is decided per key")
        conflict_sql = f"ON CONFLICT ({_col_sql(key_cols)}) DO NOTHING"
        rows = _dedupe(rows, key_cols, keep="first")
    elif mode == "upsert" and key_cols:
        conflict = _col_sql(key_cols)
        update_cols = [c for c in cols if c not in key_cols]
        if update_cols:
            updates = ", ".join(f'"{c}"=excluded."{c}"' for c in update_cols)
            conflict_sql = f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
        else:
            conflict_sql = f"ON CONFLICT ({conflict}) DO NOTHING"
        rows = _dedupe(rows, key_cols, keep="last")  # row-by-row: last one wins
    elif mode == "append":
        conflict_sql = f"ON CONFLICT ({_col_sql(key_cols)}) DO NOTHING" if key_cols else ""
        if key_cols:
            rows = _dedupe(rows, key_cols, keep="first")  # DO NOTHING: first wins
    elif mode == "replace" and key_cols:
        # Delete the rows matching the batch's groups (group_by defaults to the
        # key), then insert — a per-group refresh (one slug/ticker/date/run)
        # doesn't clear other groups. One DELETE for the whole batch.
        group_cols = spec.get("group_by") or key_cols
        distinct = list({tuple(r.get(c) for c in group_cols) for r in rows})
        _delete_groups(conn, name, group_cols, distinct)
        conflict_sql = ""
    else:
        raise RawStoreError(f"table {name}: unsupported write mode {mode!r}")

    if len(rows) >= _BULK_MIN_ROWS:
        _bulk_insert(conn, name, name, cols, rows, conflict_sql)
    else:
        values = ", ".join(["?"] * len(cols))
        conn.executemany(
            f"INSERT INTO {name} ({_col_sql(cols)}) VALUES ({values}) {conflict_sql}",
            [[r.get(c) for c in cols] for r in rows])


# ``executemany`` binds and executes one row at a time: 50k rows take ~15 s.
# Registering the batch as a DataFrame and running ONE ``INSERT ... SELECT``
# takes ~0.2 s. Small batches keep ``executemany`` (no pandas overhead).
_BULK_MIN_ROWS = 64
_NUMERIC_TYPES = {"INTEGER", "BIGINT", "SMALLINT", "TINYINT", "HUGEINT", "UBIGINT",
                  "UINTEGER", "DOUBLE", "FLOAT", "REAL"}


def _dedupe(rows: list[dict], key_cols: list[str], keep: str) -> list[dict]:
    """One row per key, as a row-by-row write would have left it.

    A single ``INSERT ... ON CONFLICT DO UPDATE`` refuses to touch one row twice,
    so a batch carrying a key twice must be collapsed first.
    """
    seen: dict[tuple, dict] = {}
    for r in rows:
        k = tuple(r.get(c) for c in key_cols)
        if keep == "last" or k not in seen:
            seen[k] = r
    return rows if len(seen) == len(rows) else list(seen.values())


def _delete_groups(conn, name: str, group_cols: list[str], groups: list[tuple]) -> None:
    if not groups:
        return
    if len(groups) < _BULK_MIN_ROWS:
        where = " AND ".join(f'"{c}" = ?' for c in group_cols)
        for kv in groups:
            conn.execute(f"DELETE FROM {name} WHERE {where}", list(kv))
        return
    import pandas as pd
    view = "_rc_groups"
    conn.register(view, pd.DataFrame([[None if v is None else str(v) for v in g] for g in groups],
                                     columns=group_cols, dtype=object))
    try:
        match = " AND ".join(f'CAST({name}."{c}" AS VARCHAR) = {view}."{c}"' for c in group_cols)
        conn.execute(f"DELETE FROM {name} WHERE EXISTS (SELECT 1 FROM {view} WHERE {match})")
    finally:
        conn.unregister(view)


def _cell_text(v, base: str):
    """A cell as the VARCHAR the bulk INSERT casts back to ``base``.

    Every value goes in as text and is CAST to the column type in SQL, which
    reproduces exactly what binding the Python value did (float repr is
    round-trip exact; ``CAST('3.7' AS INTEGER)`` rounds like a bound 3.7).
    Bytes stay bytes (BLOB). Booleans follow DuckDB's own rendering.
    """
    if v is None or isinstance(v, (bytes, bytearray, memoryview)):
        return None if v is None else bytes(v)
    if isinstance(v, bool):
        return ("1" if v else "0") if base in _NUMERIC_TYPES else ("true" if v else "false")
    return str(v)


def _bulk_insert(conn, target: str, spec_name: str, cols: list[str], rows: list[dict],
                 conflict_sql: str) -> None:
    import pandas as pd
    bases = [_base_type(spec_name, c) for c in cols]
    frame = pd.DataFrame([[_cell_text(r.get(c), b) for c, b in zip(cols, bases)] for r in rows],
                         columns=cols, dtype=object)
    view = "_rc_batch"
    # Analyse every row, not a sample: a column whose first 1000 cells are NULL
    # must still be typed from the cells that are not.
    conn.execute(f"SET pandas_analyze_sample = {max(len(rows), 1000)}")
    conn.register(view, frame)
    try:
        select = ", ".join(f'CAST({view}."{c}" AS {b})' for c, b in zip(cols, bases))
        conn.execute(f"INSERT INTO {target} ({_col_sql(cols)}) "
                     f"SELECT {select} FROM {view} {conflict_sql}")
    finally:
        conn.unregister(view)


def write_rows(name: str, rows: list[dict]) -> int:
    """Insert/upsert rows into a manifest table. Raises on failure.

    Atomic: the whole batch commits or none of it does, so a partial/failed
    refresh can never leave the table emptied by the DELETE half of a
    "replace".
    """
    spec = _check(name)
    if not rows:
        return 0
    _auto_ensure(write=True)
    rows = _json_rows(name, rows)

    def _op():
        with connect(db=_db_of(name)) as conn:
            _ensure_table(conn, name)  # self-healing: create the table if the DB predates it
            with _tx(conn):
                _apply_write(conn, name, spec, rows)
                _capture_changes(conn, name)

    _retry_write(_op)
    return len(rows)


def update_columns(name: str, key_col: str, rows: list[dict],
                   columns: list[str]) -> int:
    """UPDATE only ``columns`` on rows matched by ``key_col`` — never insert.

    The narrow writer for a partial-row refresh: ``equity_universe`` is
    ``write: "upsert"``, so sending ``{ticker, market_cap, price}`` through
    ``write_rows`` overwrites *every* non-key column from ``r.get(c)`` and NULLs
    name/sector/GICS/quote (see docs/data_health.md, "Feasibility review"). A key
    absent from the table matches no row and is a no-op, so this can fill a
    backfill without ever growing or shrinking the table. One transaction per
    call; all statements share the module's connect/retry semantics.
    """
    spec = _check(name)
    _refuse_history(name, spec, "update_columns")
    if not rows:
        return 0
    valid = _columns_of(spec)
    for c in [key_col, *columns]:
        if c not in valid:
            raise RawStoreError(f"table {name} has no column {c!r}")
    _auto_ensure(write=True)
    rows = _json_rows(name, rows)
    set_sql = ", ".join(f'"{c}" = ?' for c in columns)
    sql = f'UPDATE {name} SET {set_sql} WHERE "{key_col}" = ?'

    def _op():
        with connect(db=_db_of(name)) as conn:
            _ensure_table(conn, name)
            with _tx(conn):
                if len(rows) >= _BULK_MIN_ROWS:
                    _bulk_update(conn, name, key_col, columns, rows)
                else:
                    conn.executemany(
                        sql, [[r.get(c) for c in columns] + [r.get(key_col)] for r in rows])
                _capture_changes(conn, name, touched=columns)

    _retry_write(_op)
    return len(rows)


def _bulk_update(conn, name: str, key_col: str, columns: list[str], rows: list[dict]) -> None:
    """``UPDATE ... FROM`` a registered batch — one statement instead of N."""
    import pandas as pd
    cols = [key_col, *columns]
    bases = [_base_type(name, c) for c in cols]
    # Row-by-row UPDATEs let the last row for a key win; keep that.
    rows = _dedupe(rows, [key_col], keep="last")
    frame = pd.DataFrame([[_cell_text(r.get(c), b) for c, b in zip(cols, bases)] for r in rows],
                         columns=cols, dtype=object)
    view = "_rc_upd"
    conn.execute(f"SET pandas_analyze_sample = {max(len(rows), 1000)}")
    conn.register(view, frame)
    try:
        sets = ", ".join(f'"{c}" = CAST({view}."{c}" AS {b})'
                         for c, b in zip(columns, bases[1:]))
        conn.execute(f'UPDATE {name} SET {sets} FROM {view} '
                     f'WHERE {name}."{key_col}" = CAST({view}."{key_col}" AS {bases[0]})')
    finally:
        conn.unregister(view)


def replace_all(name: str, rows: list[dict], *, allow_empty: bool = False) -> int:
    """Atomically make ``rows`` the ENTIRE contents of a table.

    The full-refresh equivalent of the JSON stores these tables replaced: keys
    absent from ``rows`` are removed, instead of lingering forever under the
    per-row upsert. Delete + insert share one transaction, so a failure leaves
    the previous contents untouched.

    An EMPTY ``rows`` would delete everything and write nothing, which is what a
    degraded run (no peers, no edges, a fetch that returned nothing) produces by
    accident. It raises unless the caller says it means it with
    ``allow_empty=True`` (#284).
    """
    spec = _check(name)
    _refuse_history(name, spec, "replace_all")
    if not rows and not allow_empty:
        raise RawStoreError(
            f"replace_all({name!r}, []) would empty the table; pass allow_empty=True if an "
            f"empty table is the intent, or keep the stored rows when a run produced nothing")
    cols = _columns_of(spec)
    _auto_ensure(write=True)
    rows = _json_rows(name, rows)

    def _op():
        with connect(db=_db_of(name)) as conn:
            _ensure_table(conn, name)
            with _tx(conn):
                conn.execute(f"DELETE FROM {name}")
                if len(rows) >= _BULK_MIN_ROWS:
                    _bulk_insert(conn, name, name, cols, rows, conflict_sql="")
                elif rows:
                    conn.executemany(
                        f"INSERT INTO {name} ({_col_sql(cols)}) "
                        f"VALUES ({', '.join(['?'] * len(cols))})",
                        [[r.get(c) for c in cols] for r in rows])
                _capture_changes(conn, name)

    _retry_write(_op)
    return len(rows)


class _Tx:
    """Handle passed to ``transaction()`` — the same write API, one transaction."""

    def __init__(self, conn, db: str = "raw"):
        self._conn = conn
        self._db = db

    def _own(self, name: str) -> dict:
        spec = _check(name)
        if _db_of(name) != self._db:
            raise RawStoreError(f"{name} lives in the {_db_of(name)} file; this transaction writes the {self._db} "
                                f"file (one transaction, one file)")
        return spec

    def execute(self, sql: str, params: list | None = None):
        _refuse_history_sql(self._conn, sql)
        tracked = _tracked_writes(self._conn, sql, self._db)
        res = self._conn.execute(sql, params or [])
        if not tracked:
            return res
        res = _Fetched(res)            # read before the capture reuses the connection
        for t in tracked:
            _capture_changes(self._conn, t)
        return res

    def ensure_table(self, name: str) -> None:
        """Create ``name`` if missing, inside this transaction (before ``execute``)."""
        self._own(name)
        _ensure_table(self._conn, name)

    def write_rows(self, name: str, rows: list[dict]) -> int:
        spec = self._own(name)
        if not rows:
            return 0
        _ensure_table(self._conn, name)
        _apply_write(self._conn, name, spec, _json_rows(name, rows))
        _capture_changes(self._conn, name)
        return len(rows)


@contextmanager
def transaction(db: str = "raw"):
    """One connection + one transaction spanning SEVERAL tables.

    For writes that must land together or not at all — e.g. a 13F zip's
    submissions/holdings/names plus its ``ingested_zips`` marker, where a
    half-applied zip would be silently re-ingested and double-counted. All
    tables in one transaction must share ``db``.

    Unlike ``write_rows``/``replace_all`` this cannot retry a lost commit race
    for you — the caller owns the block, so it must re-run it itself. Use
    ``atomic(fn)`` for a block that should be retried.
    """
    _auto_ensure(write=True)
    with connect(db=db) as conn:
        with _tx(conn):
            yield _Tx(conn, db)


def atomic(fn, db: str = "raw"):
    """Run ``fn(tx)`` in ONE transaction, retrying a lost commit race.

    ``fn`` receives the ``transaction()`` handle (``tx.write_rows``,
    ``tx.execute``) and must be safe to re-run: a conflicting attempt is rolled
    back whole before the retry. Returns ``fn``'s result.
    """
    _auto_ensure(write=True)

    def _op():
        with connect(db=db) as conn:
            with _tx(conn):
                return fn(_Tx(conn, db))

    return _retry_write(_op)


def execute(sql: str, params: list | None = None, db: str = "raw") -> None:
    """Run a single write statement (DELETE/UPDATE) against a store file."""
    _auto_ensure(write=True)
    with connect(db=db) as conn:
        _refuse_history_sql(conn, sql)
        tracked = _tracked_writes(conn, sql, db)
        if not tracked:
            conn.execute(sql, params or [])
            return
        with _tx(conn):
            conn.execute(sql, params or [])
            for t in tracked:
                _capture_changes(conn, t)


def query(sql: str, params: list | None = None, read_only: bool = True,
          db: str = "raw") -> list[dict]:
    """Run a read query; returns rows as dicts. Safe for raw SQL from callers."""
    if read_only and not _db_path(db).exists():
        return []
    _auto_ensure(write=not read_only)
    try:
        with connect(read_only=read_only, db=db) as conn:
            _refuse_history_sql(conn, sql)
            tracked = _tracked_writes(conn, sql, db)
            if not tracked:
                cur = conn.execute(sql, params or [])
                cols = [d[0] for d in cur.description] if cur.description else []
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            with _tx(conn):                  # a write sent through query(): captured like execute()
                cur = conn.execute(sql, params or [])
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                for t in tracked:
                    _capture_changes(conn, t)
            return rows
    except duckdb.CatalogException:
        # Missing table == no data (reads stay lenient; a pre-migration DB may
        # not have every table yet).
        if read_only:
            return []
        raise


def get(name: str, key) -> dict | None:
    """Fetch one row by its key column(s). ``key`` may be a single value or tuple."""
    spec = _check(name)
    key_cols = spec.get("key") or []
    if not key_cols:
        raise RawStoreError(f"table {name} has no key column")
    if not isinstance(key, tuple):
        key = (key,)
    if len(key_cols) != len(key):
        raise RawStoreError(f"table {name} expects {len(key_cols)} key values, got {len(key)}")
    where = " AND ".join(f'"{c}" = ?' for c in key_cols)
    rows = query(f"SELECT * FROM {name} WHERE {where}", list(key), db=_db_of(name))
    if not rows:
        return None
    return _decode_row(name, rows[0])


def get_many(name: str, keys, columns: list[str] | None = None) -> dict:
    """``{key: row}`` for the existing rows among ``keys`` of a table with ONE key column.

    One query for the lot (``get`` costs one per key); rows are decoded as ``get`` decodes them.
    ``columns`` narrows the SELECT (the key column is always included).
    """
    spec = _check(name)
    key_cols = spec.get("key") or []
    if len(key_cols) != 1:
        raise RawStoreError(f"table {name} needs exactly one key column for get_many")
    keys = list(dict.fromkeys(keys))
    if not keys:
        return {}
    key_col = key_cols[0]
    select = "*"
    if columns:
        valid = set(_columns_of(spec))
        bad = [c for c in columns if c not in valid]
        if bad:
            raise RawStoreError(f"table {name} has no column(s) {bad!r}")
        select = _col_sql(list(dict.fromkeys([key_col, *columns])))
    marks = ", ".join("?" for _ in keys)
    rows = query(f'SELECT {select} FROM {name} WHERE "{key_col}" IN ({marks})', keys, db=_db_of(name))
    return {row[key_col]: _decode_row(name, row) for row in rows}


def _decode_row(name: str, row: dict) -> dict:
    """Parse stored JSON columns back to Python objects.

    A ``JSON`` column holds JSON text, so its cell is decoded whatever the
    contained type is (dict, list, or a scalar — a plain string in a JSON
    column round-trips as ``"RTX"``, not ``'"RTX"'``). Legacy TEXT-blob columns
    are only re-parsed when they hold a JSON object/array (their pre-schema
    evolution contract). ``compress`` columns are decompressed (and decoded).
    """
    spec = MANIFEST[name]
    compress = spec.get("compress") or {}
    for col in _columns_of(spec):
        v = row.get(col)
        if col in compress and v is not None:
            row[col] = _decompress(v, compress[col])
            continue
        if not isinstance(v, str):
            continue
        try:
            parsed = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            continue
        base = _col_type(name, col).split("(", 1)[0].rstrip("[]")
        # ``text_verbatim`` tables hold third-party text: a TEXT cell is never re-parsed.
        if base in _NESTED_TYPES or (isinstance(parsed, (dict, list))
                                     and not spec.get("text_verbatim")):
            if base in _JSON_TYPES and isinstance(parsed, str) and not spec.get("text_verbatim"):
                # Legacy double-encoded cell (#202): decode once more, but only when the
                # inner text is itself a JSON object/array; any other string stays as is.
                try:
                    inner = json.loads(parsed)
                except (json.JSONDecodeError, TypeError):
                    inner = None
                if isinstance(inner, (dict, list)):
                    parsed = inner
            row[col] = parsed
    return row


def get_all(name: str, columns: list[str] | None = None) -> list[dict]:
    """All rows as dicts; JSON columns parsed back to objects.

    ``columns`` narrows the SELECT — e.g. ``chain_filings`` without its
    ``item_text`` blobs when only the filing dates are needed.
    """
    spec = _check(name)
    if columns:
        valid = set(_columns_of(spec))
        bad = [c for c in columns if c not in valid]
        if bad:
            raise RawStoreError(f"table {name} has no column(s) {bad!r}")
        select = _col_sql(columns)
    else:
        select = "*"
    return [_decode_row(name, row)
            for row in query(f"SELECT {select} FROM {name}", db=_db_of(name))]


def count(name: str) -> int:
    _check(name)
    rows = query(f"SELECT COUNT(*) AS n FROM {name}", db=_db_of(name))
    return int(rows[0]["n"]) if rows else 0


def delete(name: str, key) -> None:
    """Delete a single row by its key column(s)."""
    spec = _check(name)
    _refuse_history(name, spec, "delete")
    key_cols = spec.get("key") or []
    if not key_cols:
        raise RawStoreError(f"table {name} has no key column")
    if not isinstance(key, tuple):
        key = (key,)
    _auto_ensure(write=True)
    where = " AND ".join(f'"{c}" = ?' for c in key_cols)
    with connect(db=_db_of(name)) as conn:
        with _tx(conn):
            conn.execute(f"DELETE FROM {name} WHERE {where}", list(key))
            _capture_changes(conn, name)


def clear(name: str) -> None:
    """Delete all rows from a manifest table.

    Prefer ``replace_all`` when new rows follow: clear-then-write across two
    calls is two transactions, so a failed write leaves the table empty and a
    concurrent reader can observe the gap.
    """
    _refuse_history(name, _check(name), "clear")
    _auto_ensure(write=True)
    with connect(db=_db_of(name)) as conn:
        _ensure_table(conn, name)
        with _tx(conn):
            conn.execute(f"DELETE FROM {name}")
            _capture_changes(conn, name)


# ── watchlist (single accessor for all 6+ inline readers) ──────────────────
def load_watchlist() -> set[str]:
    """Current watchlist tickers (from the ``watchlist`` table)."""
    rows = get_all("watchlist")
    return {r["ticker"] for r in rows if r.get("ticker")}


def save_watchlist(tickers: set[str]) -> None:
    """Replace the whole watchlist (atomically). An empty set clears it: that is the user's choice.
    The dashboard does not call it: a click goes through ``toggle_watchlist``."""
    replace_all("watchlist", [{"ticker": t} for t in sorted(tickers)], allow_empty=True)


def toggle_watchlist(ticker: str) -> bool:
    """Add ``ticker`` to the watchlist, or remove it when it is there; returns whether it is in now (#306, D36).

    One transaction that reads and writes this ticker's row only, so two clicks that race cannot drop each other's
    ticker (a read-modify-write of the whole list did), and a lost commit race re-runs against the winner's state.
    The store records the click in ``watchlist_changes`` ('present', or 'absent' on a removal)."""
    def _flip(tx) -> bool:
        tx.ensure_table("watchlist")
        if tx.execute("SELECT 1 FROM watchlist WHERE ticker = ?", [ticker]).fetchone():
            tx.execute("DELETE FROM watchlist WHERE ticker = ?", [ticker])
            return False
        tx.write_rows("watchlist", [{"ticker": ticker}])
        return True

    return atomic(_flip)


def reset_db(include_edgar: bool = True) -> None:
    """Drop and recreate the database files (used by migration --reset / tests).

    ``include_edgar=False`` recreates ONLY the raw file. The migration's
    ``--reset`` must pass it: it points ``DB_PATH`` at ``raw.duckdb.new``, and
    ``edgar_db_path()`` — a sibling of ``DB_PATH`` — is then still the LIVE
    ``edgar.duckdb``, which this used to delete. The same flag guards the sibling
    ``discord.duckdb``, ``history.duckdb`` and ``prices.duckdb``.
    """
    targets = [Path(DB_PATH)] + (
        [edgar_db_path(), discord_db_path(), history_db_path(), prices_db_path()] if include_edgar else [])
    for p in targets:
        if p.exists():
            p.unlink()
        _TABLE_READY.difference_update({k for k in _TABLE_READY if k[0] == str(p)})
    _ENSURED.pop(str(Path(DB_PATH)), None)
    ensure_all_tables()


def compact(db: str = "raw") -> dict:
    """Rewrite a store file without its free blocks; swap it in; verify.

    DuckDB re-uses the blocks freed by DELETE/``replace_all`` but never gives
    them back to the OS, so a file only grows. ``COPY FROM DATABASE`` writes a
    fresh, densely packed copy; row counts are verified table by table before
    the swap. The previous file is kept as ``<name>.pre-compact`` (overwritten
    each time) until the caller deletes it.

    Needs the file to itself: refuses while another RevenueChaser process is
    using the store, and the swap fails (leaving the original untouched) if any
    other process holds it.
    """
    src = _db_path(db)
    if not src.exists():
        raise RawStoreError(f"no store file at {src}")
    peers = _peer_pids(src)
    if peers:
        raise RawStoreError(f"store in use by RevenueChaser process(es) {peers}; "
                            f"stop them and retry")
    if _sessions().get("depth"):
        raise RawStoreError("compact() cannot run inside a session()")
    dst = src.with_name(src.stem + ".compact.duckdb")
    bak = src.with_name(src.name + ".pre-compact")
    dst.unlink(missing_ok=True)
    before = src.stat().st_size
    conn = _connect_with_retry(_WRITE_DELAYS, src)
    try:
        catalog = conn.execute("SELECT current_database()").fetchone()[0]
        tables = [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'").fetchall()]
        counts = {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
        conn.execute(f"ATTACH '{dst.as_posix()}' AS _rc_compact")
        try:
            conn.execute(f'COPY FROM DATABASE "{catalog}" TO _rc_compact')
            got = {t: conn.execute(f'SELECT count(*) FROM _rc_compact.main."{t}"').fetchone()[0]
                   for t in tables}
        finally:
            conn.execute("DETACH _rc_compact")
    finally:
        with _ATTACH_LOCK:
            conn.close()
    if got != counts:
        dst.unlink(missing_ok=True)
        bad = {t: (counts[t], got.get(t)) for t in counts if counts[t] != got.get(t)}
        raise RawStoreError(f"compact verification failed: {bad}")
    os.replace(src, bak)
    try:
        os.replace(dst, src)
    except OSError:
        os.replace(bak, src)  # put the original back
        raise
    _TABLE_READY.difference_update({k for k in _TABLE_READY if k[0] == str(src)})
    return {"db": db, "before_bytes": before, "after_bytes": src.stat().st_size,
            "tables": len(tables), "previous": str(bak)}


def schema_version() -> str:
    return _SCHEMA_VERSION


@contextmanager
def brief_wait():
    """Within the block, a lock held by another RevenueChaser process is waited for only as long as a foreign
    holder is (~0.35 s), then ``StoreLockedError``. For best-effort bookkeeping writes (the ``job_runs`` row, #282)
    that must not hold up the caller for the ~29 s a real peer wait takes. In-process races keep their budget."""
    previous = getattr(_LOCAL, "brief", False)
    _LOCAL.brief = True
    try:
        yield
    finally:
        _LOCAL.brief = previous
