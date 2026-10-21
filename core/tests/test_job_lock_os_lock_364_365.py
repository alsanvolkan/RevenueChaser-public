"""#365 / #364: the kernel owns a job lock's liveness, not the PID in the file.

#365: the old takeover read the lock file, judged it stale and then unlinked it, so two acquirers that both read the
same abandoned file could both remove it — the second removing the first one's fresh lock — and both end up holding
it. #364: liveness was the PID plus ``procs.process_start_time``, so a lock file whose start time cannot be read kept
its lock forever. ``acquire()`` now takes a non-blocking OS lock on the file (``fcntl.flock``, ``msvcrt.locking``):
a dead holder's lock is released by the kernel, and a takeover is a single atomic step.

The lock file keeps ``"<pid> <token> <start>"`` at offset 0; on Windows the OS lock is a mandatory byte range, so it
covers the single byte at ``_LOCK_BYTE`` and never the metadata another process has to read.
"""
from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from RevenueChaser.core import job_lock, procs


def _live_pid():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    return proc


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _hold_as_a_holder_does(path) -> int:
    """Take ``path``'s OS lock through a second handle, as a live holder would; returns the fd to release."""
    fd = os.open(path, os.O_RDWR)
    assert job_lock._os_lock(fd) is True, "the file was expected to be free"
    return fd


def _release(fd) -> None:
    job_lock._os_unlock(fd)
    os.close(fd)


# ── R1 (#365): two acquirers of one stale lock ────────────────────────────────

# A child that takes the "edgar" lock exactly as production does, and reports whether it got it. With the pause
# switch it first reproduces the old takeover's window: it reads the abandoned owner, then waits for the ``go`` file
# before returning that value to the comparison inside ``_break_if_unchanged``, so the unlink that follows lands
# whenever the other acquirer has meanwhile created its lock file.
_ACQUIRE_SCRIPT = r"""
import os, sys, time
from pathlib import Path
from RevenueChaser.core import job_lock

lock_path, ready, judged, go, result, pause = sys.argv[1:7]
job_lock._lock_path = lambda name: Path(lock_path)

if pause == "pause":
    real = job_lock._read_owner
    reads = [0]

    def read_owner_then_pause(p):
        owner = real(p)
        if owner[0] is not None:
            reads[0] += 1
            if reads[0] == 3:                      # the read inside _break_if_unchanged, just before its unlink
                Path(judged).write_text("judged")
                deadline = time.time() + 30
                while time.time() < deadline and not os.path.exists(go):
                    time.sleep(0.02)
        return owner

    job_lock._read_owner = read_owner_then_pause

Path(ready).write_text("ready")
with job_lock.acquire("edgar", wait=0) as got:
    Path(result).write_text("1" if got else "0")
    if pause == "pause":                           # the paused acquirer holds until the other one has finished
        deadline = time.time() + 30
        while time.time() < deadline and not os.path.exists(go):
            time.sleep(0.02)
"""


def _acquire_in_a_child(lock_path, ready, judged, go, result, pause: bool):
    return subprocess.Popen(
        [sys.executable, "-c", _ACQUIRE_SCRIPT, str(lock_path), str(ready), str(judged), str(go), str(result),
         "pause" if pause else "plain"],
        env=dict(os.environ))


def test_two_acquirers_of_one_stale_lock_never_both_hold_it(rawstore_tmp_db, tmp_path):
    """One process is paused between judging the abandoned lock stale and taking it over; another goes ahead.

    Today both read the same abandoned file, both unlink it — the second removing the first one's fresh lock — and
    both create their own, so both hold the lock. With the OS lock the takeover is one atomic step: exactly one of
    the two gets it, and the loser sees a live holder.
    """
    path = job_lock._lock_path("edgar")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{_dead_pid()} abandoned")          # nobody holds this file's OS lock

    ready, judged, go = tmp_path / "ready", tmp_path / "judged", tmp_path / "go"
    first_result, second_result = tmp_path / "first", tmp_path / "second"
    first = second = None
    try:
        first = _acquire_in_a_child(path, ready, judged, go, first_result, pause=True)
        assert _wait_for(ready, 45), "the first acquirer never started"
        _wait_for(judged, 2)                             # no assertion: with the OS lock there is no pause to reach
        second = _acquire_in_a_child(path, ready, judged, go, second_result, pause=False)
        second.wait(60)
        go.write_text("")                                # let the paused process finish its takeover
        first.wait(60)
    finally:
        for proc in (first, second):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()

    assert first_result.exists() and second_result.exists(), "an acquirer never reported"
    got = [first_result.read_text(), second_result.read_text()]
    assert got.count("1") == 1, f"both acquirers hold the lock: {got}"


def _wait_for(path, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return path.exists()


# ── R2 (#364): a PID that looks alive cannot hold a freed lock ────────────────

def test_a_pid_that_looks_alive_cannot_hold_a_lock_the_os_has_freed(rawstore_tmp_db, monkeypatch):
    """No start time to compare (access denied, an unsupported OS): the PID alone no longer decides."""
    pid = 4242 if os.getpid() != 4242 else 4243
    monkeypatch.setattr(procs, "pid_alive", lambda p: True)
    real_start = procs.process_start_time
    # The holder's start time is the one that cannot be read; ours stays readable, so the metadata keeps its shape.
    monkeypatch.setattr(procs, "process_start_time",
                        lambda p: real_start(p) if p == os.getpid() else None)
    path = job_lock._lock_path("universe")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid} tok 1")

    assert job_lock.holders() == [], "an unlocked file is nobody's lock"
    with job_lock.acquire("universe", wait=0) as got:
        assert got is True
        assert path.read_text().split()[0] == str(os.getpid())


