"""Host-level job mutex (file lock beside the raw DB)."""
from __future__ import annotations

import os
import subprocess
import sys
import time

from RevenueChaser.core import job_lock
from RevenueChaser.core.tests.lock_holders import lock_held


def test_acquire_excludes_second_holder(rawstore_tmp_db):
    with job_lock.acquire("edgar", wait=0) as got:
        assert got is True
        with job_lock.acquire("edgar", wait=0) as got2:
            assert got2 is False
    # released on exit
    with job_lock.acquire("edgar", wait=0) as got3:
        assert got3 is True


def test_stale_lock_is_broken(rawstore_tmp_db):
    p = job_lock._lock_path("edgar")
    p.parent.mkdir(parents=True, exist_ok=True)
    # A PID that is certainly dead: a hard-coded one (it was 999) can belong to a
    # live process on a CI runner, and then the lock is rightly kept.
    p.write_text(str(_dead_pid()))
    old = time.time() - 10_000
    os.utime(p, (old, old))
    with job_lock.acquire("edgar", wait=0, stale_after=3600) as got:
        assert got is True


def _live_pid():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    return proc


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_an_old_lock_whose_holder_holds_the_os_lock_is_not_broken(rawstore_tmp_db):
    """A 2-hour crawl keeps its lock: age alone no longer breaks it, and the OS lock is what holds it now (#365)."""
    proc = _live_pid()
    p = job_lock._lock_path("edgar")
    p.write_text(f"{proc.pid} tok {job_lock._our_start()}")
    old = time.time() - 10_000
    os.utime(p, (old, old))
    fd = os.open(p, os.O_RDWR)                 # the holder's OS lock
    assert job_lock._os_lock(fd)
    try:
        with job_lock.acquire("edgar", wait=0, stale_after=3600) as got:
            assert got is False
    finally:
        job_lock._os_unlock(fd)
        os.close(fd)
        proc.kill()
        proc.wait()


def test_a_fresh_lock_of_a_dead_process_is_broken(rawstore_tmp_db):
    p = job_lock._lock_path("edgar")
    p.write_text(f"{_dead_pid()} tok")
    with job_lock.acquire("edgar", wait=0) as got:
        assert got is True


def test_release_never_removes_someone_elses_lock(rawstore_tmp_db):
    p = job_lock._lock_path("edgar")
    with job_lock.acquire("edgar", wait=0) as got:
        assert got
        # our lock was (wrongly) taken over: another holder's owner text is now in the file. It can no longer
        # unlink a held lock file (Windows refuses), so it writes over it — the release check is the same.
        p.write_text("4242 someone-else")
    assert p.read_text() == "4242 someone-else"


def test_a_refused_acquire_leaves_the_holders_lock_intact(rawstore_tmp_db):
    """Nit (#26): the loser of a race must neither break nor unlock the winner's lock, which it still releases."""
    name = "edgar_sync"
    with lock_held(name) as path:
        holder_text = path.read_text(encoding="utf-8")
        with job_lock.acquire(name, wait=0) as got:
            assert got is False
        assert job_lock.holders() == [(name, os.getpid())], "the refused acquirer dropped the holder's lock"
        assert path.read_text(encoding="utf-8") == holder_text, "the refused acquirer wrote to the holder's file"
        assert job_lock._os_lock_is_taken(path), "the holder's OS lock is gone"
    assert job_lock.holders() == [], "the holder could not release its own lock after a refusal"


def test_holders_lists_live_locks_only(rawstore_tmp_db):
    job_lock._lock_path("dead").write_text(f"{_dead_pid()} t")
    with job_lock.acquire("edgar_sync", wait=0):
        assert job_lock.holders() == [("edgar_sync", os.getpid())]
