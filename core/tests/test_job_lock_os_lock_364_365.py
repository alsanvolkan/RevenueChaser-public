"""#365 and #364: the OS file lock owns job-lock liveness.

#365: ``_break_if_unchanged`` compared the lock file and *then* unlinked it, so two acquirers that had both judged
one lock stale could both end up holding it. #364: liveness came from the PID plus ``procs.process_start_time``, so
a PID whose start time could not be read kept the lock held for ever.

The kernel now owns liveness: a lock is held while another handle holds its byte, an acquirer that wins the byte
then rewrites the file, and nothing is ever unlinked to take a lock over. The PID rules stay, but only as a veto
on a *positively* identified live owner.
"""
from __future__ import annotations

import itertools
import os
import subprocess
import sys
import threading
import time

import pytest

from RevenueChaser.core import job_lock, procs


@pytest.fixture
def live_proc():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def _live_pid() -> int:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]).pid


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _write(name: str, text: str):
    p = job_lock._lock_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ── #365: the takeover is one atomic step ─────────────────────────────────────

@pytest.mark.skipif(not getattr(job_lock, "_OS_LOCK_AVAILABLE", True),
                    reason="needs the OS lock (the file-only fallback keeps the old takeover)")
def test_two_acquirers_of_one_stale_lock_leave_exactly_one_holder(rawstore_tmp_db, monkeypatch):
    """R1: the first acquirer has judged the stale lock and read its bytes, then pauses before acting on them.

    That pause is the window #365 names: the old ``_break_if_unchanged`` decided on the bytes it had read and
    unlinked whatever was there afterwards, so the second acquirer's fresh lock was removed and both held.
    """
    p = _write("edgar", f"{_dead_pid()} stale-token")
    judged, let_go = threading.Event(), threading.Event()
    real = job_lock._read_owner
    calls = itertools.count(1)

    def slow_owner(path):
        value = real(path)                        # the bytes this acquirer decides on ...
        if next(calls) == 3 and threading.current_thread().name == "first":
            judged.set()                          # ... are in hand, and it pauses before it acts on them
            assert let_go.wait(10)
        return value
    monkeypatch.setattr(job_lock, "_read_owner", slow_owner)

    held: dict[str, bool] = {}

    def acquirer(key: str) -> None:
        with job_lock.acquire("edgar", wait=0) as got:
            held[key] = got
            if got:
                time.sleep(0.5)                   # keep it while the other acquirer finishes its attempt

    first = threading.Thread(target=acquirer, args=("first",), name="first")
    first.start()
    assert judged.wait(10), "the first acquirer never judged the lock"
    second = threading.Thread(target=acquirer, args=("second",), name="second")
    second.start()
    second.join(10)
    let_go.set()
    first.join(10)

    assert sorted(held.values()) == [False, True], f"both acquirers hold the lock: {held}"


# ── #364: an unreadable start time no longer holds the lock ───────────────────

def test_a_pid_whose_start_time_cannot_be_read_does_not_keep_the_lock(rawstore_tmp_db, live_proc, monkeypatch):
    """R2: the PID runs, but nothing can vouch for *when* it started, and nobody holds the OS lock."""
    monkeypatch.setattr(procs, "pid_alive", lambda pid: True)
    monkeypatch.setattr(procs, "process_start_time", lambda pid: None)
    _write("edgar", f"{live_proc.pid} tok 1")
    with job_lock.acquire("edgar", wait=0) as got:
        assert got is True


def test_a_crashed_holder_is_taken_at_once(rawstore_tmp_db):
    """R3: a run killed between creating the file and writing its owner leaves nothing that names a holder."""
    p = _write("edgar", "")
    old = time.time() - 10_000
    os.utime(p, (old, old))
    with job_lock.acquire("edgar", wait=0, stale_after=0) as got:
        assert got is True                       # today: no owner to read, so the age rule (0) keeps it held
        assert job_lock._os_held(p) is True      # and the byte really is held, not merely believed held
        assert job_lock._read_owner(p)[0] == os.getpid()


# ── the Windows byte range never covers the metadata ─────────────────────────

def test_the_locked_byte_never_covers_the_metadata(rawstore_tmp_db):
    """R4: ``msvcrt.locking`` is mandatory, so the metadata at offset 0 must stay readable while the lock is held."""
    p = job_lock._lock_path("edgar")
    with job_lock.acquire("edgar", wait=0) as got:
        assert got
        pid, token, _start = job_lock._read_owner(p)     # a second open handle, the way another process reads it
        assert pid == os.getpid()
        assert len(token) == 32
        other = os.open(p, os.O_RDWR)
        try:
            assert job_lock._os_lock(other) is False     # the byte is held, and it is not the metadata's byte
        finally:
            os.close(other)


def test_holders_lists_only_the_live_lock_and_leaves_both_files_alone(rawstore_tmp_db, monkeypatch):
    """R5: a read-only probe. It lists the live lock, changes neither file, and takes nothing over.

    The stale one is the #364 shape: its PID runs, its start time cannot be read, and nobody holds the OS lock.
    """
    monkeypatch.setattr(procs, "pid_alive", lambda pid: True)
    monkeypatch.setattr(procs, "process_start_time", lambda pid: None)
    stale = _write("dead", f"{_live_pid()} cannot-be-vouched-for 1")
    stale_before = stale.read_bytes()
    with job_lock.acquire("edgar_sync", wait=0) as got:
        assert got
        live = job_lock._lock_path("edgar_sync")
        live_before = live.read_bytes()
        assert job_lock.holders() == [("edgar_sync", os.getpid())]
        assert stale.read_bytes() == stale_before
        assert live.read_bytes() == live_before
    with job_lock.acquire("dead", wait=0) as got2:      # the probe left no lock behind on the stale file
        assert got2 is True
