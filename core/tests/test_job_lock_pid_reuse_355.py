"""#355 (review N4): a reused PID must not make a stale job lock look live.

On Windows a PID comes back quickly, so after a crash or a reboot an unrelated process (or the restarted dashboard
itself) can carry the PID written in a lock file. The lock file now also carries the holder's process start time,
and a lock is live only while its PID runs *and* that process started when the holder did. A lock with our own PID
is live only while its token is one this process issued.
"""
from __future__ import annotations

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


def _write_lock(name: str, text: str) -> None:
    p = job_lock._lock_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _hold_os_lock(name: str) -> int:
    """Take the lock file's OS lock through a second handle, as a live holder does (#365)."""
    fd = os.open(job_lock._lock_path(name), os.O_RDWR)
    assert job_lock._os_lock(fd), "the lock file was expected to be free"
    return fd


def _drop_os_lock(fd: int) -> None:
    job_lock._os_unlock(fd)
    os.close(fd)


# ── our own PID ───────────────────────────────────────────────────────────────

def test_a_lock_with_our_pid_but_a_token_we_never_issued_is_broken(rawstore_tmp_db):
    """The restarted dashboard got the crashed run's PID: the old lock is not ours."""
    _write_lock("universe", f"{os.getpid()} token-of-an-earlier-process")
    with job_lock.acquire("universe", wait=0) as got:
        assert got is True


def test_a_lock_with_our_pid_and_no_token_is_broken(rawstore_tmp_db):
    _write_lock("universe", f"{os.getpid()}")
    with job_lock.acquire("universe", wait=0) as got:
        assert got is True


def test_holders_skips_a_lock_with_our_pid_but_a_foreign_token(rawstore_tmp_db):
    _write_lock("universe_run", f"{os.getpid()} token-of-an-earlier-process")
    assert job_lock.holders() == []


def test_our_own_live_lock_still_excludes_another_thread(rawstore_tmp_db):
    import threading
    seen = []
    with job_lock.acquire("universe", wait=0) as got:
        assert got
        t = threading.Thread(target=lambda: seen.append(job_lock.acquire("universe", wait=0).__enter__()))
        t.start()
        t.join(5)
        assert job_lock.holders() == [("universe", os.getpid())]
    assert seen == [False]


def test_a_released_token_no_longer_counts_as_ours(rawstore_tmp_db):
    p = job_lock._lock_path("universe")
    with job_lock.acquire("universe", wait=0) as got:
        assert got
        text = p.read_text()
    p.write_text(text)                           # the same file comes back after its holder released it
    assert job_lock.holders() == []


# ── another process: the start time ───────────────────────────────────────────

def test_the_lock_file_carries_pid_token_and_start_time(rawstore_tmp_db):
    p = job_lock._lock_path("universe")
    with job_lock.acquire("universe", wait=0):
        pid, token, start = p.read_text().split()
    assert int(pid) == os.getpid()
    assert len(token) == 32
    assert start == procs.process_start_time(os.getpid())


def test_a_live_pid_whose_start_time_differs_is_broken(rawstore_tmp_db, live_proc):
    """The PID runs, but it is a different process than the one that took the lock."""
    _write_lock("universe", f"{live_proc.pid} tok 1")
    assert job_lock.holders() == []
    with job_lock.acquire("universe", wait=0) as got:
        assert got is True


def test_a_live_pid_with_the_same_start_time_keeps_its_lock(rawstore_tmp_db, live_proc):
    _write_lock("universe", f"{live_proc.pid} tok {procs.process_start_time(live_proc.pid)}")
    fd = _hold_os_lock("universe")
    try:
        assert job_lock.holders() == [("universe", live_proc.pid)]
        with job_lock.acquire("universe", wait=0) as got:
            assert got is False
    finally:
        _drop_os_lock(fd)


def test_a_live_pid_whose_start_time_cannot_be_read_is_decided_by_the_os_lock(rawstore_tmp_db, live_proc,
                                                                              monkeypatch):
    """No start time to compare (access denied, an unsupported OS): the holder's OS lock decides, not the start time.

    #364: an unreadable start time must not keep a lock nobody holds; #365: the OS lock keeps a held one.
    """
    _real_start = procs.process_start_time
    monkeypatch.setattr(procs, "process_start_time",
                        lambda pid: None if pid != os.getpid() else _real_start(pid))
    _write_lock("universe", f"{live_proc.pid} tok 1")
    fd = _hold_os_lock("universe")
    try:
        with job_lock.acquire("universe", wait=0) as got:
            assert got is False
    finally:
        _drop_os_lock(fd)
    with job_lock.acquire("universe", wait=0) as got:
        assert got is True, "an unreadable start time held a lock nobody holds (#364)"


def test_an_old_format_lock_of_a_live_pid_keeps_its_lock(rawstore_tmp_db, live_proc):
    """A lock written before #355 has no start time: while its holder holds the OS lock it stays live."""
    _write_lock("universe", f"{live_proc.pid} tok")
    fd = _hold_os_lock("universe")
    try:
        assert job_lock.holders() == [("universe", live_proc.pid)]
        with job_lock.acquire("universe", wait=0) as got:
            assert got is False
    finally:
        _drop_os_lock(fd)


