"""Host-level job mutex — one run at a time per lock name, across threads and processes.

The names in use: ``"edgar_sync"`` (``module_value_chain/sync.py``), ``"relatedness"``
(``module_relatedness/engine.py``) and ``"universe"`` (``core/universe_fetch.universe_write_lock``: the universe
writers, and the whole universe refresh job and its CLI, #244), with ``"universe_run"`` beside it while the refresh
job runs, so a board Fetch can tell it from Phase B, and ``"board_fetch_<board>"`` while a single-board Fetch runs,
so a universe run can tell it is running (``universe_fetch.board_fetch_lock``, #356).

The lock is an OS file lock on a file in the store dir: ``fcntl.flock`` on POSIX, and ``msvcrt.locking`` on Windows
on one byte at ``_LOCK_BYTE`` (a mandatory byte range, so it never covers the metadata). ``acquire()`` yields
``True`` when the lock was taken and ``False`` when another holder exists. The kernel owns liveness (#365): a lock
is held while another handle holds its byte, so two acquirers can never both win one, and the lock of a process
that died is free at once — no PID, no start time and no age decides that.

The file holds ``"<pid> <token> <start>"``. That is a record, not the lock: an acquirer that wins the byte truncates
the file and writes its own line. The PID rules still veto a takeover when they *positively* identify a live owner
(#355): its PID is ours and its token is one this process issued and still holds, or its PID runs and that process
started at ``<start>`` (``procs.process_start_time``). An owner whose start time cannot be read is not vouched for
(#364): the byte decides, so a live holder keeps its lock and a crashed one is not held for ever by a PID that came
back. Release removes the file only if it still carries our token, so a holder whose lock was taken over can never
delete the lock of whoever took it. A leftover file that nobody holds is harmless: the next acquirer judges by the
OS lock.

Where neither ``fcntl`` nor ``msvcrt`` imports, the pre-#365 escape runs verbatim: create with ``O_CREAT|O_EXCL``
and break a lock whose owner is gone, with ``stale_after`` as the age rule for a file whose owner cannot be read.

    with job_lock.acquire("edgar_sync", wait=0) as got:
        if not got:
            return  # someone else is running it
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import procs, rawstore

try:                                    # POSIX
    import fcntl as _fcntl
except ImportError:                     # pragma: no cover - the Windows half covers this branch
    _fcntl = None
try:                                    # Windows
    import msvcrt as _msvcrt
except ImportError:                     # pragma: no cover - the POSIX half covers this branch
    _msvcrt = None

_OS_LOCK_AVAILABLE = _fcntl is not None or _msvcrt is not None
# ``msvcrt.locking`` takes a mandatory byte-range lock, so the byte it holds must never be part of the metadata
# another process reads: offset 0 keeps ``"<pid> <token> <start>"``, this one byte belongs to the kernel.
_LOCK_BYTE = 1 << 20

_WIN_GENERIC_READ_WRITE = 0x80000000 | 0x40000000
_WIN_SHARE_ALL = 0x1 | 0x2 | 0x4       # FILE_SHARE_READ | WRITE | DELETE: a lock file is unlinked, never kept open
_WIN_OPEN_ALWAYS = 4
_WIN_OPEN_EXISTING = 3
_WIN_KERNEL32: tuple | None = None


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


def _holder_live(pid: int, token: str, start: str) -> bool:
    """True when the process that wrote this lock is positively identified and still runs (#355, #364)."""
    if pid == os.getpid():
        with _HELD_GUARD:
            return token in _HELD_TOKENS
    if not procs.pid_alive(pid):
        return False
    if start:
        now = procs.process_start_time(pid)
        # An unreadable start time vouches for nothing (#364): the OS lock decides, so a live holder keeps its
        # lock and a crashed one is not held for ever by a PID that came back.
        return now is not None and now == start
    return True


def _is_stale(path: Path, stale_after: float) -> bool:
    pid, token, start = _read_owner(path)
    if pid is not None:
        return not _holder_live(pid, token, start)
    try:
        return bool(stale_after) and time.time() - path.stat().st_mtime > stale_after
    except OSError:
        return False


def _break_if_unchanged(path: Path, seen: tuple[int | None, str, str]) -> None:
    """Remove a stale lock unless it changed hands since we judged it stale.

    Only the fallback path uses this (no ``fcntl``/``msvcrt``): the OS lock makes the takeover atomic, so nothing
    is unlinked to take a lock any more (#365).
    """
    if _read_owner(path) == seen:
        path.unlink(missing_ok=True)


# ── the OS lock ───────────────────────────────────────────────────────────────

def _kernel32():
    """``kernel32`` with ``CreateFileW`` typed, loaded once."""
    global _WIN_KERNEL32
    if _WIN_KERNEL32 is None:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        _WIN_KERNEL32 = (ctypes, wintypes, k32)
    return _WIN_KERNEL32


def _open_lock_file(path: Path, create: bool = True) -> int:
    """An fd for the lock file; ``create`` False opens an existing one only (the read-only probe of ``holders()``).

    ``msvcrt.locking`` needs a CRT fd, and ``os.open`` on Windows does not share the file for deletion, while a lock
    file is deleted — by its release, and by a lock broken or rolled back around it — so the handle allows it.
    """
    if os.name != "nt":
        return os.open(path, (os.O_CREAT | os.O_RDWR) if create else os.O_RDWR, 0o644)
    ctypes, wintypes, k32 = _kernel32()
    handle = k32.CreateFileW(str(path), _WIN_GENERIC_READ_WRITE, _WIN_SHARE_ALL, None,
                             _WIN_OPEN_ALWAYS if create else _WIN_OPEN_EXISTING, 0, None)
    if not handle or handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), f"cannot open the lock file {path.name}")
    return _msvcrt.open_osfhandle(handle, os.O_RDWR)


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _os_lock(fd: int) -> bool:
    """Take this process's lock on ``fd`` if nobody holds it. False = a live holder; never raises (#365)."""
    try:
        if _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
        _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False
    finally:
        try:
            os.lseek(fd, 0, os.SEEK_SET)       # the metadata lives at offset 0
        except OSError:
            pass


