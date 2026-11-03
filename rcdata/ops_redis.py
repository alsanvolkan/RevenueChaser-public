"""Redis storage for ``rcdata.ops`` (master plan Order 3): the same tables, rows and answers as ``ops.sqlite``.

``ops`` routes every public function here while ``RC_REDIS_URL`` is set; nothing else imports this module. The
Redis server is a container (``docker-compose.yml`` at the repository root) with AOF on, so a restart keeps the
day's call counts, the queue and the open issues.

**Layout.** Every key starts with the namespace: ``RC_REDIS_NAMESPACE`` when set (the server's, ``rc:ops:live``), else
``rc:ops:<12 hex of sha1(resolved RC_MARKET_DIR)>``, one namespace per market root, as there was one ``ops.sqlite`` per
market root (tests and the e2e dashboards isolate themselves by moving that root, and keep doing so). Under it, per table of ``ops.TABLES``:

- ``<ns>:<table>:row:<primary key>``: a hash, one field per non-NULL column (NULL is a missing field). Composite keys
  are joined with ``|``. The column types come from ``ops._DDL`` itself, so the two backends cannot drift.
- ``<ns>:<table>``: a sorted set of every primary key (score: the id; ``sampled_at`` / ``started_at`` for the two
  time-keyed tables; 0 elsewhere).
- ``<ns>:<table>:seq``: the last id given out (``INCR``); ``<ns>:<table>:v``: bumped by every write to the table.
- The lookups the reads need: ``build_requests:active`` / ``:finished``, ``dataset_check:ds:<dataset>``,
  ``build_issues:open:<dataset>`` and ``:open:<dataset>:kind:<kind>`` (unresolved rows; ``:kinds:<dataset>`` lists the
  kinds), ``job_runs2:running``.
- ``<ns>:ops_schema``: the schema stamp (``ops.OPS_SCHEMA_VERSION``).

**Atomicity.** What SQLite did in one ``BEGIN IMMEDIATE`` transaction is a ``WATCH`` of the tables' ``:v`` keys, the
reads, then one ``MULTI``/``EXEC`` of the writes; a write by anyone else to a watched table in between makes ``EXEC``
fail, and the whole step runs again (optimistic, never a lost update), at most ``MAX_ATTEMPTS`` times with a jittered
backoff, then ``WatchError``, which callers treat as a busy store. A lone write is one ``MULTI``/``EXEC``. The
backup's ``snapshot`` is one Lua script, so a steady writer cannot starve it.
"""
from __future__ import annotations

import functools
import hashlib
import os
import random
import socket
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Iterable

import redis

from ..core import config
from . import ops

_TYPES = {"INTEGER": int, "REAL": float, "TEXT": str}
_ACTIVE = ("queued", "running")
_PAGE = 50
# The optimistic retry (review S1): this many attempts, waiting a jittered, doubling pause between them, then the
# WatchError goes up; it is a RedisError, so the callers' busy paths (ops.BUSY_ERRORS) take it. ~3 s at the worst.
MAX_ATTEMPTS = 25
BACKOFF_FIRST_S = 0.005
BACKOFF_MAX_S = 0.25
_ENSURED: set[str] = set()
_CLIENTS: dict[str, redis.Redis] = {}


# ── Keys, rows, the client ─────────────────────────────────────────────────────────────────────────────────────

@functools.cache
def _spec(table: str) -> tuple[tuple[tuple[str, type], ...], tuple[str, ...]]:
    """``(((column, type), ...), primary key columns)`` of ``table``, read from its SQLite DDL."""
    if table not in ops._DDL or table == "ops_schema":
        raise ValueError(f"unknown ops table {table!r}")
    with closing(sqlite3.connect(":memory:")) as con:
        con.execute(ops._DDL[table])
        info = list(con.execute(f"PRAGMA table_info({table})"))
    return (tuple((r[1], _TYPES[r[2].upper()]) for r in info),
            tuple(c for _, c in sorted((r[5], r[1]) for r in info if r[5])))


