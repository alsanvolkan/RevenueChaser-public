"""Host-level job mutex — one run at a time per lock name, across threads and processes.

The names in use: ``"edgar_sync"`` (``module_value_chain/sync.py``), ``"relatedness"``
(``module_relatedness/engine.py``) and ``"universe"`` (``core/universe_fetch.universe_write_lock``: the universe
writers, and the whole universe refresh job and its CLI, #244), with ``"universe_run"`` beside it while the refresh
job runs, so a board Fetch can tell it from Phase B, and ``"board_fetch_<board>"`` while a single-board Fetch runs,
so a universe run can tell it is running (``universe_fetch.board_fetch_lock``, #356).

The lock is a file in the store dir, and **the kernel owns its liveness**: ``acquire()`` opens it and takes a
non-blocking OS lock on it (``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows). So a holder that dies releases
its lock, a lock file left behind is harmless, and a takeover is a single atomic step — two acquirers can no longer
both judge one abandoned file stale and both end up holding it (#365). ``acquire()`` yields ``True`` when the lock
was taken and ``False`` when another holder exists.

The file holds ``"<pid> <token> <start>"`` at offset 0: that is what ``holders()`` reports and what a release checks
before removing the file, so a holder whose lock was broken can never delete the lock of whoever took it over. On
Windows the OS lock is a mandatory byte range, so it covers the one byte at ``_LOCK_BYTE`` and never the metadata a
second process has to read. ``holders()`` reads a lock it cannot try — ``EACCES``, ``EPERM``, ``EAGAIN`` /
``EWOULDBLOCK``, from the try or from the open — as held, and only ``ENOENT`` as no holder (#365).

A second thread of this process is excluded too: a lock file carrying a token this process issued and still holds is
live even though the kernel would let the same process re-take it.

Where no OS lock exists (``_OS_LOCK_AVAILABLE`` is False: neither ``fcntl`` nor ``msvcrt`` imports) the older
heuristic runs unchanged: the PID plus its start time decides, and a file whose owner cannot be read at all (empty or
garbled, e.g. a crash mid-write) falls back to the age rule ``stale_after``. "Still running" there is more than
"the PID runs" (#355): Windows reuses PIDs, so after a crash or a reboot another process, or the restarted dashboard
itself, can carry the PID in an old lock file. So a lock is live only when
- its PID is ours and its token is one this process issued and still holds, or
- its PID runs and that process started at ``<start>`` (``procs.process_start_time``).
  When either start time is unknown (a lock written before #355, access denied, an
  OS without one) the PID alone decides, as before.

    with job_lock.acquire("edgar_sync", wait=0) as got:
        if not got:
            return  # someone else is running it
"""
from __future__ import annotations

import errno
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import procs, rawstore

try:                                                    # POSIX
    import fcntl
except ImportError:                                     # pragma: no cover - Windows
    fcntl = None
try:                                                    # Windows
    import msvcrt
except ImportError:                                     # pragma: no cover - POSIX
    msvcrt = None

# False only where neither module exists: then the PID-and-start-time fallback below is the whole rule.
_OS_LOCK_AVAILABLE = fcntl is not None or msvcrt is not None

# The byte the OS lock covers on Windows. msvcrt.locking takes a *mandatory* byte-range lock, so this must never be
# the byte the metadata sits in: the owner is written at offset 0 and stays readable by a second process (#365).
_LOCK_BYTE = 1 << 20


# Tokens of the locks this process holds now. A lock file with our PID and any other token was left by an
# earlier process that had our PID (#355).
_HELD_TOKENS: set[str] = set()
_HELD_GUARD = threading.Lock()
_OUR_START: dict[int, str] = {}


def _our_start() -> str:
    """This process's start time, read once (keyed by PID, so a forked child reads its own)."""
    pid = os.getpid()
    if pid not in _OUR_START:
        _OUR_START[pid] = procs.process_start_time(pid) or ""
    return _OUR_START[pid]


