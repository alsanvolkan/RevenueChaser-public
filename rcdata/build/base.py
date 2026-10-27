"""What every builder dataset shares: the run context, the HTTP helper, raw and Parquet files, the pacer, the gates,
and ``run_request``, the one fetch → type → build → swap sequence.

A dataset (``Dataset``) has four steps:

- ``fetch(ctx, as_of)`` downloads raw files under ``<MARKET_RAW_DIR>/raw/<source>/<dataset>/...``. A raw file that
  exists is never fetched again: it is the resume marker. Raw files are written to ``<name>.tmp`` and renamed, so a
  file under its real name is always whole.
- ``to_parquet(ctx, raw)`` types a raw file into its Parquet partition under ``<MARKET_RAW_DIR>/parquet/...``, also
  through ``.tmp`` and a rename (``write_parquet``), so a partition that exists is complete; ``to_parquet_all`` does
  a run's raw files together, so a partition several of them touch is written once.
- ``build(ctx, con)`` creates the dataset's tables in the new serving file from its Parquet (no network).
- ``check(ctx, con)`` compares the new tables with the live file and returns the ``dataset_check`` row.

``run_request`` takes one queued request: it refuses a provider at its day cap (a ``skipped`` check, no call), runs
the dataset's fetch and typing, builds the whole serving kind from Parquet (every dataset of the kind, so one
dataset's run never drops another's tables), swaps it in, and writes the check row, the run row, the provider call
counts and UW's rate headers. An exception anywhere is a ``failed`` check row; the live file is untouched.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

import duckdb
import httpx

from ...core import calendar, config, job_lock, rawstore
from .. import caps, ops, serving

logger = logging.getLogger(__name__)

# Keyless sources: a small retry ladder of their own (429, 5xx and transport errors), every attempt counted.
HTTP_ATTEMPTS = 3
HTTP_BACKOFF_S = 2.0
HTTP_TIMEOUT_S = 60.0
USER_AGENT = "RevenueChaser market-data builder (+https://github.com/alsanvolkan/RevenueChaser-public)"
# The builder's own job-lock name: a second builder is refused while one runs.
BUILDER_LOCK = "rcdata_build"
PARQUET_COMPRESSION = "zstd"
PARQUET_ROW_GROUP = 100_000
# The call counter is written to ops.sqlite every this many calls of a provider (and at the end of the run, and on
# a kill), so a run killed mid-way still leaves its calls counted against the day cap (S1).
COUNT_FLUSH_EVERY = 25


class SourceError(RuntimeError):
    """A provider answer the dataset cannot use (an HTTP failure after retries, an empty or malformed body)."""


class CapReached(SourceError):
    """A provider reached its day cap during the run: nothing more is sent to it today."""


class NotIdle(RuntimeError):
    """Another job holds a job lock, or another builder runs: this run does not start."""


def raw_root() -> Path:
    return Path(config.MARKET_RAW_DIR) / "raw"


def parquet_root() -> Path:
    return Path(config.MARKET_RAW_DIR) / "parquet"


# ── The run context ────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class RunContext:
    run_id: str
    dataset: str
    trigger: str = "cli"
    fetched_at: float = field(default_factory=time.time)
    sleep: Callable[[float], Any] = time.sleep
    clock: Callable[[], float] = time.monotonic
    as_of: date | None = None
    calls: dict[str, list[int]] = field(default_factory=dict)       # provider -> [calls, failures, http_429]
    notes: list[str] = field(default_factory=list)
    uw_sampled: bool = False
    allow_shrink: bool = False                                      # a request's explicit "allow_shrink": true
    _pending: dict[str, list[int]] = field(default_factory=dict)    # counted, not yet written to ops.sqlite
    _http: httpx.Client | None = None
    _uw: Any = None

    def count(self, provider: str, calls: int = 0, failures: int = 0, http_429: int = 0) -> None:
        for book in (self.calls, self._pending):
            c = book.setdefault(provider, [0, 0, 0])
            c[0] += calls
            c[1] += failures
            c[2] += http_429
        if self._pending[provider][0] >= COUNT_FLUSH_EVERY:
            self.flush_counts(provider)

    def flush_counts(self, provider: str | None = None) -> None:
        """Write the pending counts (of ``provider``, or all) to ``provider_calls_daily``."""
        for name in [provider] if provider else sorted(self._pending):
            calls, failures, http_429 = self._pending.pop(name, [0, 0, 0])
            if calls or failures or http_429:
                ops.count_calls(name, calls, failures=failures, http_429=http_429)

    def check_cap(self, provider: str) -> None:
        """Raise ``CapReached`` when today's calls of ``provider``, written and pending, reached its cap."""
        cap = caps.cap_for(provider)
        if cap is None:
            return
        used = ops.calls_today(provider) + self._pending.get(provider, [0])[0]
        if used >= cap:
            raise CapReached(f"day cap reached: {provider} {used}/{cap}")

    def total_calls(self) -> int:
        return sum(c[0] for c in self.calls.values())

    def issue(self, kind: str, key: str | None, detail: str | None) -> None:
        logger.warning("%s %s %s: %s", self.dataset, kind, key or "", detail or "")
        ops.record_issue(self.dataset, kind, key, detail, run_id=self.run_id)

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True,
                                      headers={"User-Agent": USER_AGENT})
        return self._http

    def uw_client(self):
        """One UW client per run (its retry, 429 handling and breaker), with the header and call observer."""
        if self._uw is None:
            from ...core.providers.unusual_whales import UWClient
            # The client's own backoff sleep (``unusual_whales._sleep``), not the context's: its retry ladder
            # stays exactly what every other UW caller gets.
            self._uw = UWClient()
            self._uw.on_response = self._on_uw_response
        return self._uw

    def _on_uw_response(self, ep: str, response) -> None:
        status = getattr(response, "status_code", 0)
        self.count("uw", calls=1, http_429=int(status == 429))
        if not self.uw_sampled and ops.sample_uw_headers(getattr(response, "headers", None), run_id=self.run_id):
            self.uw_sampled = True

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
        if self._uw is not None:
            health = self._uw.health()
            # Final failures; a call the breaker refused sent nothing, so it is not a provider failure here.
            self.count("uw", failures=max(0, health["failed"] - health["skipped_calls"]))
            self._uw.close()
            self._uw = None


