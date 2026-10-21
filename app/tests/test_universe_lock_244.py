"""#244 (PL6, folded into #243): one universe run at a time.

``run_universe_refresh`` holds the ``universe`` file lock (``core.universe_fetch.universe_write_lock``) for the whole
run, on every path: the dashboard job and the CLI entry point (``app/universe_cli.py``). A second run while the
lock is held makes no network call and no store write; it ends with "another universe run is in progress, pid N"
(a ``JobMessage`` on the dashboard, exit code ``UNIVERSE_BUSY_EXIT`` on the CLI). The single-board Fetches
(AG, Swing, Fundamentals) are refused while a universe run holds the lock: they are checked, not locked, because
the universe run calls the same runners in-process. ``/run/universe`` does not queue a second universe job.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading

import pytest

from RevenueChaser.app import dashboard, jobs, runners, universe_cli
from RevenueChaser.app.tests.test_runners_freeze_128 import freezers, order  # noqa: F401 - fixtures
from RevenueChaser.core import job_control, job_lock, rawstore
from RevenueChaser.core.tests.panel_client import panel_test_client


# dashboard.py's own registrations, captured at import: some test files clear the registry (test_jobs.py,
# test_provider_health.py), and these tests must run the jobs exactly as the dashboard registered them.
_BOARD_JOBS = ("swing", "module_fundamentals", "module_asymmetric_growth", "universe")
_REGISTERED = {name: {k: getattr(jobs, name)[k] for k in _BOARD_JOBS}
               for name in ("_RUNNERS", "_PRODUCES", "_REQUIRES_UW", "_LANE", "_CANCELLABLE")}


@pytest.fixture(autouse=True)
def dashboard_jobs(monkeypatch):
    for name, entries in _REGISTERED.items():
        for key, value in entries.items():
            monkeypatch.setitem(getattr(jobs, name), key, value)


@pytest.fixture
def sync_jobs(monkeypatch):
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)
    monkeypatch.setattr(jobs, "_ensure_long_worker", lambda: None)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_SEQ", {})


@pytest.fixture
def writes(monkeypatch):
    """Every ``write_rows`` table name except the job's own ``job_runs`` bookkeeping row."""
    seen: list[str] = []
    real = rawstore.write_rows

    def spy(name, rows, *a, **k):
        if name != "job_runs":
            seen.append(name)
        return real(name, rows, *a, **k)
    monkeypatch.setattr(rawstore, "write_rows", spy)
    return seen


# A child that holds the lock files it is given the way ``job_lock.acquire`` does: it takes the kernel's OS lock
# (the byte at ``_LOCK_BYTE``) and, unless told ``bare``, writes "<pid> <token> <start>" at offset 0. A file with the
# metadata alone is a crashed holder — the kernel holds no lock — so a faked holder must hold the real OS lock
# (#365). It reports its own pid once every lock is in hand and then waits: the parent closes stdin to stop it, and
# the kernel releases the locks — the same end a crash has. (It reports its pid because on Windows the venv's
# python.exe is a launcher that runs the interpreter as a child, so Popen's pid is not the holder's.)
_HOLDER_SCRIPT = r"""
import os, sys, uuid
from RevenueChaser.core import job_lock

mode, paths = sys.argv[1], sys.argv[2:]
fds = []
for path in paths:
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    if not job_lock._os_lock(fd):
        print(f"busy {path}", flush=True)
        sys.exit(1)
    if mode == "owner":
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()} {uuid.uuid4().hex} {job_lock._our_start()}".rstrip().encode("ascii"))
    fds.append(fd)                                  # kept open: only this process's end releases the locks
print(f"ready {os.getpid()}", flush=True)
sys.stdin.readline()
"""