def _client() -> redis.Redis:
    url = os.environ[ops.REDIS_URL_ENV].strip()
    client = _CLIENTS.get(url)
    if client is None:
        wait = ops.BUSY_TIMEOUT_MS / 1000            # the same patience SQLite's busy_timeout gave a writer
        client = _CLIENTS[url] = redis.Redis.from_url(url, decode_responses=True, socket_timeout=wait,
                                                      socket_connect_timeout=wait)
    return client


# The server names its namespace (review S2): a moved or remounted market root must not start an empty store.
NAMESPACE_ENV = "RC_REDIS_NAMESPACE"


def namespace() -> str:
    """``RC_REDIS_NAMESPACE`` when set (the server's, e.g. ``rc:ops:live``), else one per market root: the tests and
    the e2e dashboards isolate themselves by their roots (see the module docstring)."""
    explicit = os.getenv(NAMESPACE_ENV, "").strip()
    if explicit:
        return explicit
    root = str(Path(config.MARKET_DIR).resolve())
    return "rc:ops:" + hashlib.sha1(root.encode("utf-8")).hexdigest()[:12]


def _key(*parts: Any) -> str:
    return ":".join((namespace(), *map(str, parts)))


def _pk(table: str, row: dict) -> str:
    return "|".join(_enc(dict(_spec(table)[0])[c], row[c]) for c in _spec(table)[1])


def _enc(kind: type, value: Any) -> str:
    if kind is float:
        return repr(float(value))
    return str(int(value)) if kind is int else str(value)


def _decode(table: str, fields: dict) -> dict:
    return {c: (None if fields.get(c) is None else kind(fields[c])) for c, kind in _spec(table)[0]}


def _fetch(table: str, pks: Iterable[str]) -> list[dict]:
    """The rows of ``pks``, in that order (a key that is gone is left out)."""
    pipe = _client().pipeline(transaction=False)
    for pk in pks:
        pipe.hgetall(_key(table, "row", pk))
    return [_decode(table, h) for h in pipe.execute() if h]


def _all(table: str) -> list[dict]:
    return _fetch(table, _client().zrange(_key(table), 0, -1))


def _one(table: str, *pk_values: Any) -> dict | None:
    cols = dict(_spec(table)[0])
    got = _fetch(table, ["|".join(_enc(cols[c], v) for c, v in zip(_spec(table)[1], pk_values))])
    return got[0] if got else None


def _score(table: str, row: dict) -> float:
    if table == "uw_remaining":
        return row["sampled_at"]
    if table == "job_runs2":
        return row["started_at"] or 0.0
    return row["id"] if "id" in _spec(table)[1] else 0.0


def _put(p, table: str, row: dict) -> None:
    """Queue the write of one whole row (and its lookups) on ``p``, a pipeline in ``MULTI``."""
    cols = _spec(table)[0]
    full = {c: row.get(c) for c, _ in cols}
    pk = _pk(table, full)
    k = _key(table, "row", pk)
    p.hset(k, mapping={c: _enc(kind, full[c]) for c, kind in cols if full[c] is not None})
    nulls = [c for c, _ in cols if full[c] is None]
    if nulls:
        p.hdel(k, *nulls)
    p.zadd(_key(table), {pk: _score(table, full)})
    if table == "build_requests":
        on, off = ("active", "finished") if full["status"] in _ACTIVE else ("finished", "active")
        p.zadd(_key(table, on), {pk: full["id"]})
        p.zrem(_key(table, off), pk)
    elif table == "dataset_check":
        p.zadd(_key(table, "ds", full["dataset"]), {pk: full["id"]})
    elif table == "build_issues":
        lookups = (_key(table, "open", full["dataset"]), _key(table, "open", full["dataset"], "kind", full["kind"]))
        if full["resolved_on"] is None:
            for lk in lookups:
                p.zadd(lk, {pk: full["id"]})
            p.sadd(_key(table, "kinds", full["dataset"]), full["kind"])
        else:
            for lk in lookups:
                p.zrem(lk, pk)
    elif table == "job_runs2":
        (p.sadd if full["status"] == "running" else p.srem)(_key(table, "running"), pk)
    p.incr(_key(table, "v"))