def _lock_path(name: str) -> Path:
    # Beside the raw DB, so it follows RC_STORE_DIR / the test isolation fixture.
    return Path(rawstore.DB_PATH).parent / f"{name}.lock"


def _read_owner(path: Path) -> tuple[int | None, str, str]:
    """``(pid, token, start)`` from a lock file; ``(None, "", "")`` when unreadable."""
    try:
        parts = path.read_text(encoding="ascii", errors="replace").split()
    except OSError:
        return None, "", ""
    if not parts:
        return None, "", ""
    try:
        pid = int(parts[0])
    except ValueError:
        return None, "", ""
    return pid, (parts[1] if len(parts) > 1 else ""), (parts[2] if len(parts) > 2 else "")


def _os_lock(fd: int) -> bool:
    """Take this process's exclusive OS lock on ``fd``; False when another holder has it. Never raises.

    ``flock`` is preferred on POSIX: its lock belongs to the open file description, so an unrelated open and close
    of the same file (``holders()``) cannot drop it the way a POSIX record lock would.
    """
    try:
        if fcntl is not None:
            if getattr(fcntl, "flock", None) is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1)
        else:
            os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        os.lseek(fd, 0, os.SEEK_SET)
        return True
    except OSError:                                     # BlockingIOError included: someone else holds it
        return False


def _os_unlock(fd: int) -> None:
    """Release what ``_os_lock`` took. Never raises."""
    try:
        if fcntl is not None:
            if getattr(fcntl, "flock", None) is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
                fcntl.lockf(fd, fcntl.LOCK_UN, 1)
        else:
            os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _token_is_ours(token: str) -> bool:
    """True while ``token`` is one this process issued and has not released."""
    if not token:
        return False
    with _HELD_GUARD:
        return token in _HELD_TOKENS


def _holder_live(pid: int | None, token: str, start: str) -> bool:
    """True when the process that wrote this lock still runs (the fallback rule, see the module docstring)."""
    if pid is None:
        return False
    if pid == os.getpid():
        return _token_is_ours(token)
    if not procs.pid_alive(pid):
        return False
    if start:
        now = procs.process_start_time(pid)
        if now is not None and now != start:
            return False                      # the PID runs, but it is another process now
    return True


def _taken_error(exc: OSError) -> bool:
    """True when ``exc`` says the lock is HELD (or cannot be tried), False only when the file is gone.

    A held lock surfaces as ``PermissionError`` / ``EACCES`` (a Windows mandatory byte range refusing the open
    or the try), ``EPERM`` (POSIX ``lockf`` through some filesystems, and ``flock``) or ``EAGAIN`` /
    ``EWOULDBLOCK`` (a non-blocking lock that is taken). None of those may read as "no holder":
    ``core.universe_fetch.universe_run_holder`` uses ``holders()`` to hold a board Fetch off during a universe
    run, and "nobody holds it" there starts the Fetch beside the run (#365). Only ENOENT — the file vanished
    between the glob and here — is nobody's lock.
    """
    return getattr(exc, "errno", None) != errno.ENOENT


def _os_lock_is_taken(path: Path) -> bool:
    """True when a holder has ``path``'s OS lock. Tries it and releases it at once; writes nothing.

    A try the platform refuses is a held lock (``_taken_error``), never "no holder", and nothing here raises.
    """
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError as exc:                              # ENOENT: gone; anything else: it cannot be tried
        return _taken_error(exc)
    try:
        try:
            got = _os_lock(fd)
        except OSError as exc:                          # a platform that reports the refusal by raising
            return _taken_error(exc)
        if got:
            _os_unlock(fd)
        return not got
    finally:
        os.close(fd)


def _live_pid(path: Path) -> int | None:
    """The PID of a live holder of ``path``, or None. Reads, and tries the OS lock; never steals or unlinks."""
    pid, token, start = _read_owner(path)
    if pid is None:
        return None
    if pid == os.getpid() and _token_is_ours(token):
        return pid                            # ours: another thread of this process may hold it
    if not _OS_LOCK_AVAILABLE:
        return pid if _holder_live(pid, token, start) else None
    return pid if _os_lock_is_taken(path) else None