def _wait_until_ready(proc, timeout: float = 60.0) -> int:
    """The PID of the process holding the locks; a holder that could not take them says so here."""
    line: list[str] = []
    reader = threading.Thread(target=lambda: line.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    word, _, pid = (line[0].strip() if line else "").partition(" ")
    if word == "ready" and pid.isdigit():
        return int(pid)
    proc.kill()
    err = proc.stderr.read() or ""
    raise RuntimeError(f"the lock holder never reported ready ({line or 'no output'}), stderr: {err}")


def _held_by_another_process(*names, owner: bool = True):
    """A live second process holding ``names``' OS locks, as a real holder does (#365).

    It writes the lock metadata ``acquire()`` writes, or with ``owner=False`` leaves the file empty so a held lock
    has an unreadable owner. Yields the holder's PID once it holds every lock; ending the process releases them.
    """
    paths = [job_lock._lock_path(n) for n in names]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(job_lock.__file__)))
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (pkg_root, env.get("PYTHONPATH"))))
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER_SCRIPT, "owner" if owner else "bare",
                             *map(str, paths)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        yield _wait_until_ready(proc)
    finally:
        proc.stdin.close()                          # EOF: the holder exits and the kernel frees its locks
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for path in paths:
            path.unlink(missing_ok=True)


@pytest.fixture
def other_process_holds_the_lock():
    """A universe run in another live process (the CLI run, or a second dashboard): the writer lock and the
    ``universe_run`` marker."""
    yield from _held_by_another_process("universe", "universe_run")


@pytest.fixture
def phase_b_holds_the_lock():
    """Phase B ("Fetch caps", ``backfill_universe_caps``) in another process: the writer lock only."""
    yield from _held_by_another_process("universe")


def _universe_job():
    jobs.register("t_universe_244", runners.run_universe_refresh, [], requires_uw=False, lane="long")
    return jobs.submit("t_universe_244")


def _released() -> bool:
    return "universe" not in dict(job_lock.holders())


# ── a second run does nothing ─────────────────────────────────────────────────

def test_a_second_universe_job_makes_no_fetch_and_no_write(sync_jobs, order, writes, other_process_holds_the_lock):
    jid = _universe_job()
    jobs.execute(jid)
    job = jobs.get(jid)
    assert job["status"] == "done", job
    assert job["message"] == f"another universe run is in progress, pid {other_process_holds_the_lock}"
    assert order == [], "no scan, no fetch, no board write"
    assert writes == []


def test_the_cli_exits_with_its_own_code_when_the_lock_is_held(order, writes, other_process_holds_the_lock,
                                                               capsys):
    assert universe_cli.main([]) == universe_cli.UNIVERSE_BUSY_EXIT
    assert universe_cli.UNIVERSE_BUSY_EXIT not in (0, 1)
    assert f"pid {other_process_holds_the_lock}" in capsys.readouterr().err
    assert order == [] and writes == []


def test_the_cli_runs_the_refresh_when_the_lock_is_free(order, freezers):
    assert universe_cli.main([]) == 0
    assert {"ag.scan", "swing.scan", "fund.scan"} <= set(order)
    assert _released()


def test_the_dashboard_job_is_refused_while_the_cli_runs(sync_jobs, order, freezers, monkeypatch):
    """Both paths contend for one lock name: a CLI run in progress refuses the dashboard job."""
    from RevenueChaser.module_asymmetric_growth import engine as ag_engine
    inside, release = threading.Event(), threading.Event()
    real_scan = ag_engine.run_asymmetric_growth_hunter

    def slow_scan(*a, **k):
        inside.set()
        assert release.wait(5)
        return real_scan(*a, **k)
    monkeypatch.setattr(ag_engine, "run_asymmetric_growth_hunter", slow_scan)
    codes = []
    cli = threading.Thread(target=lambda: codes.append(universe_cli.main([])))
    cli.start()
    try:
        assert inside.wait(5)
        jid = _universe_job()
        jobs.execute(jid)
        assert jobs.get(jid)["message"].startswith("another universe run is in progress, pid ")
    finally:
        release.set()
        cli.join(10)
    assert codes == [0]
    assert order.count("ag.scan") == 1