def _delete(p, table: str, row: dict) -> None:
    pk = _pk(table, row)
    p.delete(_key(table, "row", pk))
    p.zrem(_key(table), pk)
    p.incr(_key(table, "v"))


def _new_ids(table: str, n: int = 1) -> list[int]:
    last = _client().incrby(_key(table, "seq"), n)
    return list(range(last - n + 1, last + 1))


def _write(table: str, *rows: dict) -> None:
    with _client().pipeline(transaction=True) as p:
        for row in rows:
            _put(p, table, row)
        p.execute()


def _transaction(tables: Iterable[str], work: Callable[[], tuple[Any, Callable | None]]) -> Any:
    """Run ``work`` (reads; returns ``(result, writes)``) under a ``WATCH`` of ``tables``, then ``writes(pipe)`` in one
    ``MULTI``/``EXEC``; again from the start when another client wrote to one of the tables in between, at most
    ``MAX_ATTEMPTS`` times, then ``WatchError`` (a busy store to the caller)."""
    _ready()
    tables = list(tables)
    for attempt in range(MAX_ATTEMPTS):
        with _client().pipeline(transaction=True) as p:
            try:
                p.watch(*(_key(t, "v") for t in tables))
                result, writes = work()
                if writes is not None:
                    p.multi()
                    writes(p)
                    p.execute()
                return result
            except redis.WatchError:
                if attempt + 1 < MAX_ATTEMPTS:
                    _backoff(attempt)
    raise redis.WatchError(f"ops store busy: {', '.join(tables)} kept changing, {MAX_ATTEMPTS} attempts")


def _backoff_s(attempt: int) -> float:
    """Full jitter: a random pause up to ``BACKOFF_FIRST_S * 2**attempt``, capped at ``BACKOFF_MAX_S``."""
    return random.uniform(BACKOFF_FIRST_S / 2, min(BACKOFF_MAX_S, BACKOFF_FIRST_S * 2 ** attempt))


def _backoff(attempt: int) -> None:
    time.sleep(_backoff_s(attempt))


# ── The schema stamp ───────────────────────────────────────────────────────────────────────────────────────────

def ensure(path: Path | None = None) -> None:
    """Stamp ``ops_schema`` (``path`` is the SQLite file's and means nothing here); ``OpsSchemaTooNew`` on a newer
    stamp. Redis needs no tables, so an older stamp is brought up by writing the new one.

    A namespace never stamped is a new store: refused (``OpsNotCopied``) while ``ops.sqlite`` of this root holds rows,
    so switching Redis on before the copy, or losing the namespace, cannot start the day from zero."""
    if _client().get(_key("ops_schema")) is None:
        held = _rows_in_file(ops.ops_path())
        if held:
            raise ops.OpsNotCopied(f"Redis namespace {namespace()} is empty and {ops.ops_path()} holds {held} rows: "
                                   f"copy it first (python -m RevenueChaser.rcdata.ops_copy), or unset "
                                   f"{ops.REDIS_URL_ENV} to stay on the file")
    _stamp()


def _rows_in_file(path: Path) -> int:
    if not path.is_file():
        return 0
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as con:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return sum(con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                   for t in ops.TABLES if t != "ops_schema" and t in names)


def _stamp() -> None:
    """Write this checkout's schema stamp (``ensure`` without the empty-namespace check: the copy's way in)."""
    r, k = _client(), _key("ops_schema")
    r.set(k, ops.OPS_SCHEMA_VERSION, nx=True)
    found = int(r.get(k))
    if found > ops.OPS_SCHEMA_VERSION:
        raise ops.OpsSchemaTooNew(f"Redis {namespace()} is ops schema {found}; this checkout knows "
                                  f"{ops.OPS_SCHEMA_VERSION}")
    if found < ops.OPS_SCHEMA_VERSION:
        r.set(k, ops.OPS_SCHEMA_VERSION)
    _ENSURED.add(namespace())


