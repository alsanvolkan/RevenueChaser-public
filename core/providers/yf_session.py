"""One yfinance session reset after Yahoo refuses the crumb (U1, #402).

yfinance keeps one cookie and one crumb for the whole process (``yfinance.data.YfData``, a singleton every thread
shares), and saves the cookie on disk between runs. When Yahoo stops accepting the crumb it answers HTTP 401 "Invalid
Crumb", and the process keeps sending it. On 2026-10-14 that came at 21:29Z inside RC-Screeners-Daily, and every
later bar refetch of that process failed (#402).

yfinance does not raise that 401: its quote scraper logs it on the ``yfinance`` logger, and a ``history`` read turns a
refused request into an empty frame with a generic message. So this module puts a filter on that logger that notes
when a 401 was logged (it lets every record through), and ``recover(started)`` is what a reader runs after a failed
read that began at ``started``. When a 401 was seen since the last reset, or the read's own exception is one, it drops
the cookie and the crumb, at most once per ``COOLDOWN_S`` for the whole process, and says whether a retry is worth it.
A failure with no 401 (a rate limit, a missing name) resets nothing.
"""
from __future__ import annotations

import contextlib
import logging
import re
import threading
import time

logger = logging.getLogger(__name__)

COOLDOWN_S = 600.0       # a reset that did not help is not repeated for ten minutes: never a stream of new sessions
_AUTH = re.compile(r"HTTP Error 401|\b401 Client Error|status_code = 401|Invalid Crumb|Unauthorized", re.IGNORECASE)
_COOKIE_KEYS = ("curlCffi", "basic", "csrf")    # the keys yfinance has saved its cookie under, by version

# When a 401 was last seen and when the session was last reset, on ``clock()``. Only ``recover`` resets, under
# ``_DECIDE``; the log filter writes "seen" without that lock, because yfinance may log while it holds its own cookie
# lock, which ``_reset`` takes.
_STATE = {"seen": float("-inf"), "reset": float("-inf")}
_DECIDE = threading.Lock()


def clock() -> float:
    """The clock of this module: monotonic, so a test's frozen wall clock does not stop it."""
    return time.monotonic()


def is_auth_error(text) -> bool:
    """True when ``text`` (a log message or an exception) is Yahoo's 401."""
    return text is not None and bool(_AUTH.search(str(text)))


class _Watch(logging.Filter):
    """Notes a 401 in a ``yfinance`` log record; never drops one."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if is_auth_error(record.getMessage()):
                _STATE["seen"] = clock()
        except Exception:  # noqa: BLE001 - a log line must never fail because of this filter
            pass
        return True


def watch() -> None:
    """Put the 401 filter on the ``yfinance`` logger, once per process."""
    log = logging.getLogger("yfinance")
    if not any(isinstance(f, _Watch) for f in log.filters):
        log.addFilter(_Watch())


def recover(started: float, error: BaseException | None = None) -> bool:
    """After a failed read that began at ``started`` (``clock()``): True when a retry may now succeed.

    True when the session was reset after ``started``, by another thread or here. Here means: a 401 is pending (seen
    after the last reset, or ``error`` is one) and the last reset is at least ``COOLDOWN_S`` old. Otherwise False.
    """
    if is_auth_error(error):
        _STATE["seen"] = clock()
    with _DECIDE:
        if _STATE["reset"] >= started:
            return True
        if _STATE["seen"] <= _STATE["reset"] or clock() - _STATE["reset"] < COOLDOWN_S:
            return False
        cleared = _reset()
        _STATE["reset"] = clock()
    logger.warning("yfinance: Yahoo refused the session (HTTP 401); reset it (%s) and retrying",
                   ", ".join(cleared) or "nothing to clear")
    return True


def _reset() -> list[str]:
    """Drop yfinance's cookie and crumb, in memory and on disk, so its next request mints new ones.

    Best effort over yfinance versions: a part the installed version lacks is skipped. Returns what was cleared.
    """
    cleared: list[str] = []
    try:
        from yfinance import data as yf_data
        shared = yf_data.YfData()                 # the process's one instance
    except Exception as exc:  # noqa: BLE001
        logger.warning("yfinance: no shared session to reset (%s)", exc)
        shared = None
    if shared is not None:
        lock = getattr(shared, "_cookie_lock", None)
        with lock if lock is not None else contextlib.nullcontext():
            for name in ("_crumb", "_cookie"):
                if hasattr(shared, name):
                    setattr(shared, name, None)
                    cleared.append(name.strip("_"))
            jar = getattr(getattr(shared, "_session", None), "cookies", None)
            with contextlib.suppress(Exception):
                jar.clear()
                cleared.append("session cookies")
    try:
        from yfinance import cache as yf_cache
        saved = yf_cache.get_cookie_cache()
        for key in _COOKIE_KEYS:
            saved.store(key, None)                # None deletes the saved cookie of that key
        cleared.append("saved cookie")
    except Exception as exc:  # noqa: BLE001
        logger.debug("yfinance: the saved cookie was not dropped (%s)", exc)
    return cleared


watch()