def http_get(ctx: RunContext, provider: str, url: str, params: dict | None = None,
             headers: dict | None = None) -> httpx.Response:
    """GET with the keyless retry ladder; every attempt is counted for ``provider``. Raises ``SourceError``.

    ``params`` may hold a key (FRED): neither the query nor the headers are ever logged.
    """
    last = ""
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        ctx.check_cap(provider)
        try:
            r = ctx.http.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            ctx.count(provider, calls=1)
            last = type(e).__name__
        else:
            ctx.count(provider, calls=1, http_429=int(r.status_code == 429))
            if r.status_code < 400:
                return r
            last = f"HTTP {r.status_code}"
            if not (r.status_code == 429 or r.status_code >= 500):
                break
        if attempt < HTTP_ATTEMPTS:
            ctx.sleep(HTTP_BACKOFF_S * 2 ** (attempt - 1))
    ctx.count(provider, failures=1)
    raise SourceError(f"{provider} {url}: {last}")


# ── Raw files ──────────────────────────────────────────────────────────────────────────────────────────────────

def _publish(tmp: Path, final: Path) -> None:
    os.replace(tmp, final)


def write_raw(path: Path, data: bytes) -> Path:
    """Write a raw file whole or not at all (``.tmp`` then rename); gzip when the name ends in ``.gz``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(gzip.compress(data) if path.suffix == ".gz" else data)
    _publish(tmp, path)
    return path


def read_raw(path: Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".gz" else data


def write_raw_json(path: Path, obj: Any) -> Path:
    return write_raw(path, json.dumps(obj, sort_keys=True).encode("utf-8"))


def read_raw_json(path: Path) -> Any:
    return json.loads(read_raw(path))


# ── Parquet ────────────────────────────────────────────────────────────────────────────────────────────────────

def _copy(con: duckdb.DuckDBPyConnection, select: str, path: Path) -> None:
    """``COPY (select) TO path`` as zstd Parquet. The one place a Parquet file is written."""
    con.execute(f"COPY ({select}) TO '{rawstore._sql_text(path)}' "
                f"(FORMAT parquet, COMPRESSION {PARQUET_COMPRESSION}, ROW_GROUP_SIZE {PARQUET_ROW_GROUP})")


def write_parquet(con: duckdb.DuckDBPyConnection, select: str, final: Path) -> Path:
    """Write ``select`` to ``final`` through ``<final>.tmp`` and a rename, so a partition that exists is whole."""
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(final.name + ".tmp")
    _copy(con, select, tmp)
    _publish(tmp, final)
    return final


def sql_path(path: Path) -> str:
    """A path as a quoted SQL string literal (through rawstore's one escaper)."""
    return f"'{rawstore._sql_text(path)}'"


def rows_table(con: duckdb.DuckDBPyConnection, name: str, schema: list[tuple[str, str]], rows: list[dict]) -> None:
    """Create table ``name`` with ``schema`` in ``con`` (an in-memory connection) and load ``rows`` into it typed."""
    cols = ", ".join(f'"{c}" {t}' for c, t in schema)
    con.execute(f"CREATE OR REPLACE TABLE {name} ({cols})")
    if not rows:
        return
    import pandas as pd
    frame = pd.DataFrame([{c: r.get(c) for c, _ in schema} for r in rows], columns=[c for c, _ in schema])
    con.register("_rows_frame", frame)
    try:
        casts = ", ".join(f'CAST("{c}" AS {t})' for c, t in schema)
        con.execute(f"INSERT INTO {name} SELECT {casts} FROM _rows_frame")
    finally:
        con.unregister("_rows_frame")


def upsert_partition(rows: list[dict], schema: list[tuple[str, str]], key: list[str], final: Path) -> Path:
    """Merge ``rows`` into the Parquet partition ``final`` by ``key``; the newest ``fetched_at`` wins a key."""
    con = duckdb.connect()
    try:
        rows_table(con, "new_rows", schema, rows)
        cols = ", ".join(f'"{c}"' for c, _ in schema)
        source = "SELECT * FROM new_rows"
        if final.exists():
            source = f"SELECT {cols} FROM read_parquet({sql_path(final)}) UNION ALL BY NAME " + source
        keys = ", ".join(f'"{k}"' for k in key)
        select = (f"SELECT {cols} FROM ({source}) QUALIFY row_number() OVER "
                  f"(PARTITION BY {keys} ORDER BY fetched_at DESC) = 1 ORDER BY {keys}")
        return write_parquet(con, select, final)
    finally:
        con.close()


# ── Pacing ─────────────────────────────────────────────────────────────────────────────────────────────────────

class Pacer:
    """At most ``per_minute`` calls in any 60 s window: ``wait()`` before each call sleeps when the window is full."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = time.sleep):
        self.per_minute, self._clock, self._sleep = per_minute, clock, sleep
        self._sent: deque[float] = deque()

    def wait(self) -> None:
        while True:
            now = self._clock()
            while self._sent and now - self._sent[0] >= 60.0:
                self._sent.popleft()
            if len(self._sent) < self.per_minute:
                break
            # At least a millisecond: a sleep that lands a hair short of the window edge must not spin.
            self._sleep(max(60.0 - (now - self._sent[0]), 0.001))
        self._sent.append(now)


# ── Sessions ───────────────────────────────────────────────────────────────────────────────────────────────────

def session_date_of(obs: date) -> date | None:
    """The New York session on or before ``obs`` (``core/calendar.py``); None outside its 2025-2028 table.

    Rule C of the calendar: no second holiday table, so an older observation has no session date here.
    """
    try:
        return calendar.to_session_date(obs)
    except ValueError:
        return None


def next_due(as_of: date) -> str | None:
    try:
        return calendar.next_trading_day(as_of).isoformat()
    except ValueError:
        return None


# ── The dataset protocol ───────────────────────────────────────────────────────────────────────────────────────

class Dataset(Protocol):
    name: str                     # "uw/spot_expiry_strike", "market_series"
    kind: str                     # the serving kind its tables live in
    providers: tuple[str, ...]    # what the day cap is checked against
    tables: tuple[str, ...]       # the serving tables it builds

    def default_as_of(self) -> date: ...
    def fetch(self, ctx: RunContext, as_of: date) -> list[Path]: ...
    def to_parquet(self, ctx: RunContext, raw: Path) -> list[Path]: ...
    def to_parquet_all(self, ctx: RunContext, raws: list[Path]) -> list[Path]: ...
    def backfill(self, ctx: RunContext, start: date, end: date) -> None: ...
    def build(self, ctx: RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]: ...
    def check(self, ctx: RunContext, con: duckdb.DuckDBPyConnection) -> dict: ...


def compare_with_live(con: duckdb.DuckDBPyConnection, kind: str, table: str, columns: list[str]) -> tuple[int, int]:
    """``(added, removed)`` rows of ``table`` in the build file against the live file, on ``columns``.

    The live file is attached read-only for the comparison and detached before returning.
    """
    cols = ", ".join(f'"{c}"' for c in columns)
    live = serving.live_path(kind)
    new_rows = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    if not live.exists():
        return int(new_rows), 0
    con.execute(f"ATTACH {sql_path(live)} AS live_cmp (READ_ONLY)")
    try:
        known = {r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'live_cmp'").fetchall()}
        if table not in known:
            return int(new_rows), 0
        added = con.execute(f'SELECT count(*) FROM (SELECT {cols} FROM "{table}" '
                            f'EXCEPT ALL SELECT {cols} FROM live_cmp."{table}")').fetchone()[0]
        removed = con.execute(f'SELECT count(*) FROM (SELECT {cols} FROM live_cmp."{table}" '
                              f'EXCEPT ALL SELECT {cols} FROM "{table}")').fetchone()[0]
        return int(added), int(removed)
    finally:
        con.execute("DETACH live_cmp")


# ── Gates ──────────────────────────────────────────────────────────────────────────────────────────────────────

def idle_problem() -> str | None:
    """Why the builder must not start now, or None: a job lock held in the store dir, or another live builder.

    The job locks are read with ``job_lock.holders()`` (checks only, never takes one); another builder shows as its
    ``rcdata_build`` lock and as a live ``.rc-proc-<pid>`` marker in the market dir.
    """
    held = job_lock.holders()
    if held:
        return "job lock held: " + ", ".join(f"{name} (pid {pid})" for name, pid in sorted(held))
    peers = rawstore._peer_pids(Path(config.MARKET_DIR) / "ops.sqlite")
    if peers:
        return "another builder runs: pid " + ", ".join(str(p) for p in peers)
    return None


def cap_problem(ds: Dataset) -> str | None:
    """Why the whole dataset is skipped: every one of its providers is at its day cap. One capped provider of
    several (FRED among the market series) is refused call by call instead (``RunContext.check_cap``)."""
    refused = []
    for provider in ds.providers:
        at, used, cap = caps.at_cap(provider)
        if not at:
            return None
        refused.append(f"{provider} {used}/{cap}")
    return "day cap reached: " + ", ".join(refused) if refused else None


# ── One request ────────────────────────────────────────────────────────────────────────────────────────────────

def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]


def kind_datasets(kind: str) -> list[Dataset]:
    from . import DATASETS
    return [d for d in DATASETS.values() if d.kind == kind]


def build_kind(ctx: RunContext, ds: Dataset) -> dict:
    """Build ``ds``'s serving kind from Parquet (every dataset of the kind), check it, swap it in; the check row."""
    if not parquet_root().is_dir():
        raise SourceError(f"the Parquet root {parquet_root()} is missing (RC_MARKET_RAW_DIR unset or not mounted?)")
    with serving.build(ds.kind) as b:
        counts: dict[str, int] = {}
        for d in kind_datasets(ds.kind):
            counts.update(d.build(ctx, b.con))
        check = ds.check(ctx, b.con)
        b.swap(counts, checks=[check], allow_shrink=ctx.allow_shrink)
    return check


def _parse_day(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def run_request(req: dict, *, trigger: str = "queue", sleep: Callable[[float], Any] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> dict:
    """Run one queued request to its end and return the ``dataset_check`` row written for it.

    ``req["params"]``: ``{"mode": "daily", "as_of": "YYYY-MM-DD"}`` (both optional) or
    ``{"mode": "backfill", "from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}``.
    """
    from . import DATASETS
    ds = DATASETS.get(req["dataset"])
    params = req.get("params") or {}
    run_id = new_run_id()
    ctx = RunContext(run_id=run_id, dataset=req["dataset"], trigger=trigger, sleep=sleep, clock=clock,
                     allow_shrink=params.get("allow_shrink") is True)
    ops.start_run(run_id, req["dataset"], trigger, params)
    try:
        return _run(ctx, ds, req, params)
    except BaseException as e:
        # A kill (SIGTERM as KeyboardInterrupt/SystemExit): no check row, but the calls already sent are counted.
        ops.finish_run(run_id, "killed", f"{type(e).__name__}: {e}"[:500])
        raise
    finally:
        ctx.close()
        ctx.flush_counts()


def _run(ctx: RunContext, ds, req: dict, params: dict) -> dict:
    status, error, check = "failed", None, None
    try:
        if ds is None:
            raise ValueError(f"unknown dataset {req['dataset']!r}")
        refused = cap_problem(ds)
        if refused:
            check = {"result": "skipped", "note": refused}
            status = "skipped"
            return _finish(ctx, req, check, status, None)
        mode = params.get("mode", "daily")
        if mode == "backfill":
            start = _parse_day(params["from"])
            end = _parse_day(params["to"]) if params.get("to") else ds.default_as_of()
            ctx.as_of = end
            ds.backfill(ctx, start, end)
        elif mode == "daily":
            ctx.as_of = _parse_day(params["as_of"]) if params.get("as_of") else ds.default_as_of()
            ds.to_parquet_all(ctx, ds.fetch(ctx, ctx.as_of))
        else:
            raise ValueError(f"unknown mode {mode!r}")
        check = build_kind(ctx, ds)
        status = "ok"
    except Exception as e:  # noqa: BLE001 - every failure becomes a failed check row, never a half-swapped file
        logger.exception("%s run %s failed", req["dataset"], ctx.run_id)
        error = f"{type(e).__name__}: {e}"[:500]
        check = {"result": "failed", "note": error}
    return _finish(ctx, req, check, status, error)


def _finish(ctx: RunContext, req: dict, check: dict, status: str, error: str | None) -> dict:
    ctx.close()
    ctx.flush_counts()
    notes = "; ".join([n for n in [check.get("note"), *ctx.notes] if n]) or None
    row = {k: check.get(k) for k in ops.CHECK_FIELDS if k not in ("dataset", "checked_on", "run_id", "note")}
    row["calls"] = ctx.total_calls()
    if row.get("source_as_of") is None and ctx.as_of is not None:
        row["source_as_of"] = ctx.as_of.isoformat()
    if row.get("next_check_due") is None and ctx.as_of is not None:
        row["next_check_due"] = next_due(ctx.as_of)
    result = row.pop("result")
    ops.record_check(ctx.dataset, result, run_id=ctx.run_id, note=notes, **row)
    ops.finish_run(ctx.run_id, status, error)
    if req.get("id") is not None:
        ops.finish_request(req["id"], {"ok": "done", "skipped": "skipped"}.get(status, "failed"), note=notes)
    out = ops.latest_check(ctx.dataset)
    logger.info(format_check(out))
    return out


def format_check(row: dict) -> str:
    """One CLI/log line per dataset run: the ``dataset_check`` fields in order, under 80 columns where it can."""
    parts = [f"{row['dataset']} {row['result']}", f"as_of={row['source_as_of']}", f"rows={row['rows']}",
             f"+{row['added']}/-{row['removed']}", f"calls={row['calls']}"]
    if row.get("note"):
        parts.append(f"note={row['note'][:60]}")
    return " ".join(str(p) for p in parts)


def iter_sessions(start: date, end: date) -> Iterable[date]:
    """Every NYSE session from ``start`` to ``end`` inclusive (``core/calendar.py``)."""
    d = start if calendar.is_trading_day(start) else calendar.next_trading_day(start)
    while d <= end:
        yield d
        d = calendar.next_trading_day(d)