def _ready() -> None:
    if namespace() not in _ENSURED:
        ensure()


def schema_version() -> int | None:
    found = _client().get(_key("ops_schema"))
    return None if found is None else int(found)


# ── The request queue ──────────────────────────────────────────────────────────────────────────────────────────

def _request_row(dataset: str, params: dict | None, by: str) -> dict:
    return {"id": _new_ids("build_requests")[0], "dataset": dataset, "requested_at": time.time(), "requested_by": by,
            "params": ops._json(params), "status": "queued"}


def _active() -> list[dict]:
    return _fetch("build_requests", _client().zrange(_key("build_requests", "active"), 0, -1))


def enqueue(dataset: str, params: dict | None = None, *, by: str) -> int:
    _ready()
    row = _request_row(dataset, params, by)
    _write("build_requests", row)
    return row["id"]


def enqueue_once(dataset: str, params: dict | None = None, *, by: str) -> int | None:
    def work():
        if any(r["dataset"] == dataset for r in _active()):
            return None, None
        row = _request_row(dataset, params, by)
        return row["id"], lambda p: _put(p, "build_requests", row)
    return _transaction(["build_requests"], work)


def queue_view(finished: int = 20) -> dict:
    finished = int(finished)
    done = _client().zrevrange(_key("build_requests", "finished"), 0, finished - 1) if finished > 0 else []
    return {"active": [ops._request(r) for r in _active()],
            "finished": [ops._request(r) for r in _fetch("build_requests", done)]}


read_queue = queue_view


def next_request(dataset: str | None = None, *, skip: Iterable[str] = (),
                 only: Iterable[str] | None = None, by: str | None = None) -> dict | None:
    skip = tuple(skip)
    if skip and only is not None:
        raise ValueError("next_request takes skip or only, not both")
    names = None if only is None else set(only)

    def work():
        for r in _active():
            if (r["status"] == "queued" and dataset in (None, r["dataset"]) and r["dataset"] not in skip
                    and by in (None, r["requested_by"]) and (names is None or r["dataset"] in names)):
                row = {**r, "status": "running", "started_at": time.time()}
                return ops._request(row), lambda p: _put(p, "build_requests", row)
        return None, None
    return _transaction(["build_requests"], work)


def queued(dataset: str, *, by: str | None = None) -> bool:
    return any(r["status"] == "queued" and r["dataset"] == dataset and by in (None, r["requested_by"])
               for r in _active())


def finish_request(request_id: int, status: str, note: str | None = None) -> None:
    if status not in ops.REQUEST_STATUSES:
        raise ValueError(f"unknown request status {status!r}")

    def work():
        row = _one("build_requests", request_id)
        if row is None:
            return None, None
        row = {**row, "status": status, "ended_at": time.time(), "note": note}
        return None, lambda p: _put(p, "build_requests", row)
    _transaction(["build_requests"], work)


def fail_orphans() -> tuple[int, int]:
    me = os.getpid()
    now, tail = time.time(), f"builder died; found running by the builder started as pid {me}"

    def work():
        runs = sorted((r for r in _fetch("job_runs2", _client().smembers(_key("job_runs2", "running")))
                       if r["status"] == "running" and r["pid"] != me),
                      key=lambda r: (r["started_at"] is not None, r["started_at"] or 0.0))
        alive = {r["run_id"]: ops._builder_alive(r["pid"]) for r in runs}
        reqs, checks = [], []
        for req in (r for r in _active() if r["status"] == "running"):
            run = next((r for r in runs if r["dataset"] == req["dataset"]
                        and (r["started_at"] or 0) >= (req["started_at"] or 0)), None)
            if run is not None and alive[run["run_id"]]:
                continue
            note = (f"lost: pid {run['pid']} gone" if run is not None else "lost: pid unknown (no run row)")
            note += f" ({tail})"
            reqs.append({**req, "status": "failed", "ended_at": now, "note": note})
            checks.append({"id": _new_ids("dataset_check")[0], "dataset": req["dataset"], "checked_on": ops._today(),
                           "result": "failed", "run_id": run["run_id"] if run else None, "note": note})
        dead = [{**r, "status": "failed", "finished_at": now, "error": f"lost: pid {r['pid']} gone ({tail})"}
                for r in runs if not alive[r["run_id"]]]

        def writes(p):
            for row in reqs:
                _put(p, "build_requests", row)
            for row in checks:
                _put(p, "dataset_check", row)
            for row in dead:
                _put(p, "job_runs2", row)
        return (len(reqs), len(dead)), (writes if reqs or dead else None)
    return _transaction(["build_requests", "job_runs2", "dataset_check"], work)


