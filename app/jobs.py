"""In-process job queue for the dashboard — sequential, non-blocking module runs.

Two lanes, one worker each: the **short** lane (every ordinary Fetch) and the
**long** lane (multi-minute to multi-hour jobs: 13F ingest, universe refresh,
EDGAR 10-K sync). Within a lane jobs run one at a time — no UW rate-limit
collisions — while a two-hour ingest can no longer hold every other Fetch
hostage. Jobs are tracked in an in-memory registry with a lock; statuses are
``queued -> running -> done|error|cancelled``, or ``queued -> cancelled``.

Cancel and deadline (#243, PL4) are cooperative. A queued job is cancelled at once. A running job gets its stop
token set (``core/job_control.py``) and ends ``cancelled`` at the runner's next checkpoint (a phase boundary, or the
next ticker in a scan pool). A job may carry a deadline (``register(deadline_s=...)``, overridable per ``submit``;
none by default); past it, the next checkpoint ends the job ``error`` with a message naming the deadline and the
phase. Nothing is interrupted mid-write, so the lane takes its next job once the runner has stopped.

The in-memory registry is cleared on server restart. Each job also leaves one ``job_runs`` row in the
store (#282, J4): written when the job starts running and again when it ends, keyed by the job id.
"""
import itertools
import logging
import os
import queue
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from RevenueChaser.core import job_control, ledger, rawstore
from RevenueChaser.core.providers.unusual_whales import BREAKER_THRESHOLD, UWClient

logger = logging.getLogger(__name__)

MAX_HISTORY = 50

_JOB_QUEUE = queue.Queue()
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
_RUNNERS: dict[str, Callable] = {}
_PRODUCES: dict[str, list[str]] = {}
_REQUIRES_UW: dict[str, bool] = {}
_LANE: dict[str, str] = {}
_DEADLINE: dict[str, Optional[float]] = {}
_CANCELLABLE: dict[str, bool] = {}
_TOKENS: dict[str, job_control.StopToken] = {}   # the stop token of each running job
_WORKER_STARTED = False          # short-lane worker (name kept for callers/tests)
_LONG_QUEUE = queue.Queue()
_LONG_STARTED = False
LANES = ("short", "long")
_CURRENT = threading.local()     # the job this worker thread is executing, if any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobMessage(Exception):
    """A non-error completion signal — the job finishes "done" with an info message."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def register(module: str, runner: Callable, produces: list[str], requires_uw: bool = True,
             lane: str = "short", deadline_s: Optional[float] = None, cancellable: bool = False) -> None:
    """Register a module runner. ``runner(uw, **params)`` returns a Path/None.

    ``requires_uw=False`` marks a module that does not need the Unusual Whales
    client; its runner receives ``uw=None`` and is not gated on an API key.
    ``lane="long"`` puts it on the long-jobs worker. ``deadline_s`` is the
    job's default deadline in seconds from its start (``None``: no deadline).
    ``cancellable=True`` marks a runner that reaches ``core.job_control``
    checkpoints, so it can stop early: only such a job accepts a cancel (or
    honours a deadline) while it runs. Any queued job can be cancelled.
    """
    if lane not in LANES:
        raise ValueError(f"unknown lane {lane!r}")
    if deadline_s is not None and not cancellable:
        raise ValueError(f"{module}: a deadline needs a runner with checkpoints (cancellable=True)")
    _RUNNERS[module] = runner
    _PRODUCES[module] = produces
    _REQUIRES_UW[module] = requires_uw
    _LANE[module] = lane
    _DEADLINE[module] = deadline_s
    _CANCELLABLE[module] = cancellable


def current_job_id() -> Optional[str]:
    """Id of the job running on this thread, ``None`` outside a job.

    The forecast ledger stamps it as ``run_id``; the job's ``job_runs`` row carries the same id.
    """
    return getattr(_CURRENT, "job_id", None)


def _change_writer():
    """(job id, module) of the job on this worker thread, or None outside a job (the store's change capture)."""
    job_id = current_job_id()
    if not job_id:
        return None
    with _LOCK:
        job = _JOBS.get(job_id)
    return job_id, (job or {}).get("module")


rawstore.set_change_writer(_change_writer)


def run_id() -> str:
    """The ``run_id`` a writer stamps on its rows: the running job's id, else a fresh one.

    Inside a job it equals ``job_runs.run_id``, so a history row joins to the job that wrote it.
    The id is per thread: call it once on the runner thread and pass it down. A pool thread started by a runner
    gets a fresh, unrelated id.
    """
    return current_job_id() or ledger.new_run_id()


def active_id(modules: tuple[str, ...]) -> Optional[str]:
    """Id of a queued or running job of one of ``modules``, else ``None``."""
    with _LOCK:
        for j in _JOBS.values():
            if j.get("module") in modules and j.get("status") in ("queued", "running"):
                return j["id"]
    return None


def has_active(modules: tuple[str, ...]) -> bool:
    """True while a job of one of ``modules`` is queued or running."""
    return active_id(modules) is not None


def _view(job: dict) -> dict:
    """A copy of a job for callers; a running job also shows the phase it reached (caller holds _LOCK)."""
    out = dict(job)
    token = _TOKENS.get(job["id"])
    if token is not None:
        out["phase"] = token.phase
    return out


def _snapshot() -> list[dict]:
    with _LOCK:
        return [_view(j) for j in _JOBS.values()]


def submit(module: str, params: dict = None, deadline_s: Optional[float] = None) -> str:
    """Enqueue a job; returns its id. Status becomes 'queued'.

    ``deadline_s`` overrides the module's registered deadline for this job; only a cancellable module takes one.
    """
    if deadline_s is not None and not _CANCELLABLE.get(module, False):
        raise ValueError(f"{module}: a deadline needs a runner with checkpoints (cancellable=True)")
    global _WORKER_STARTED
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "module": module,
        "params": params or {},
        "status": "queued",
        "produces": _PRODUCES.get(module, []),
        "queued_at": _utc_now(),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "message": None,
        "notes": [],
        "lane": _LANE.get(module, "short"),
        "deadline_s": deadline_s if deadline_s is not None else _DEADLINE.get(module),
        "deadline_at": None,
        "cancellable": _CANCELLABLE.get(module, False),
        "cancel_requested": False,
        "phase": None,
    }
    with _LOCK:
        _JOBS[job_id] = job
        _SEQ[job_id] = next(_COUNTER)
        _trim_history()
    if job["lane"] == "long":
        _LONG_QUEUE.put(job_id)
        _ensure_long_worker()
    else:
        _JOB_QUEUE.put(job_id)
        _ensure_worker()
    return job_id


