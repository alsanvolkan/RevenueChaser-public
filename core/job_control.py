"""Cooperative stop for a running job: cancel and deadline (#243, PL4).

A job that runs is never interrupted from outside: its runner holds store transactions and writes files. Instead
the job queue (``app/jobs.py``) binds a ``StopToken`` to the runner's thread, and the runner checks it where
stopping is safe:

- ``checkpoint(next_phase)`` at a phase boundary, e.g. between Asymmetric Growth, Swing and Fundamentals in the
  universe run. It raises ``JobCancelled`` after a cancel and ``JobDeadline`` once the deadline has passed;
- ``stop_requested()`` in the per-ticker pools (AG, and the Swing/Fundamentals ``run_screen``), which then cancel
  the tickers not yet started and call ``checkpoint()``, so the scan returns nothing to write or freeze.

A stop is a ``BaseException``, like ``KeyboardInterrupt``: the ``except Exception`` guards that keep one ticker or
one history write from failing a Fetch must not swallow it. ``finally`` blocks still run, so locks are released.
Outside a job (the CLI, a test, a Refresh) no token is bound and every check is a no-op.

A runner that never reaches a check (blocked in one call) is not stopped; the providers' own HTTP timeouts bound
each call.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

_local = threading.local()


class JobStopped(BaseException):
    """The job was asked to stop; ``message`` says why and where."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class JobCancelled(JobStopped):
    """Cancelled by the user (the dashboard's cancel on a running job)."""


class JobDeadline(JobStopped):
    """The job ran past its deadline."""


def _duration(seconds: float) -> str:
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:g} s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:g} min"
    return f"{minutes / 60:g} h"


class StopToken:
    """One job's stop state. ``cancel`` may be called from any thread; the deadline counts from creation."""

    def __init__(self, deadline_s: Optional[float] = None):
        self.deadline_s = deadline_s
        self.phase: Optional[str] = None
        self._started = time.monotonic()
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def expired(self) -> bool:
        return self.deadline_s is not None and time.monotonic() - self._started >= self.deadline_s

    def stop_requested(self) -> bool:
        return self.cancel_requested or self.expired()

    def _where(self, next_phase: Optional[str]) -> str:
        if next_phase and self.phase:
            return f" after phase {self.phase}, before {next_phase}"
        if next_phase:
            return f" before {next_phase}"
        if self.phase:
            return f" in phase {self.phase}"
        return ""

    def check(self, next_phase: Optional[str] = None) -> None:
        """Raise when a stop was asked for; otherwise enter ``next_phase`` (when given)."""
        if self.cancel_requested:
            raise JobCancelled(f"Cancelled{self._where(next_phase)}.")
        if self.expired():
            raise JobDeadline(f"Stopped: the job passed its deadline of {_duration(self.deadline_s)}"
                              f"{self._where(next_phase)}.")
        if next_phase:
            self.phase = next_phase


def current() -> Optional[StopToken]:
    """The token of the job running on this thread, ``None`` outside a job."""
    return getattr(_local, "token", None)


def bind(token: Optional[StopToken]) -> Optional[StopToken]:
    """Bind ``token`` to this thread; returns the token it replaces (``app/jobs.execute`` binds one per job)."""
    previous = current()
    _local.token = token
    return previous


@contextmanager
def bound(token: Optional[StopToken]) -> Iterator[Optional[StopToken]]:
    """Bind ``token`` to this thread for the block."""
    previous = bind(token)
    try:
        yield token
    finally:
        bind(previous)


def checkpoint(next_phase: Optional[str] = None) -> None:
    """A safe place to stop: raise ``JobCancelled`` / ``JobDeadline`` when asked to, else enter ``next_phase``."""
    token = current()
    if token is not None:
        token.check(next_phase)


def stop_requested() -> bool:
    """True when this thread's job was cancelled or passed its deadline (a cheap check for a pool loop)."""
    token = current()
    return token is not None and token.stop_requested()


def stop_pool(futures) -> None:
    """In a pool loop after ``stop_requested()``: cancel the tickers not yet started, then raise.

    Raised inside the ``with ThreadPoolExecutor`` block, so its exit waits only for the few tickers already
    running, and the scan returns nothing to persist or freeze.
    """
    for fut in futures:
        fut.cancel()
    checkpoint()


__all__ = ["JobStopped", "JobCancelled", "JobDeadline", "StopToken", "current", "bind", "bound", "checkpoint",
           "stop_requested", "stop_pool"]
