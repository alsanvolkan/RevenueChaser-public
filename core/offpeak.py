"""DeepSeek off-peak windows (UTC) — a pure, testable gate.

DeepSeek bills peak hours at 2x the off-peak rate. Peak is **Mon–Fri
01:00–04:00 and 06:00–10:00 UTC**; Saturday and Sunday are off-peak all day.
The windows are defined in UTC on purpose: a local-timezone definition would
shift with daylight saving and silently drift into a billed window.

The module is deliberately I/O-free. Callers **stop** when ``is_off_peak`` is
False (and resume on the next off-peak run); they never sleep, because the
dashboard has a single job worker that must not be blocked for hours.

See ``docs/llm_offpeak.md``.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

# Inclusive start, exclusive end, in UTC. Weekdays only (Mon–Fri).
PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))

_WEEKEND = {5, 6}  # datetime.weekday(): Sat=5, Sun=6


def _as_utc(now: datetime | None) -> datetime:
    """Coerce ``now`` to an aware UTC datetime (naive input is read as UTC)."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def is_off_peak(now: datetime | None = None) -> bool:
    """True when DeepSeek is billed at the off-peak rate.

    Weekends are always off-peak; weekdays are off-peak outside the two peak
    windows. ``now`` defaults to the current UTC time.
    """
    dt = _as_utc(now)
    if dt.weekday() in _WEEKEND:
        return True
    return not any(start <= dt.hour < end for start, end in PEAK_WINDOWS_UTC)


def next_off_peak(now: datetime | None = None) -> datetime:
    """The next UTC instant at which :func:`is_off_peak` is True.

    Used only to tell the user when to re-run; it does not sleep. When already
    off-peak, returns ``now`` unchanged.
    """
    dt = _as_utc(now)
    if is_off_peak(dt):
        return dt
    for start, end in PEAK_WINDOWS_UTC:
        if start <= dt.hour < end:
            return dt.replace(hour=end, minute=0, second=0, microsecond=0)
    return dt  # unreachable: peak hours are always inside a window


def local_peak_windows(tz) -> list[tuple[time, time]]:
    """Peak windows expressed in ``tz`` for display (informational only).

    A fixed Monday is used as the reference, so the result does not account for
    a daylight-saving change mid-year — say "≈" when showing it.
    """
    monday = datetime(2026, 1, 5, tzinfo=timezone.utc)  # a Monday
    out = []
    for start, end in PEAK_WINDOWS_UTC:
        s = (monday + timedelta(hours=start)).astimezone(tz).time()
        e = (monday + timedelta(hours=end)).astimezone(tz).time()
        out.append((s, e))
    return out


def describe(now: datetime | None = None) -> str:
    """One-line status for job messages / logs."""
    dt = _as_utc(now)
    if is_off_peak(dt):
        return "DeepSeek off-peak (billing at half rate)."
    nxt = next_off_peak(dt)
    return (
        f"DeepSeek peak hours — LLM disabled. Next off-peak: "
        f"{nxt.strftime('%Y-%m-%d %H:%M')} UTC."
    )
