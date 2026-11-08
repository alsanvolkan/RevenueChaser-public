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

``RATE`` is the only knob: the sustained rate. The burst is derived from the ceiling,
``LIMIT - RATE`` (1 at the default 9), so the first second after an idle period carries
at most ``burst + RATE`` = ``LIMIT`` requests and never above SEC's 10/s, which a burst
equal to the rate (16 in the first second) would exceed.

When the file or the lock cannot be used, ``pace`` returns False and the caller
falls back to its in-process throttle. A pacing failure never fails the request.

``shared_pace`` is the same bucket under another file, rate and burst, for another limit two processes share
(OpenFIGI's per-key window, ``core/providers/openfigi.py``).
"""
from __future__ import annotations

import atexit
import errno
import json
import logging
import os
import threading
import time
from pathlib import Path

from . import job_lock, rawstore

logger = logging.getLogger(__name__)

# SEC fair-access ceiling, requests per second, per user-agent (both hosts share it).
LIMIT = 10
# The sustained rate. The only knob; the burst is derived from the ceiling (see ``_burst``).
RATE = 9
# The processes that can ask SEC at the same time on the box. When this bucket cannot be used each paces itself at
# its share of the ceiling (``FALLBACK_RATE``), so all of them falling back together stay under it. A new SEC caller
# that runs as its own process belongs on this list.
SEC_PROCESSES = ("RC-Build", "the dashboard", "RC-ValueChain-Sync", "RC-Relatedness-OffPeak", "RC-Screeners-Daily",
                 "RC-Daily-Bars")
FALLBACK_RATE = LIMIT / len(SEC_PROCESSES)

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
_LOGGED: set[str] = set()

# Why ``_reserve`` gave up, per request, for this process's SEC bucket (#487: a fallback was logged once and its
# cause could not be told from the log, so a fix was chosen blind). ``shared`` counts the requests the bucket paced.
# Read with :func:`stats`. A process that fell back logs the totals hourly while it runs (the dashboard is stopped
# by SIGTERM, which skips ``atexit``) and once more when it exits normally.
#: ``no_os_lock``: the platform has no OS lock. ``open_failed``: the bucket's folder or file could not be opened (an ``errno``
#: name follows). ``lock_timeout``: the lock was not won inside ``_LOCK_WAIT``. ``io_error``: reading or writing the
#: state failed (``errno`` name follows). ``exception``: anything else, caught in :func:`pace`.
_COUNTS: dict[str, int] = {}
_last_reason: str | None = None
_TOTALS_EVERY = 3600.0
_totals_logged_at: float | None = None


def _count(key: str) -> None:
    _COUNTS[key] = _COUNTS.get(key, 0) + 1


def _why(prefix: str, exc: OSError) -> str:
    return f"{prefix}:{errno.errorcode.get(exc.errno, exc.errno)}" if exc.errno else prefix


def stats() -> dict[str, int]:
    """This process's SEC bucket outcomes so far: ``shared`` and one key per fallback reason."""
    with _GUARD:
        return dict(_COUNTS)


def _log_totals(counts: dict[str, int] | None = None) -> None:
    counts = stats() if counts is None else counts
    fell = {k: v for k, v in counts.items() if k != "shared"}
    if fell:
        logger.warning("SEC pacing totals: %d request(s) on the shared bucket, %d fell back (%s)",
                       counts.get("shared", 0), sum(fell.values()),
                       ", ".join(f"{k} {v}" for k, v in sorted(fell.items())))


def _totals_due(now: float) -> dict[str, int] | None:
    """Under ``_GUARD``: a copy of the counts an hour after the first fallback, and hourly after that. The first
    fallback itself is already its own reason line, so the clock starts there."""
    global _totals_logged_at
    if _totals_logged_at is None:
        _totals_logged_at = now
        return None
    if now - _totals_logged_at < _TOTALS_EVERY:
        return None
    _totals_logged_at = now
    return dict(_COUNTS)


atexit.register(_log_totals)


def _log_once(kind: str, message: str, *args) -> None:
    """One log line per process per kind: whether the shared bucket is in use, or why it is not."""
    if kind in _LOGGED:
        return
    _LOGGED.add(kind)
    logger.warning(message, *args)   # no SEC CLI sets up logging: INFO would vanish in exactly the second process


def _burst(rate: float) -> float:
    """Tokens a full bucket holds: what the ceiling leaves above the sustained rate (at least 1)."""
    return max(1.0, float(LIMIT) - float(rate))


def _owner(path: Path) -> str:
    """Who owns the bucket file, for the fallback warning: a file another user created (``sudo``) refuses this one."""
    try:
        st = path.stat()
    except OSError:
        return "no file"
    return f"uid {st.st_uid}, mode {st.st_mode & 0o777:o}"


def bucket_path(name: str = _NAME) -> Path:
    """The bucket file: beside ``raw.duckdb``, so ``RC_STORE_DIR`` and the test isolation follow."""
    return Path(rawstore.DB_PATH).parent / name


def _take(state: dict, now: float, rate: float, burst: float | None = None) -> tuple[float, dict]:
    """Return ``(wait_seconds, new_state)`` after reserving one token.

    ``tokens`` may be negative. That is a reservation whose caller has not slept
    yet; the next caller refills from the deficit, so two processes cannot both
    spend the last token. ``ts`` is the host monotonic clock (comparable across
    processes on one machine), the time of this reservation — not a future time.
    """
    burst = _burst(rate) if burst is None else float(burst)
    tokens = state.get("tokens") if isinstance(state, dict) else None
    ts = state.get("ts") if isinstance(state, dict) else None
    trustworthy = (
        isinstance(tokens, (int, float)) and not isinstance(tokens, bool)
        and isinstance(ts, (int, float)) and not isinstance(ts, bool)
        and -float(rate) * _DEFICIT_BURSTS <= float(tokens) <= burst
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


def _reserve(now: float, name: str = _NAME, rate: float | None = None, burst: float | None = None) -> float | None:
    """Reserve one token (of the SEC bucket, or ``name``'s at ``rate`` and ``burst``). ``None``: it cannot be used,
    and ``_last_reason`` says why."""
    global _last_reason
    _last_reason = None
    if not job_lock._OS_LOCK_AVAILABLE:
        _last_reason = "no_os_lock"
        return None
    path = bucket_path() if name == _NAME else bucket_path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o660)   # the box's users of the bucket share its group
    except OSError as exc:
        _last_reason = _why("open_failed", exc)
        return None
    try:
        if not _lock_within(fd, _LOCK_WAIT):
            _last_reason = "lock_timeout"
            return None
        try:
            wait, new_state = _take(_read_state(fd), now, float(RATE if rate is None else rate), burst)
            _write_state(fd, new_state)
            return wait
        finally:
            job_lock._os_unlock(fd)
    except OSError as exc:
        _last_reason = _why("io_error", exc)
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
            reason = _last_reason
            _count("shared" if wait is not None else (reason or "unknown"))
            due = _totals_due(time.monotonic()) if wait is None else None
        if due is not None:
            _log_totals(due)
        if wait is None:
            # One line per reason, not one per process: the first reason seen used to hide every later one.
            _log_once(f"fallback {reason}", "SEC pacing: the shared bucket %s (%s) cannot be used (%s); this request "
                      "paces itself (in-process, %g/s)", bucket_path(), _owner(bucket_path()), reason, FALLBACK_RATE)
            return False
        _log_once("shared", "SEC pacing: shared bucket %s (rate %s/s, burst %s)", bucket_path(), RATE, _burst(RATE))
        if wait > 0:
            time.sleep(wait)
        return True
    except Exception:
        with _GUARD:
            _count("exception")
        _log_once("fallback exception", "SEC pacing: the shared bucket failed; this request paces itself (in-process)")
        return False


def shared_pace(name: str, rate: float, burst: float) -> bool:
    """Wait for one token of the bucket file ``name`` (``rate`` tokens a second, at most ``burst`` held). False when it
    cannot be used, and the caller paces itself: never raises."""
    try:
        with _GUARD:
            wait = _reserve(time.monotonic(), name, rate, burst)
        if wait is None:
            _log_once(f"fallback {name}", "shared bucket %s cannot be used; this process paces itself", bucket_path(name))
            return False
        if wait > 0:
            time.sleep(wait)
        return True
    except Exception:
        _log_once(f"fallback {name}", "shared bucket %s failed; this process paces itself", name)
        return False


__all__ = ["FALLBACK_RATE", "SEC_PROCESSES", "LIMIT", "RATE", "bucket_path", "pace", "shared_pace", "stats"]