def cancel(job_id: str) -> bool:
    """Cancel a queued or running job; True when the cancel was accepted.

    A queued job ends ``cancelled`` now; the worker skips it when it reaches it. A running job is not
    interrupted (runners hold store transactions and partial files): when it was registered ``cancellable``,
    its stop token is set and it ends ``cancelled`` at its next checkpoint (``core/job_control.py``); any
    other running job refuses. A finished job cannot be cancelled.
    """
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return False
        if job["status"] == "running":
            token = _TOKENS.get(job_id)
            if token is None or not job.get("cancellable"):
                return False
            token.cancel()
            job["cancel_requested"] = True
            return True
        if job["status"] != "queued":
            return False
        job.update(status="cancelled", finished_at=_utc_now())
        snapshot = dict(job)
    _record(snapshot)   # outside the lock: a store write can wait seconds
    return True


def _trim_history() -> None:
    """Keep at most MAX_HISTORY *finished* jobs (caller holds _LOCK).

    Queued and running jobs are never evicted: dropping one made the worker skip
    it silently and the UI poll a 404. Eviction order is submission order (a
    counter), not ``queued_at`` — that has one-second resolution.
    """
    finished = [k for k, j in _JOBS.items() if j["status"] in ("done", "error", "cancelled")]
    excess = len(_JOBS) - MAX_HISTORY
    for k in sorted(finished, key=lambda k: _SEQ.get(k, 0))[:max(0, excess)]:
        _JOBS.pop(k, None)
        _SEQ.pop(k, None)


_SEQ: dict[str, int] = {}
_COUNTER = itertools.count()
_WORKER_LOCK = threading.Lock()


def _ensure_worker() -> None:
    global _WORKER_STARTED
    with _WORKER_LOCK:  # two concurrent first submits must not start two workers
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True
    t = threading.Thread(target=_worker_loop, daemon=True, name="rc-jobs")
    t.start()


def _ensure_long_worker() -> None:
    global _LONG_STARTED
    with _WORKER_LOCK:
        if _LONG_STARTED:
            return
        _LONG_STARTED = True
    t = threading.Thread(target=_worker_loop, args=(_LONG_QUEUE,), daemon=True, name="rc-jobs-long")
    t.start()