def test_the_cli_is_refused_while_the_dashboard_job_runs(sync_jobs, order, freezers, monkeypatch):
    from RevenueChaser.module_asymmetric_growth import freeze as ag_freeze
    codes = []
    real = ag_freeze.freeze_result
    # The lock is re-entrant per thread, so the CLI runs on another thread, as it would in its own process.
    monkeypatch.setattr(ag_freeze, "freeze_result", lambda result, **kw: (
        real(result, **kw), _in_thread(lambda: codes.append(universe_cli.main([])))))
    jid = _universe_job()
    jobs.execute(jid)
    assert jobs.get(jid)["status"] == "done"
    assert codes == [universe_cli.UNIVERSE_BUSY_EXIT]


def _in_thread(fn):
    t = threading.Thread(target=fn)
    t.start()
    t.join(10)


# ── the lock is released ──────────────────────────────────────────────────────

def test_the_lock_is_released_after_an_exception(sync_jobs, order, freezers, monkeypatch):
    from RevenueChaser.module_swing_potential import pipeline as sw_pipeline

    def boom(*a, **k):
        raise RuntimeError("swing scan blew up")
    monkeypatch.setattr(sw_pipeline, "run_scan", boom)
    jid = _universe_job()
    jobs.execute(jid)
    assert jobs.get(jid)["status"] == "error"
    assert _released()
    with job_lock.acquire("universe", wait=0) as got:
        assert got is True


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_the_lock_is_released_after_a_cancel_or_a_deadline(sync_jobs, order, freezers, monkeypatch, stop):
    from RevenueChaser.module_asymmetric_growth import freeze as ag_freeze
    real = ag_freeze.freeze_result
    if stop == "cancel":
        hook = lambda: job_control.current().cancel()  # noqa: E731
    else:
        hook = lambda: job_control.current().__setattr__("deadline_s", 0.0)  # noqa: E731
    monkeypatch.setattr(ag_freeze, "freeze_result", lambda result, **kw: (real(result, **kw), hook()))
    jid = _universe_job()
    jobs.execute(jid)
    assert jobs.get(jid)["status"] == ("cancelled" if stop == "cancel" else "error")
    assert _released()


# ── single-board Fetches are refused while a universe run holds the lock ──────

@pytest.mark.parametrize("key", ["swing", "module_fundamentals", "module_asymmetric_growth"])
def test_a_single_board_fetch_is_refused_while_a_universe_run_holds_the_lock(
        sync_jobs, order, writes, other_process_holds_the_lock, key):
    jid = jobs.submit(key)                      # as dashboard.py registers it
    jobs.execute(jid)
    job = jobs.get(jid)
    assert job["status"] == "done", job
    assert f"pid {other_process_holds_the_lock}" in job["message"]
    assert order == [] and writes == []


@pytest.mark.parametrize("key", ["swing", "module_fundamentals", "module_asymmetric_growth"])
def test_a_single_board_refresh_job_is_not_refused(sync_jobs, order, other_process_holds_the_lock, key):
    jid = jobs.submit(key, {"no_fetch": True})
    jobs.execute(jid)
    job = jobs.get(jid)
    assert job["status"] == "done", job
    assert order, "a Refresh reads the store; it is not refused"


@pytest.mark.parametrize("key", ["swing", "module_fundamentals", "module_asymmetric_growth"])
def test_a_single_board_fetch_is_not_refused_while_phase_b_holds_the_writer_lock(
        sync_jobs, order, freezers, phase_b_holds_the_lock, key):
    """Review N2: only a universe run refuses a board Fetch; Phase B ("Fetch caps") did not on main."""
    jid = jobs.submit(key)
    jobs.execute(jid)
    job = jobs.get(jid)
    assert job["status"] == "done", job
    assert not (job["message"] or "").startswith("A universe run is in progress")
    assert order, "the board Fetch ran"


