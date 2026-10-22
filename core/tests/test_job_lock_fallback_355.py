"""#355: the pre-OS-lock fallback path is tested again.

The batch that made the kernel own a job lock's liveness (#365) rewrote six PID-liveness tests in
``test_job_lock.py`` and ``test_job_lock_pid_reuse_355.py`` to hold the OS lock, so nothing exercised the older
fallback any more: the lock file's PID plus ``procs.process_start_time``, with ``_break_if_unchanged`` guarding the
takeover. That path is the whole rule on a host with neither ``fcntl`` nor ``msvcrt`` (``_OS_LOCK_AVAILABLE`` is
False), so every test here monkeypatches it off and replays the scenario of the ORIGINAL version of those tests
(``git show 6250a89:core/tests/test_job_lock.py``,
``git show 6250a89:core/tests/test_job_lock_pid_reuse_355.py``):

- a live PID with the same start time keeps its lock,
- a reused PID (another process now has it) is stale and taken over,
- an old-format lock with no start time keeps its lock for a live PID,
- a lock that changed hands between the stale judgement and the takeover is NOT broken,
- a dead PID is taken over at once,
- a second acquire of a held lock gets ``got=False``.

Synthetic locks in the test store only, no network.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from RevenueChaser.core import job_lock, procs


@pytest.fixture(autouse=True)
def _no_os_lock(monkeypatch):
    """Run every test here on the fallback path, whatever this host supports."""
    monkeypatch.setattr(job_lock, "_OS_LOCK_AVAILABLE", False)


@pytest.fixture
def live_proc():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _write_lock(name: str, text: str) -> None:
    p = job_lock._lock_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# ── another process: the start time decides ───────────────────────────────────

def test_a_live_pid_with_the_same_start_time_keeps_its_lock(rawstore_tmp_db, live_proc):
    _write_lock("universe", f"{live_proc.pid} tok {procs.process_start_time(live_proc.pid)}")
    assert job_lock.holders() == [("universe", live_proc.pid)]
    with job_lock.acquire("universe", wait=0) as got:
        assert got is False


def test_a_reused_pid_is_stale_and_taken_over(rawstore_tmp_db, live_proc):
    """The PID runs, but it is a different process than the one that took the lock (#355)."""
    _write_lock("universe", f"{live_proc.pid} tok 1")
    assert job_lock.holders() == []
    with job_lock.acquire("universe", wait=0) as got:
        assert got is True
        assert job_lock._lock_path("universe").read_text().split()[1] != "tok", "the takeover wrote its own owner"


def test_an_old_format_lock_of_a_live_pid_keeps_its_lock(rawstore_tmp_db, live_proc):
    """A lock written before #355 has no start time: the PID alone decides."""
    _write_lock("universe", f"{live_proc.pid} tok")
    assert job_lock.holders() == [("universe", live_proc.pid)]
    with job_lock.acquire("universe", wait=0) as got:
        assert got is False


def test_a_lock_that_changed_hands_is_not_broken(rawstore_tmp_db, live_proc, monkeypatch):
    """Judged stale, then taken over before the break: the new holder's file stays (_break_if_unchanged)."""
    _write_lock("universe", f"{live_proc.pid} tok 1")
    real = job_lock._is_stale
    fresh = f"{live_proc.pid} tok2 {procs.process_start_time(live_proc.pid)}"

    def stale_then_swapped(path, stale_after):
        out = real(path, stale_after)
        path.write_text(fresh)
        return out

    monkeypatch.setattr(job_lock, "_is_stale", stale_then_swapped)
    with job_lock.acquire("universe", wait=0) as got:
        assert got is False
    assert job_lock._lock_path("universe").read_text() == fresh


# ── a lock nobody holds ───────────────────────────────────────────────────────

def test_a_dead_pid_is_taken_over_at_once(rawstore_tmp_db):
    _write_lock("edgar", f"{_dead_pid()} tok")
    with job_lock.acquire("edgar", wait=0) as got:
        assert got is True


def test_a_second_acquire_of_a_held_lock_gets_false(rawstore_tmp_db):
    with job_lock.acquire("edgar", wait=0) as got:
        assert got is True
        with job_lock.acquire("edgar", wait=0) as got2:
            assert got2 is False
    with job_lock.acquire("edgar", wait=0) as got3:      # released on exit
        assert got3 is True


def test_the_fallback_path_is_the_one_these_tests_run():
    """Guard: if the autouse monkeypatch were dropped, the tests above would silently re-test the OS-lock path."""
    assert job_lock._OS_LOCK_AVAILABLE is False


# ── release: the fallback still removes its file ──────────────────────────────

def test_the_fallback_release_removes_its_lock_file(rawstore_tmp_db):
    """The OS-lock path leaves its file (#365), but this path must not: it takes the file with O_CREAT|O_EXCL,
    so a leftover would block every acquirer."""
    path = job_lock._lock_path("universe")
    with job_lock.acquire("universe", wait=0) as got:
        assert got is True
        assert path.exists()
    assert not path.exists(), "the fallback path left its O_CREAT|O_EXCL lock file behind"
    with job_lock.acquire("universe", wait=0) as got:
        assert got is True


def test_the_fallback_release_never_removes_someone_elses_lock_file(rawstore_tmp_db):
    """Kept on this path too: the token check is what stops a broken holder deleting the taker's file."""
    path = job_lock._lock_path("universe")
    with job_lock.acquire("universe", wait=0) as got:
        assert got
        path.write_text("4242 someone-elses-token 1")     # our lock was taken over
    assert path.read_text() == "4242 someone-elses-token 1"