def _os_unlock(fd: int) -> None:
    """Release the lock ``_os_lock`` took on ``fd``; never raises."""
    try:
        if _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        else:
            os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _os_held(path: Path) -> bool:
    """True when another handle holds this lock file's byte.

    Read-only: a successful probe is released at once and the file is left exactly as it was (never stolen,
    truncated or unlinked), and a file that vanished under the probe is simply not held.
    """
    if not _OS_LOCK_AVAILABLE:
        return False
    try:
        fd = _open_lock_file(path, create=False)
    except OSError:
        return False
    try:
        if _os_lock(fd):
            _os_unlock(fd)
            return False
        return True
    finally:
        _close(fd)


def holders(directory: Path | None = None) -> list[tuple[str, int]]:
    """``(name, pid)`` of every live lock in the store dir."""
    d = Path(directory or Path(rawstore.DB_PATH).parent)
    out = []
    for p in d.glob("*.lock"):
        pid, token, start = _read_owner(p)
        if pid is None:
            continue                            # no owner to name (and no file of ours to make up one for)
        if _holder_live(pid, token, start) or _os_held(p):
            out.append((p.stem, pid))
    return out


# ── acquire ───────────────────────────────────────────────────────────────────

def _write_owner(fd: int, token: str) -> None:
    """Replace the file's record with ours: it is what ``holders()`` and the next acquirer read."""
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()} {token} {_our_start()}".rstrip().encode("ascii"))


def _take_by_file(path: Path, token: str, stale_after: float) -> bool:
    """The takeover without an OS lock: ``O_CREAT|O_EXCL``, and a stale file broken (the pre-#365 escape)."""
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            seen = _read_owner(path)
            if _is_stale(path, stale_after):
                try:
                    _break_if_unchanged(path, seen)
                    continue
                except OSError:
                    pass                        # could not remove it: the caller waits for the deadline
            return False
        try:
            _write_owner(fd, token)
        finally:
            os.close(fd)
        return True


def _take(path: Path, token: str, stale_after: float) -> tuple[bool, int | None]:
    """Try once to take the lock: ``(got, fd)``, the fd held for the whole with-block (None in the fallback)."""
    if not _OS_LOCK_AVAILABLE:
        return _take_by_file(path, token, stale_after), None
    seen = _read_owner(path)
    if seen[0] is not None and not _is_stale(path, stale_after):
        return False, None                      # a process that wrote this file still runs: it keeps it
    try:
        fd = _open_lock_file(path)
    except OSError:
        return False, None
    if not _os_lock(fd):
        _close(fd)                              # another holder's byte: alive
        return False, None
    if _read_owner(path) != seen:               # it changed hands while we judged it
        _os_unlock(fd)
        _close(fd)
        return False, None
    try:
        _write_owner(fd, token)
    except OSError:
        _os_unlock(fd)
        _close(fd)
        return False, None
    return True, fd


@contextmanager
def acquire(name: str = "edgar", wait: float = 0.0, stale_after: float = 3600.0):
    path = _lock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + max(0.0, wait)
    token = uuid.uuid4().hex
    got = False
    fd: int | None = None
    # Registered before the file exists, so another thread of ours never sees our fresh lock as a leftover.
    with _HELD_GUARD:
        _HELD_TOKENS.add(token)
    try:
        while True:
            got, fd = _take(path, token, stale_after)
            if got:
                break
            if time.time() >= deadline:
                break
            time.sleep(0.2)
        try:
            yield got
        finally:
            if fd is not None:
                _os_unlock(fd)                  # the byte first, then the handle
                _close(fd)
            try:
                if got and _read_owner(path)[1] == token:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        with _HELD_GUARD:
            _HELD_TOKENS.discard(token)


__all__ = ["acquire", "holders"]