# ── Checks, issues, runs ───────────────────────────────────────────────────────────────────────────────────────

def record_check(dataset: str, result: str, *, source_as_of: str | None = None, rows: int | None = None,
                 added: int | None = None, removed: int | None = None, coverage: float | None = None,
                 calls: int | None = None, next_check_due: str | None = None, run_id: str | None = None,
                 note: str | None = None, checked_on: str | None = None) -> int:
    if result not in ops.CHECK_RESULTS:
        raise ValueError(f"unknown check result {result!r}")
    _ready()
    values = (dataset, checked_on or ops._today(), result, None if source_as_of is None else str(source_as_of), rows,
              added, removed, coverage, calls, next_check_due, run_id, note)
    row = {"id": _new_ids("dataset_check")[0], **dict(zip(ops.CHECK_FIELDS, values))}
    _write("dataset_check", row)
    return row["id"]


def _checks_newest_first(dataset: str) -> Iterable[dict]:
    """``dataset``'s check rows, newest first, read a page at a time."""
    start = 0
    while True:
        page = _client().zrevrange(_key("dataset_check", "ds", dataset), start, start + _PAGE - 1)
        if not page:
            return
        yield from _fetch("dataset_check", page)
        start += _PAGE


def latest_check(dataset: str) -> dict | None:
    return next(iter(_checks_newest_first(dataset)), None)


read_latest_check = latest_check


def recent_checks(dataset: str, limit: int = 20) -> list[dict]:
    limit = int(limit)
    return _fetch("dataset_check", _client().zrevrange(_key("dataset_check", "ds", dataset), 0, limit - 1)) \
        if limit > 0 else []


def latest_unskipped_check(dataset: str) -> dict | None:
    return next((r for r in _checks_newest_first(dataset) if r["result"] != "skipped"), None)


def latest_good_check(dataset: str) -> dict | None:
    return next((r for r in _checks_newest_first(dataset) if r["result"] in ("changed", "unchanged")), None)


def ever_built(dataset: str) -> bool:
    return latest_good_check(dataset) is not None


def record_issue(dataset: str, kind: str, key: str | None, detail: str | None, run_id: str | None = None) -> int:
    _ready()
    row = {"id": _new_ids("build_issues")[0], "dataset": dataset, "found_on": ops._today(), "kind": kind, "key": key,
           "detail": detail, "run_id": run_id}
    _write("build_issues", row)
    return row["id"]


def sync_issues(dataset: str, kinds, found, run_id: str | None = None) -> tuple[int, int]:
    kinds = sorted(set(kinds))
    want = {(k, key): detail for k, key, detail in found}
    stray = sorted({k for k, _ in want} - set(kinds))
    if stray:
        raise ValueError(f"found rows of kinds outside {kinds}: {stray}")
    if not kinds:
        return 0, 0
    today = ops._today()

    def work():
        r = _client()
        open_rows = _fetch("build_issues", [pk for k in kinds
                                            for pk in r.zrange(_key("build_issues", "open", dataset, "kind", k), 0, -1)])
        have = {(row["kind"], row["key"]): row for row in open_rows if row["resolved_on"] is None}
        gone = [{**row, "resolved_on": today} for k, row in have.items() if k not in want]
        fresh = [(k, key, detail) for (k, key), detail in sorted(want.items()) if (k, key) not in have]
        ids = _new_ids("build_issues", len(fresh)) if fresh else []
        new = [{"id": i, "dataset": dataset, "found_on": today, "kind": k, "key": key, "detail": detail,
                "run_id": run_id} for i, (k, key, detail) in zip(ids, fresh)]

        def writes(p):
            for row in gone + new:
                _put(p, "build_issues", row)
        return (len(new), len(gone)), (writes if gone or new else None)
    return _transaction(["build_issues"], work)


