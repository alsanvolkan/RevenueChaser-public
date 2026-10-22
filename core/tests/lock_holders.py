"""A live holder of a ``job_lock`` name, for the writer-lock tests (PL6-a, #26).

``job_lock.acquire`` takes a non-blocking OS lock on the file's ``_LOCK_BYTE`` (``fcntl.flock`` /
``msvcrt.locking``), and a second handle on the same file cannot take that byte range again — which is exactly what a
holder in another process, in another thread, or a CLI run does (#365,
``test_job_lock_os_lock_364_365.py:37``). The file carries ``"<pid> <token>"`` at offset 0 so ``holders()`` reports a
real PID, the way a production holder's metadata does.

Not a test module (no ``test_`` prefix).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from RevenueChaser.core import job_lock

HOLDER_TOKEN = "test-holder"


@contextmanager
def lock_held(name: str):
    """Hold ``name``'s lock through a second handle for the block; yields the lock file's path.

    While the block runs, ``job_lock.acquire(name, wait=0)`` yields ``False`` here, as it does for a second process.
    """
    if not job_lock._OS_LOCK_AVAILABLE:                       # pragma: no cover - every supported host has one
        pytest.skip("no OS file lock on this host: a second handle cannot hold the fallback's file lock")
    path = job_lock._lock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()} {HOLDER_TOKEN}".encode("ascii"))
        assert job_lock._os_lock(fd) is True, f"the {name!r} lock was expected to be free"
        yield Path(path)
    finally:
        job_lock._os_unlock(fd)
        os.close(fd)
