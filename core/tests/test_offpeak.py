"""Off-peak gate — the DeepSeek billing windows (see core/offpeak.py)."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from RevenueChaser.core import offpeak

UTC = timezone.utc
TR = timezone(timedelta(hours=3))  # UTC+3, no DST — a fixed local reference


def _dt(y, m, d, hh=0, mm=0, tz=UTC):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


# 2026-09-14 is a Monday; 19/20 are Sat/Sun.
MON, SAT, SUN = 14, 19, 20
M = 9


@pytest.mark.parametrize("hour,expected", [
    (0, True),    # 00:59 zone start — before the first window
    (1, False),   # peak start (inclusive)
    (2, False),
    (3, False),
    (4, True),    # peak end (exclusive)
    (5, True),
    (6, False),   # second peak start
    (9, False),
    (10, True),   # second peak end
    (12, True),
    (23, True),
])
def test_weekday_boundaries(hour, expected):
    assert offpeak.is_off_peak(_dt(2026, M, MON, hour, 30)) is expected


@pytest.mark.parametrize("day", [SAT, SUN])
@pytest.mark.parametrize("hour", [0, 1, 2, 6, 9, 12, 23])
def test_weekends_are_always_off_peak(day, hour):
    assert offpeak.is_off_peak(_dt(2026, M, day, hour)) is True


def test_exact_window_edges_are_half_open():
    # 01:00 is peak, 00:59 is not; 04:00 is off, 03:59 is not.
    assert offpeak.is_off_peak(_dt(2026, M, MON, 1, 0)) is False
    assert offpeak.is_off_peak(_dt(2026, M, MON, 0, 59)) is True
    assert offpeak.is_off_peak(_dt(2026, M, MON, 3, 59)) is False
    assert offpeak.is_off_peak(_dt(2026, M, MON, 4, 0)) is True


def test_naive_datetime_is_read_as_utc():
    assert offpeak.is_off_peak(datetime(2026, M, MON, 2, 0)) is False  # naive peak
    assert offpeak.is_off_peak(datetime(2026, M, MON, 5, 0)) is True


def test_non_utc_aware_datetime_is_converted():
    # 06:00 UTC+3 == 03:00 UTC (peak) -> not off-peak
    assert offpeak.is_off_peak(_dt(2026, M, MON, 6, 0, tz=TR)) is False
    # 07:00 UTC+3 == 04:00 UTC (window end) -> off-peak
    assert offpeak.is_off_peak(_dt(2026, M, MON, 7, 0, tz=TR)) is True


def test_next_off_peak_inside_first_window():
    assert offpeak.next_off_peak(_dt(2026, M, MON, 2, 0)) == _dt(2026, M, MON, 4, 0)


def test_next_off_peak_inside_second_window():
    assert offpeak.next_off_peak(_dt(2026, M, MON, 7, 30)) == _dt(2026, M, MON, 10, 0)


def test_next_off_peak_when_already_off_peak_is_now():
    now = _dt(2026, M, MON, 11, 0)
    assert offpeak.next_off_peak(now) == now


def test_next_off_peak_on_weekend_is_now():
    now = _dt(2026, M, SAT, 2, 0)
    assert offpeak.next_off_peak(now) == now


def test_local_peak_windows_utc_and_utc_plus_3():
    assert offpeak.local_peak_windows(UTC) == [(time(1, 0), time(4, 0)),
                                               (time(6, 0), time(10, 0))]
    assert offpeak.local_peak_windows(TR) == [(time(4, 0), time(7, 0)),
                                              (time(9, 0), time(13, 0))]


def test_describe_off_peak_and_peak():
    assert "off-peak" in offpeak.describe(_dt(2026, M, MON, 11, 0))
    msg = offpeak.describe(_dt(2026, M, MON, 2, 0))
    assert "peak" in msg.lower()
    assert "2026-09-14 04:00 UTC" in msg


def test_module_has_no_io():
    """The gate must stay pure — no httpx/network/rawstore imports."""
    import inspect
    src = inspect.getsource(offpeak)
    for banned in ("httpx", "requests", "rawstore", "socket"):
        assert banned not in src