def open_issues(dataset: str, limit: int = 20) -> tuple[int, list[dict]]:
    r, k, limit = _client(), _key("build_issues", "open", dataset), int(limit)
    return r.zcard(k), (_fetch("build_issues", r.zrevrange(k, 0, limit - 1)) if limit > 0 else [])


def open_issue_kinds(dataset: str) -> list[dict]:
    r = _client()
    kinds = sorted(r.smembers(_key("build_issues", "kinds", dataset)))
    pipe = r.pipeline(transaction=False)
    for k in kinds:
        pipe.zcard(_key("build_issues", "open", dataset, "kind", k))
    counts = [{"kind": k, "count": n} for k, n in zip(kinds, pipe.execute()) if n]
    return sorted(counts, key=lambda c: (-c["count"], c["kind"]))


def start_run(run_id: str, dataset: str, trigger: str, params: dict | None = None) -> None:
    _ready()
    _write("job_runs2", {"run_id": run_id, "dataset": dataset, "trigger": trigger, "pid": os.getpid(),
                         "host": socket.gethostname(), "started_at": time.time(), "status": "running",
                         "params": ops._json(params)})


def finish_run(run_id: str, status: str, error: str | None = None) -> None:
    def work():
        row = _one("job_runs2", run_id)
        if row is None:
            return None, None
        row = {**row, "status": status, "finished_at": time.time(), "error": error}
        return None, lambda p: _put(p, "job_runs2", row)
    _transaction(["job_runs2"], work)


def last_builder_run() -> dict | None:
    newest = _fetch("job_runs2", _client().zrevrange(_key("job_runs2"), 0, 0))
    cols = ("run_id", "dataset", "trigger", "pid", "started_at", "finished_at", "status", "error")
    return {c: newest[0][c] for c in cols} if newest else None


# ── Provider accounting and the read log ───────────────────────────────────────────────────────────────────────

def _add(table: str, pk_values: tuple, fields: dict, adds: dict[str, int]) -> None:
    """Add ``adds`` to the row of ``pk_values`` in ``table`` (made with ``fields`` and zeros when missing)."""
    def work():
        row = _one(table, *pk_values) or {**fields, **{c: 0 for c in adds}}
        row = {**row, **{c: (row[c] or 0) + n for c, n in adds.items()}}
        return None, lambda p: _put(p, table, row)
    _transaction([table], work)


def count_calls(provider: str, n: int = 1, failures: int = 0, http_429: int = 0) -> None:
    day = ops._today()
    _add("provider_calls_daily", (provider, day), {"provider": provider, "ny_day": day},
         {"calls": n, "failures": failures, "http_429": http_429})


def calls_today(provider: str) -> int:
    row = _one("provider_calls_daily", provider, ops._today())
    return int(row["calls"]) if row else 0


def calls_by_day(first_day: str, last_day: str) -> list[dict]:
    return sorted((r for r in _all("provider_calls_daily") if first_day <= r["ny_day"] <= last_day),
                  key=lambda r: (r["provider"], r["ny_day"]))


def latest_uw_sample() -> dict | None:
    newest = _fetch("uw_remaining", _client().zrevrange(_key("uw_remaining"), 0, 0))
    cols = ("sampled_at", "run_id", "daily_req_count", "daily_cap", "daily_remaining_computed",
            "per_minute_remaining", "token_req_limit")
    return {c: newest[0][c] for c in cols} if newest else None