# ── R3: a crashed holder ──────────────────────────────────────────────────────

def test_a_crashed_holders_lock_is_free_at_once(rawstore_tmp_db):
    """The PID in the file now belongs to a live process (PID reuse); stale_after=0 must not make the taker wait."""
    proc = _live_pid()
    try:
        path = job_lock._lock_path("universe")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{proc.pid} tok {procs.process_start_time(proc.pid)}")
        with job_lock.acquire("universe", wait=0, stale_after=0) as got:
            assert got is True, "a lock nobody holds must be free"
            assert path.read_text().split()[1] != "tok"
            # the takeover is a real lock, not just a claim: another handle cannot take it
            assert job_lock.holders() == [("universe", os.getpid())]
            fd = os.open(path, os.O_RDWR)
            try:
                assert job_lock._os_lock(fd) is False, "the takeover did not lock the file"
            finally:
                os.close(fd)
    finally:
        proc.kill()
        proc.wait()


# ── R4: the metadata stays readable while the lock is held ────────────────────

def test_the_metadata_stays_readable_while_the_lock_is_held(rawstore_tmp_db):
    """The Windows OS lock is a mandatory byte range: it covers _LOCK_BYTE, never the metadata at offset 0."""
    path = job_lock._lock_path("universe")
    with job_lock.acquire("universe", wait=0) as got:
        assert got
        pid, token, start = job_lock._read_owner(path)
        assert pid == os.getpid() and len(token) == 32
        assert start == job_lock._our_start()

        fd = os.open(path, os.O_RDWR)                   # a second handle, as another process would use
        try:
            raw = os.read(fd, 4096).decode("ascii")
            assert raw.split()[:2] == [str(os.getpid()), token], "the metadata is not readable at offset 0"
            assert job_lock._os_lock(fd) is False, "a second handle took the byte range of a held lock"
        finally:
            os.close(fd)


# ── R5: holders() judges without touching the files ──────────────────────────

def test_holders_lists_the_live_lock_and_leaves_both_files_alone(rawstore_tmp_db):
    live = job_lock._lock_path("live")
    stale = job_lock._lock_path("stale")
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(f"{os.getpid()} token-of-a-live-holder 1")
    stale.write_text(f"{_dead_pid()} abandoned 1")
    fd = _hold_as_a_holder_does(live)
    try:
        before = {p.name: p.read_bytes() for p in (live, stale)}
        assert job_lock.holders() == [("live", os.getpid())]
        # a query is read-only: asking twice answers the same, so the first call took nothing (#365)
        assert job_lock.holders() == [("live", os.getpid())]
        assert {p.name: p.read_bytes() for p in (live, stale)} == before, "holders() wrote to a lock file"
    finally:
        _release(fd)


# ── R6: a lock that cannot be tried because it is held is TAKEN ───────────────

_HELD = pytest.mark.skipif(not job_lock._OS_LOCK_AVAILABLE, reason="no OS lock on this host")


def _held_lock(directory, pid: int = 4242):
    path = directory / "universe_run.lock"
    path.write_text(f"{pid} tok 1")
    return path


def _raising(exc):
    def _os_lock(_fd):
        raise exc
    return _os_lock


@_HELD
def test_an_eacces_from_the_lock_attempt_lists_the_holder(tmp_path, monkeypatch):
    """A held Windows byte-range lock surfaces as PermissionError/EACCES: it is a holder, not "nobody"."""
    path = _held_lock(tmp_path)
    monkeypatch.setattr(job_lock, "_os_lock", _raising(PermissionError(errno.EACCES, "Permission denied")))
    assert job_lock.holders(tmp_path) == [("universe_run", 4242)]
    assert path.read_text() == "4242 tok 1", "holders() wrote to the lock file"


@_HELD
def test_an_eperm_from_the_lock_attempt_lists_the_holder(tmp_path, monkeypatch):
    """POSIX lockf reports a held lock as EPERM on some filesystems."""
    _held_lock(tmp_path)
    monkeypatch.setattr(job_lock, "_os_lock", _raising(PermissionError(errno.EPERM, "Operation not permitted")))
    assert job_lock.holders(tmp_path) == [("universe_run", 4242)]


@_HELD
def test_a_lock_file_that_cannot_be_opened_lists_the_holder(tmp_path, monkeypatch):
    """The open itself is refused (a mandatory range, or an unreadable file): that is a holder too."""
    path = _held_lock(tmp_path)
    real_open = os.open

    def deny(target, flags, *args, **kwargs):
        if Path(target) == path:
            raise PermissionError(errno.EACCES, "Permission denied", str(target))
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(job_lock.os, "open", deny)
    assert job_lock.holders(tmp_path) == [("universe_run", 4242)]


@_HELD
def test_a_lock_file_that_vanishes_before_the_try_is_no_holder(tmp_path, monkeypatch):
    """ENOENT is the one answer that means nobody holds it: it must not raise, and must not be listed."""
    path = _held_lock(tmp_path)
    real_open = os.open

    def gone(target, flags, *args, **kwargs):
        if Path(target) == path:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(target))
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(job_lock.os, "open", gone)
    assert job_lock.holders(tmp_path) == []