def test_a_universe_run_is_refused_while_phase_b_holds_the_writer_lock(sync_jobs, order, phase_b_holds_the_lock):
    jid = _universe_job()
    jobs.execute(jid)
    # Delta review NB1: the holder is Phase B, not a universe run, and the message says so.
    assert jobs.get(jid)["message"] == (f"the universe data is being written by another job, pid "
                                        f"{phase_b_holds_the_lock}; this universe run did not start")
    assert order == []


@pytest.fixture
def marker_alone_held():
    """Only the ``universe_run`` marker held elsewhere (a crash between the two acquires, or PID reuse)."""
    yield from _held_by_another_process("universe_run")


def test_a_universe_run_is_refused_while_the_run_marker_alone_is_held(sync_jobs, order, writes, marker_alone_held):
    """Delta review D10."""
    jid = _universe_job()
    jobs.execute(jid)
    assert jobs.get(jid)["message"] == f"another universe run is in progress, pid {marker_alone_held}"
    assert order == [] and writes == []


def test_a_universe_run_holds_the_run_marker_only_while_it_runs(sync_jobs, order, freezers, monkeypatch):
    from RevenueChaser.core import universe_fetch
    from RevenueChaser.module_asymmetric_growth import freeze as ag_freeze
    seen = []
    real = ag_freeze.freeze_result
    monkeypatch.setattr(ag_freeze, "freeze_result",
                        lambda result, **kw: (real(result, **kw), seen.append(universe_fetch.universe_run_holder())))
    jid = _universe_job()
    jobs.execute(jid)
    assert jobs.get(jid)["status"] == "done"
    assert seen == [os.getpid()]
    assert universe_fetch.universe_run_holder() is None


def test_a_lock_with_an_unknown_holder_refuses_the_universe_run(sync_jobs, order, writes):
    """Review N3 (R3): a held lock whose owner cannot be read (holder 0) still refuses; nothing is fetched or
    written. The holder is a real process holding the OS lock, with no metadata in the file (#365)."""
    gen = _held_by_another_process("universe", owner=False)
    next(gen)
    try:
        jid = _universe_job()
        jobs.execute(jid)
        assert jobs.get(jid)["message"].startswith("the universe data is being written by another job, pid unknown")
        assert order == [] and writes == []
    finally:
        gen.close()


def test_a_single_board_fetch_runs_when_no_universe_run_holds_the_lock(sync_jobs, order, freezers):
    jid = jobs.submit("swing")
    jobs.execute(jid)
    assert jobs.get(jid)["status"] == "done"
    assert "swing.scan" in order and "swing" in freezers


# ── dedupe ────────────────────────────────────────────────────────────────────

def test_a_duplicate_universe_submit_is_refused_with_the_existing_job_id(sync_jobs):
    client = panel_test_client(dashboard.app)
    first = client.post("/run/universe")
    assert first.status_code == 202
    first_id = first.get_json()["job_id"]
    second = client.post("/run/universe")
    assert second.status_code == 409
    assert second.get_json()["job_id"] == first_id
    assert [j["id"] for j in jobs.list_jobs() if j["module"] == "universe" and j["status"] == "queued"] == [first_id]
    assert jobs.cancel(first_id)
    third = client.post("/run/universe")
    assert third.status_code == 202
    jobs.cancel(third.get_json()["job_id"])


def test_a_universe_submit_is_refused_while_one_is_running(sync_jobs, monkeypatch):
    """Review N3 (R2): the #129 case, a scheduled run while a manual one is going."""
    client = panel_test_client(dashboard.app)
    running = jobs.submit("universe")
    monkeypatch.setitem(jobs._JOBS[running], "status", "running")
    r = client.post("/run/universe")
    assert r.status_code == 409 and r.get_json()["job_id"] == running
    assert [j["id"] for j in jobs.list_jobs() if j["module"] == "universe"] == [running]