def _add_reads(process: str, counts: list[tuple[str, str, int]]) -> None:
    def work():
        merged = []
        for day, table, n in counts:
            row = _one("read_log", day, process, table) or {"day": day, "process": process, "table_name": table,
                                                            "reads": 0}
            merged.append({**row, "reads": row["reads"] + n})

        def writes(p):
            for row in merged:
                _put(p, "read_log", row)
        return None, writes
    _transaction(["read_log"], work)


def reads_since(first_day: str, tables) -> dict[str, int]:
    names = list(tables)
    if not names:
        return {}
    got: dict[str, int] = {}
    for r in _all("read_log"):
        if r["day"] >= first_day and r["table_name"] in names:
            got[r["table_name"]] = got.get(r["table_name"], 0) + r["reads"]
    return {t: got.get(t, 0) for t in names}


def need_reads(first_day: str) -> list[dict]:
    p = ops.NEED_READ_PREFIX
    rows = sorted((r for r in _all("read_log") if r["day"] >= first_day and r["table_name"].startswith(p)),
                  key=lambda r: (r["table_name"], r["process"], r["day"]))
    return [{"need": r["table_name"][len(p):], "process": r["process"], "day": r["day"], "reads": int(r["reads"])}
            for r in rows]


# ── Watch lists and the schedule ───────────────────────────────────────────────────────────────────────────────

def _watch_change(kind: str, ticker: str, action: str, by: str, note: str | None, now: float) -> list:
    """The writes of one add or remove: the list row and its history row (with a new id)."""
    change = {"id": _new_ids(f"watchlist_{kind}_changes")[0], "ticker": ticker, "action": action, "at": now,
              "by": by, "note": note}
    row = {"ticker": ticker, "added_at": now, "added_by": by, "note": note}
    head = ((lambda p: _put(p, f"watchlist_{kind}", row)) if action == "add"
            else (lambda p: _delete(p, f"watchlist_{kind}", row)))
    return [head, lambda p: _put(p, f"watchlist_{kind}_changes", change)]


def _watch_tables(kind: str) -> list[str]:
    return [f"watchlist_{kind}", f"watchlist_{kind}_changes"]


def _watch_run(kind: str, plan: Callable[[set[str]], tuple[Any, list[tuple[str, str]]]], by: str,
               note: str | None = None) -> Any:
    """``plan(tickers on the list)`` -> ``(result, [(ticker, action)])``; the moves are written in one step."""
    def work():
        result, moves = plan(set(_client().zrange(_key(f"watchlist_{kind}"), 0, -1)))
        now = time.time()
        writes = [w for t, action in moves for w in _watch_change(kind, t, action, by, note, now)]

        def run(p):
            for w in writes:
                w(p)
        return result, (run if writes else None)
    return _transaction(_watch_tables(kind), work)


def watch_add(kind: str, ticker: str, *, by: str, note: str | None = None) -> bool:
    kind, ticker = ops._watch_kind(kind), ticker.strip().upper()
    return _watch_run(kind, lambda have: (False, []) if ticker in have else (True, [(ticker, "add")]), by, note)


def watch_remove(kind: str, ticker: str, *, by: str) -> bool:
    kind, ticker = ops._watch_kind(kind), ticker.strip().upper()
    return _watch_run(kind, lambda have: (True, [(ticker, "remove")]) if ticker in have else (False, []), by)


def watch_toggle(kind: str, ticker: str, *, by: str) -> bool:
    kind, ticker = ops._watch_kind(kind), ticker.strip().upper()
    return _watch_run(kind, lambda have: ((False, [(ticker, "remove")]) if ticker in have
                                          else (True, [(ticker, "add")])), by)


def watch_replace(kind: str, tickers, *, by: str) -> tuple[int, int]:
    kind = ops._watch_kind(kind)
    want = {str(t).strip().upper() for t in tickers if str(t).strip()}
    return _watch_run(kind, lambda have: ((len(want - have), len(have - want)),
                                          [(t, "remove") for t in sorted(have - want)]
                                          + [(t, "add") for t in sorted(want - have)]), by)


