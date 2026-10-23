"""Cross-process SEC fair-access bucket (#26, PL6-b).

SEC's limit is 10 requests/second per user-agent, and ``www.sec.gov`` and
``data.sec.gov`` share it. One process already paces itself (``sec_edgar._throttle``).
Two processes do not: the dashboard's identity refresh and a filings sync each
keep their own bucket, and together they can reach ~20/s, which is what draws
a 403 block page.

This module is the one budget both processes spend. The state is a small JSON
file beside the store (the same directory ``job_lock`` uses), and the critical
section is the OS lock already in ``job_lock`` — ``fcntl.flock`` or
``msvcrt.locking``. A holder that dies releases that lock with its process, so
a crash cannot wedge the bucket. The file is a dotfile, not ``*.lock``:
``job_lock.holders()`` globs ``*.lock``, and a token file is not a job mutex.

``RATE`` is the only knob. It is both the sustained rate and the burst, so the
two cannot drift. 8 stays under the 10/s fair-access limit.

When the file or the lock cannot be used, ``pace`` returns False and the caller
falls back to its in-process throttle. A pacing failure never fails the request.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import job_lock, rawstore

# Requests per second, and the burst. SEC fair-access is 10/s; stay under it.
RATE = 8

# How long to wait for the OS lock before giving the caller the in-process fallback.
# A live critical section is a read and a write; anything longer means the lock is stuck.
_LOCK_WAIT = 0.25
_LOCK_SPIN = 0.002

# A timestamp more than this far ahead is not a clock a live peer wrote. Reset.
_SKEW = 1.0
# How many bursts of reservations still count as a live pile-up (two processes, a
# handful of threads each) rather than a corrupt file. Derived from RATE, not a second knob.
_DEFICIT_BURSTS = 8

_GUARD = threading.Lock()
_NAME = ".sec_throttle.json"


def bucket_path() -> Path:
    """The bucket file: beside ``raw.duckdb``, so ``RC_STORE_DIR`` and the test isolation follow."""
    return Path(rawstore.DB_PATH).parent / _NAME


def _take(state: dict, now: float, rate: float) -> tuple[float, dict]:
    """Return ``(wait_seconds, new_state)`` after reserving one token.

    ``tokens`` may be negative. That is a reservation whose caller has not slept
    yet; the next caller refills from the deficit, so two processes cannot both
    spend the last token. ``ts`` is the host monotonic clock (comparable across
    processes on one machine), the time of this reservation — not a future time.
    """
    burst = float(rate)
    tokens = state.get("tokens") if isinstance(state, dict) else None
    ts = state.get("ts") if isinstance(state, dict) else None
    trustworthy = (
        isinstance(tokens, (int, float)) and not isinstance(tokens, bool)
        and isinstance(ts, (int, float)) and not isinstance(ts, bool)
        and -burst * _DEFICIT_BURSTS <= float(tokens) <= burst
        and float(ts) <= now + _SKEW
    )
    if trustworthy:
        elapsed = max(0.0, now - float(ts))
        tokens = min(burst, float(tokens) + elapsed * rate)
    else:
        tokens = burst
    tokens -= 1.0
    wait = 0.0 if tokens >= 0.0 else (-float(tokens)) / rate
    # Cap one sleep so a math bug cannot stall a multi-thousand request run. Do not
    # refill: the deficit stays, and the next caller waits for it too.
    if wait > _DEFICIT_BURSTS:
        wait = float(_DEFICIT_BURSTS)
    return wait, {"tokens": tokens, "ts": now}


def _read_state(fd: int) -> dict:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 65536)
    if not raw.strip():
        return {}
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(fd: int, state: dict) -> None:
    payload = json.dumps(state, separators=(",", ":")).encode("ascii")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload)


def _lock_within(fd: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if job_lock._os_lock(fd):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_LOCK_SPIN)


def _reserve(now: float) -> float | None:
    """Reserve one token. ``None`` means the shared bucket cannot be used."""
    if not job_lock._OS_LOCK_AVAILABLE:
        return None
    path = bucket_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR)
    except OSError:
        return None
    try:
        if not _lock_within(fd, _LOCK_WAIT):
            return None
        try:
            wait, new_state = _take(_read_state(fd), now, float(RATE))
            _write_state(fd, new_state)
            return wait
        finally:
            job_lock._os_unlock(fd)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def pace() -> bool:
    """Wait for one shared token. False when the bucket is unavailable — never raises.

    The sleep is outside the lock, so a peer is not stuck behind this process's
    interval. Threads of this process share ``_GUARD`` for the reservation only:
    on POSIX the OS lock is per-process and would not keep two threads apart.
    """
    try:
        with _GUARD:
            wait = _reserve(time.monotonic())
        if wait is None:
            return False
        if wait > 0:
            time.sleep(wait)
        return True
    except Exception:
        return False


__all__ = ["RATE", "bucket_path", "pace"]