def _epoch(iso: Optional[str]) -> Optional[float]:
    return datetime.fromisoformat(iso).timestamp() if iso else None


def _record(job: dict) -> None:
    """Upsert this job's ``job_runs`` row from a job dict (#282). Never raises: a failed write must not fail the job.

    The registry keeps times as ISO strings (second resolution); the table stores epoch seconds. The write waits
    only briefly for a store another process holds (``rawstore.brief_wait``); it is then skipped and logged, which
    leaves the previous row (``running`` after a skipped end write) in place. ``params`` goes in as a dict:
    ``write_rows`` encodes JSON columns itself. This module is the only writer of the row.
    """
    try:
        with rawstore.brief_wait():
            rawstore.write_rows("job_runs", [{
                "run_id": job["id"], "job_id": job["id"], "module": job["module"], "lane": job["lane"],
                "pid": os.getpid(), "params": job["params"],
                "queued_at": _epoch(job["queued_at"]), "started_at": _epoch(job["started_at"]),
                "finished_at": _epoch(job["finished_at"]), "status": job["status"],
                "error": job["error"], "message": job["message"],
            }])
    except Exception:  # noqa: BLE001 - the run log is bookkeeping, never a reason to fail a job
        logger.warning("job_runs: could not record job %s", job.get("id"), exc_info=True)


def _set(job_id: str, **fields) -> None:
    """Update a job. ``running`` is published first (``execute`` claims a job with it); an end status is stored
    in ``job_runs`` *before* the registry shows it: a poller that sees "done" may open the store at once, and the
    dashboard's writer must have let go of it by then."""
    status = fields.get("status")
    if status in ("done", "error", "cancelled"):
        job = get(job_id)
        if job is not None:
            _record({**job, **fields})
    with _LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(fields)
            snapshot = dict(_JOBS[job_id])
        else:
            snapshot = None
    if status == "running" and snapshot is not None:
        _record(snapshot)


def _gap_fields(uw) -> dict:
    """Job fields for a run whose provider calls partly failed (PL1 / ADR-1).

    The job still finishes "done" — the board has whatever did arrive — but the
    message (shown by the dashboard like any job message) says the board may be
    incomplete, so an outage cannot pass for a quiet market. ``{}`` when the
    client recorded no failures, or has no ``health()`` (test doubles).
    """
    health_fn = getattr(uw, "health", None)
    if not callable(health_fn):
        return {}
    try:
        health = health_fn() or {}
    except Exception:  # noqa: BLE001 - reporting must never fail the job
        return {}
    failed = int(health.get("failed") or 0)
    if failed <= 0:
        return {}
    example = ""
    first = (health.get("failures") or [None])[0]
    if first:
        why = first.get("status") or first.get("error") or "error"
        example = f" (e.g. {first.get('endpoint', '')}: {why})"
    calls = int(health.get("calls") or failed)
    stopped = ""
    if health.get("breaker_open"):
        skipped = int(health.get("skipped_calls") or 0)
        stopped = (f" The client stopped sending requests after {BREAKER_THRESHOLD} failures in a row"
                   f" ({skipped} calls were never sent).")
    message = (f"Finished with gaps: {failed} of {calls} Unusual Whales calls failed{example}.{stopped} "
               "The board may be incomplete — run Fetch again later.")
    return {"message": message, "provider_health": health}


def _with_freeze_notes(fields: dict) -> dict:
    """Put this job's forecast-freeze notes (``core.ledger``) in the job's own ``notes`` list.

    A freeze reports through notes, not through ``JobMessage``: ``run_universe_refresh`` runs three
    boards in a row and an exception from the first would stop the others.

    Not in ``message``: the dashboard opens a modal ``alert()`` for every finished job that has one
    (``templates/dashboard.html``, ``poll``), so a "not frozen" sentence would pop up after every Fetch of
    Swing, Fundamentals, AG and the universe, which have no message on main. ``notes`` is served by
    ``/api/jobs`` and each note is also a ``forecast ledger:`` line in the Server log (Logs tab).
    """
    notes = ledger.take_notes()
    return {**fields, "notes": notes} if notes else fields