def test_a_lock_that_changed_hands_is_not_broken(rawstore_tmp_db, live_proc):
    """Judged stale and taken over: the new holder's file is its own, and it then keeps everyone else out."""
    _write_lock("universe", f"{live_proc.pid} tok 1")
    path = job_lock._lock_path("universe")
    with job_lock.acquire("universe", wait=0) as got:
        assert got is True
        pid, token, _start = path.read_text().split()
        assert int(pid) == os.getpid() and token != "tok", "the takeover did not write our own owner"
        assert job_lock.holders() == [("universe", os.getpid())]
        with job_lock.acquire("universe", wait=0) as again:
            assert again is False


# ── procs.process_start_time ──────────────────────────────────────────────────

@pytest.mark.skipif(not sys.platform.startswith("linux") and os.name != "nt", reason="no start time on this OS")
def test_process_start_time_is_stable_and_tells_two_processes_apart(live_proc):
    ours = procs.process_start_time(os.getpid())
    assert ours and ours == procs.process_start_time(os.getpid())
    time.sleep(0.05)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(0.05)
        a, b = procs.process_start_time(live_proc.pid), procs.process_start_time(other.pid)
        assert a and b
        assert a != b or os.name != "nt"     # Linux counts in clock ticks; two quick starts can share one
    finally:
        other.kill()
        other.wait()


def test_process_start_time_of_a_gone_pid_is_none():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    if os.name == "nt":
        # On Windows the Popen object still holds the process handle after wait(), so OpenProcess keeps
        # succeeding and the creation time is returned. Close it first, as production does.
        handle = getattr(proc, "_handle", None)
        if handle is not None:
            handle.Close()
    assert procs.process_start_time(proc.pid) is None
    assert procs.process_start_time(0) is None
    assert procs.process_start_time(None) is None


def test_linux_start_time_is_field_22_of_proc_stat(tmp_path, monkeypatch):
    """The command name can hold spaces and parentheses; the fields are counted after the last ')'."""
    stat = tmp_path / "4242" / "stat"
    stat.parent.mkdir()
    fields = ["S"] + [str(n) for n in range(4, 30)]          # fields 3..29; field 22 is "22"
    stat.write_text("4242 (a (weird) name) " + " ".join(fields))
    monkeypatch.setattr(procs, "_PROC_ROOT", str(tmp_path))
    assert procs._start_time_linux(4242) == "22"
    assert procs._start_time_linux(4243) is None


# ── Windows, through a fake kernel32 ──────────────────────────────────────────

class _FakeKernel32:
    """``OpenProcess`` / ``GetProcessTimes`` / ``CloseHandle`` of a fake Windows host."""

    def __init__(self, created: dict[int, int]):
        self.created, self.closed = created, []
        self.OpenProcess = self._open
        self.GetProcessTimes = self._times
        self.CloseHandle = self.closed.append

    def _open(self, access, inherit, pid):
        assert access == procs._PROCESS_QUERY_LIMITED_INFORMATION
        return 1000 + pid if pid in self.created else 0

    def _times(self, handle, creation, exit_, kernel, user):
        value = self.created[handle - 1000]
        creation._obj.dwLowDateTime = value & 0xFFFFFFFF
        creation._obj.dwHighDateTime = value >> 32
        return True


def test_windows_start_time_is_the_creation_filetime():
    k32 = _FakeKernel32({4242: 0x01DB_1234_5678_9ABC})
    assert procs._start_time_windows(4242, k32) == str(0x01DB_1234_5678_9ABC)
    assert k32.closed == [1000 + 4242], "the process handle is closed"


def test_windows_start_time_is_none_when_the_process_cannot_be_opened():
    """Gone or access denied: nothing to compare, the PID alone decides."""
    k32 = _FakeKernel32({4242: 1})
    assert procs._start_time_windows(4243, k32) is None
    assert k32.closed == []


def test_a_fresh_lock_is_never_judged_a_leftover_by_another_thread(rawstore_tmp_db, monkeypatch):
    """Review B1: right after our lock file gets its content, another thread of ours tries the same lock. The token is
    registered before the file exists, so the file never shows our PID with a token we do not hold."""
    real_write = os.write
    out: dict = {}

    def other():
        with job_lock.acquire("universe", wait=0) as got:
            out["other_got"] = got

    def write_then_let_another_thread_try(fd, data):
        n = real_write(fd, data)
        if "started" not in out and data.startswith(f"{os.getpid()} ".encode()):
            out["started"] = True
            t = threading.Thread(target=other)
            t.start()
            t.join(5)
        return n
    monkeypatch.setattr(os, "write", write_then_let_another_thread_try)
    with job_lock.acquire("universe", wait=0) as got:
        assert got
    assert out.get("started")
    assert out["other_got"] is False, "another thread broke our live lock"