def watch_list(kind: str) -> list[str]:
    return sorted(_client().zrange(_key(f"watchlist_{ops._watch_kind(kind)}"), 0, -1))


def watch_count(kind: str) -> int:
    return _client().zcard(_key(f"watchlist_{ops._watch_kind(kind)}"))


def watch_rows(kind: str) -> list[dict]:
    return sorted(_all(f"watchlist_{ops._watch_kind(kind)}"), key=lambda r: r["ticker"])


def watch_last_change() -> float | None:
    stamps = [r["at"] for k in ops.WATCH_KINDS for r in _all(f"watchlist_{k}_changes") if r["at"] is not None]
    return max(stamps) if stamps else None


def _schedule(dataset: str, change: Callable[[dict | None], dict | None]) -> None:
    def work():
        row = change(_one("dataset_schedule", dataset))
        return None, (None if row is None else lambda p: _put(p, "dataset_schedule", row))
    _transaction(["dataset_schedule"], work)


def schedule_set(dataset: str, cadence: str, next_due: str | None) -> None:
    _schedule(dataset, lambda row: {**(row or {"dataset": dataset}), "cadence": cadence, "next_due": next_due})


def schedule_pause(dataset: str, *, by: str) -> None:
    now = time.time()
    _schedule(dataset, lambda row: {**(row or {"dataset": dataset}), "paused_at": now, "paused_by": by})


def schedule_resume(dataset: str) -> None:
    _schedule(dataset, lambda row: None if row is None else {**row, "paused_at": None, "paused_by": None})


def schedule_get(dataset: str) -> dict | None:
    return _one("dataset_schedule", dataset)


read_schedule = schedule_get


# ── Whole tables ───────────────────────────────────────────────────────────────────────────────────────────────

def rows(table: str) -> list[dict]:
    pk = _spec(table)[1]
    return sorted(_all(table), key=lambda r: tuple(r[c] for c in pk))


def put_rows(table: str, new_rows: Iterable[dict], batch: int = 1000) -> int:
    _spec(table)
    _ready()
    new_rows, top = list(new_rows), 0
    for start in range(0, len(new_rows), batch):
        with _client().pipeline(transaction=True) as p:
            for row in new_rows[start:start + batch]:
                _put(p, table, row)
                top = max(top, int(row.get("id") or 0))
            p.execute()
    if top and "id" in _spec(table)[1]:
        # Ids go on from the highest copied one, as SQLite's would.
        seq = _key(table, "seq")
        _client().set(seq, max(top, int(_client().get(seq) or 0)))
    return len(new_rows)


# Every row of the tables named in ARGV, read inside the server in one script, so nothing is written in between.
_SNAPSHOT_LUA = """
local out = {}
for i, t in ipairs(ARGV) do
  local rows = {}
  for j, pk in ipairs(redis.call('ZRANGE', KEYS[1] .. ':' .. t, 0, -1)) do
    rows[j] = redis.call('HGETALL', KEYS[1] .. ':' .. t .. ':row:' .. pk)
  end
  out[i] = rows
end
return out
"""


def snapshot() -> dict[str, list[dict]]:
    """``{table: rows}`` of every table at one moment (the backup's consistent copy).

    One Lua script reads them all inside Redis, which runs nothing else meanwhile: a steady writer cannot make it
    start over, as it did a WATCHed read (review S1: 106 tries in 15 s, never done). Redis is blocked for that read,
    once a night: 0.31 s for 30,000 issue rows (measured 3 Nov 2026, Redis 7.0 on the build box)."""
    tables = [t for t in ops.TABLES if t != "ops_schema"]
    got = _client().eval(_SNAPSHOT_LUA, 1, namespace(), *tables)
    out = {}
    for t, raw in zip(tables, got):
        pk = _spec(t)[1]
        decoded = [_decode(t, dict(zip(flat[::2], flat[1::2]))) for flat in raw if flat]
        out[t] = sorted(decoded, key=lambda r: tuple(r[c] for c in pk))
    return out