def _held_by_this_process(path: Path) -> bool:
    """True when another thread of this process holds ``path``: the kernel would let us re-take our own lock."""
    pid, token, _start = _read_owner(path)
    return pid == os.getpid() and _token_is_ours(token)


def _is_stale(path: Path, stale_after: float) -> bool:
    pid, token, start = _read_owner(path)
    if pid is not None:
        return not _holder_live(pid, token, start)
    try:
        return bool(stale_after) and time.time() - path.stat().st_mtime > stale_after
    except OSError:
        return False


def _break_if_unchanged(path: Path, seen: tuple[int | None, str, str]) -> None:
    """Remove a stale lock unless it changed hands since we judged it stale."""
    if _read_owner(path) == seen:
        path.unlink(missing_ok=True)


def holders(directory: Path | None = None) -> list[tuple[str, int]]:
    """``(name, pid)`` of every live lock in the store dir. Checks only: never takes, truncates or removes a lock.

    A lock this cannot be tried — permission denied, or any OS-lock error but ENOENT — is a holder (``_taken_error``).
    """
    d = Path(directory or Path(rawstore.DB_PATH).parent)
    out = []
    for p in d.glob("*.lock"):
        pid = _live_pid(p)
        if pid is not None:
            out.append((p.stem, pid))
    return out


@contextmanager
def acquire(name: str = "edgar", wait: float = 0.0, stale_after: float = 3600.0):
    """Hold ``name``'s lock: yields ``True`` when taken, ``False`` when another holder keeps it out.

    ``stale_after`` is used by the fallback only (no OS lock on this host): on the OS-lock path a file whose holder
    is gone is free at once, and its age means nothing.
    """
    path = _lock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + max(0.0, wait)
    token = uuid.uuid4().hex
    got = False
    fd = None
    # Registered before the file exists, so another thread of ours never sees our fresh lock as a leftover.
    with _HELD_GUARD:
        _HELD_TOKENS.add(token)
    try:
        if _OS_LOCK_AVAILABLE:
            while True:
                fd = os.open(path, os.O_CREAT | os.O_RDWR)
                if not _held_by_this_process(path) and _os_lock(fd):
                    try:
                        os.ftruncate(fd, 0)
                        os.lseek(fd, 0, os.SEEK_SET)
                        os.write(fd, f"{os.getpid()} {token} {_our_start()}".rstrip().encode("ascii"))
                    except OSError:
                        # The lock is ours but the owner never landed (a full disk): release it here, or this
                        # process would hold it until it exits and refuse its own next attempt.
                        _os_unlock(fd)
                        os.close(fd)
                        raise
                    got = True
                    break
                os.close(fd)
                fd = None
                if time.time() >= deadline:
                    break
                time.sleep(0.2)
        else:
            while True:
                try:
                    # Its own handle, never ``fd``: the fallback takes no OS lock, and the release below must
                    # not try to unlock or close a descriptor this branch has already closed.
                    new_fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    try:
                        os.write(new_fd, f"{os.getpid()} {token} {_our_start()}".rstrip().encode("ascii"))
                    finally:
                        os.close(new_fd)
                    got = True
                    break
                except FileExistsError:
                    seen = _read_owner(path)
                    if _is_stale(path, stale_after):
                        try:
                            _break_if_unchanged(path, seen)
                            continue
                        except OSError:
                            pass  # could not remove it: fall through to the deadline
                    if time.time() >= deadline:
                        break
                    time.sleep(0.2)
        try:
            yield got
        finally:
            if fd is not None:
                _os_unlock(fd)
                os.close(fd)
            try:
                # After the close, not before: Windows refuses to delete a file this process still has open, and
                # the token check is what keeps us off a holder's fresh lock — not the order of the two steps.
                if got and _read_owner(path)[1] == token:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        with _HELD_GUARD:
            _HELD_TOKENS.discard(token)


__all__ = ["acquire", "holders"]
