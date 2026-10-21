"""Host-level job mutex — one run at a time per lock name, across threads and processes.

The names in use: ``"edgar_sync"`` (``module_value_chain/sync.py``), ``"relatedness"``
(``module_relatedness/engine.py``) and ``"universe"`` (``core/universe_fetch.universe_write_lock``: the universe
writers, and the whole universe refresh job and its CLI, #244), with ``"universe_run"`` beside it while the refresh
job runs, so a board Fetch can tell it from Phase B, and ``"board_fetch_<board>"`` while a single-board Fetch runs,
so a universe run can tell it is running (``universe_fetch.board_fetch_lock``, #356).

The lock is a file created with ``O_CREAT|O_EXCL`` in the store dir, so it works
on Windows and POSIX without extra deps. ``acquire()`` yields ``True`` when the
lock was taken and ``False`` when another holder exists.

The file holds ``"<pid> <token> <start>"``. A lock is broken only when its holder
is no longer running — a job that legitimately runs for hours keeps its lock. A
file whose owner cannot be read (empty/garbled, e.g. a crash mid-write) falls
back to the age rule ``stale_after``. Release removes the file only if it still
carries our token, so a holder whose lock was broken can never delete the lock
of whoever took it over.

"Still running" is more than "the PID runs" (#355): Windows reuses PIDs, so after
a crash or a reboot another process, or the restarted dashboard itself, can carry
the PID in an old lock file. So a lock is live only when
- its PID is ours and its token is one this process issued and still holds, or
- its PID runs and that process started at ``<start>`` (``procs.process_start_time``).
  When either start time is unknown (a lock written before #355, access denied, an
  OS without one) the PID alone decides, as before.

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
    """True when the process that wrote this lock still runs (see the module docstring, #355)."""
    if pid == os.getpid():
        with _HELD_GUARD:
            return token in _HELD_TOKENS
    if not procs.pid_alive(pid):
        return False
    if start:
        now = procs.process_start_time(pid)
        if now is not None and now != start:
            return False                      # the PID runs, but it is another process now
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
    """Remove a stale lock unless it changed hands since we judged it stale."""
    if _read_owner(path) == seen:
        path.unlink(missing_ok=True)


def holders(directory: Path | None = None) -> list[tuple[str, int]]:
    """``(name, pid)`` of every live lock in the store dir."""
    d = Path(directory or Path(rawstore.DB_PATH).parent)
    out = []
    for p in d.glob("*.lock"):
        pid, token, start = _read_owner(p)
        if pid is not None and _holder_live(pid, token, start):
            out.append((p.stem, pid))
    return out


@contextmanager
def acquire(name: str = "edgar", wait: float = 0.0, stale_after: float = 3600.0):
    path = _lock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + max(0.0, wait)
    token = uuid.uuid4().hex
    got = False
    # Registered before the file exists, so another thread of ours never sees our fresh lock as a leftover.
    with _HELD_GUARD:
        _HELD_TOKENS.add(token)
    try:
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, f"{os.getpid()} {token} {_our_start()}".rstrip().encode("ascii"))
                finally:
                    os.close(fd)
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
            try:
                if got and _read_owner(path)[1] == token:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        with _HELD_GUARD:
            _HELD_TOKENS.discard(token)


__all__ = ["acquire", "holders"]