def execute(job_id: str) -> None:
    """Run one queued job to completion, recording its outcome.

    Split out of ``_worker_loop`` so a caller can drive a job synchronously —
    the end-to-end harness needs a deterministic "run this and tell me how it
    went" without racing a background thread. Behaviour is unchanged; the loop
    below is now just "take an id, execute it".
    """
    with _LOCK:
        job = _JOBS.get(job_id)
        job = dict(job) if job else None
    if job is None or job["status"] != "queued":
        return  # cancelled (or unknown) before it started
    runner = _RUNNERS.get(job["module"])
    if runner is None:
        _set(job_id, status="error", error=f"unknown module {job['module']}",
             started_at=_utc_now(), finished_at=_utc_now())
        return
    token = job_control.StopToken(job.get("deadline_s"))
    token.phase = job["module"]   # a runner with phases (the universe run) names its own as it enters them
    started_at = _utc_now()
    deadline_at = None
    if token.deadline_s is not None:
        deadline_at = datetime.fromtimestamp(_epoch(started_at) + token.deadline_s, timezone.utc)
        deadline_at = deadline_at.isoformat(timespec="seconds")
    with _LOCK:
        _TOKENS[job_id] = token   # before "running": a cancel that sees "running" must find the token
    _set(job_id, status="running", started_at=started_at, deadline_at=deadline_at)
    ledger.take_notes()           # a note a previous job left on this worker thread must not leak in
    _CURRENT.job_id = job_id
    uw = None
    outer_token = job_control.bind(token)
    try:
        # Only UW jobs get a client: building one costs ~0.4 s (a fresh HTTPS
        # pool), and its key check is cached across the submit-time probe.
        if _REQUIRES_UW.get(job["module"], True):
            uw = UWClient()
            ok, message = uw.verify()
            if not ok:
                _set(job_id, status="error", error=message, finished_at=_utc_now())
                return
        runner(uw, **job["params"])
        fields = _with_freeze_notes(_gap_fields(uw))
        if token.cancel_requested:  # the cancel came after the runner's last checkpoint: it ran to the end
            fields = {**fields, "notes": fields.get("notes", []) + [
                "The cancel came after the last safe stopping point; the run finished."]}
        _set(job_id, status="done", finished_at=_utc_now(), **fields)
    except JobMessage as exc:
        gap = _gap_fields(uw)
        if gap:
            gap["message"] = f"{exc.message} {gap['message']}"
        _set(job_id, status="done", finished_at=_utc_now(),
             **_with_freeze_notes(gap or {"message": exc.message}))
    except job_control.JobStopped as exc:
        # Stopped at a checkpoint: a freeze that already ran stays visible, as for an error below.
        fields = _with_freeze_notes({})
        if isinstance(exc, job_control.JobCancelled):
            logger.info("job %s cancelled: %s", job_id, exc.message)
            _set(job_id, status="cancelled", message=exc.message, finished_at=_utc_now(), **fields)
        else:
            logger.warning("job %s stopped: %s", job_id, exc.message)
            error = exc.message
            if fields:
                error = f"{error} (before the stop: {' '.join(fields['notes'])})"
            _set(job_id, status="error", error=error, finished_at=_utc_now(), **fields)
    except Exception as exc:  # noqa: BLE001 — job boundary
        logger.exception(f"job {job_id} failed")
        # A freeze that already happened ("Froze 4,251 AG rows...") before a later step raised stays visible.
        # The dashboard shows a failed job's ``error`` and nothing else, so the notes go there as well.
        fields = _with_freeze_notes({})
        error = str(exc)
        if fields:
            error = f"{error} (before the error: {' '.join(fields['notes'])})".strip()
        _set(job_id, status="error", error=error, finished_at=_utc_now(), **fields)
    finally:
        job_control.bind(outer_token)
        with _LOCK:
            _TOKENS.pop(job_id, None)
            if job_id in _JOBS:
                _JOBS[job_id]["phase"] = token.phase   # the phase it reached stays in the job JSON
        _CURRENT.job_id = None
        if uw is not None:
            getattr(uw, "close", lambda: None)()


def _worker_loop(q: "queue.Queue | None" = None) -> None:
    q = q if q is not None else _JOB_QUEUE
    while True:
        job_id = q.get()
        try:
            execute(job_id)
        finally:
            q.task_done()


def get(job_id: str) -> Optional[dict]:
    with _LOCK:
        j = _JOBS.get(job_id)
        return _view(j) if j else None


def list_jobs() -> list[dict]:
    rows = _snapshot()
    rows.sort(key=lambda r: r.get("queued_at", ""))
    return rows
