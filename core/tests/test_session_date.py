"""ADR-3 / #283: one session date. Rule B maps a run time to its NY session; rule C keeps the
calendar at 2025-2028 (raises outside, never guesses). No network."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from RevenueChaser.core.calendar import (
    QUALITY_COMPLETE, QUALITY_INTRADAY, SessionStamp, session_for, to_session_date,
)

NY = ZoneInfo("America/New_York")
AMS = ZoneInfo("Europe/Amsterdam")


def ny(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def test_weekday_after_close_is_that_day_complete():
    s = session_for(ny(2026, 7, 15, 16, 30))
    assert s == SessionStamp(date(2026, 7, 15), QUALITY_COMPLETE)


def test_weekday_inside_window_is_that_day_intraday():
    assert session_for(ny(2026, 7, 15, 9, 30)) == SessionStamp(date(2026, 7, 15), QUALITY_INTRADAY)
    assert session_for(ny(2026, 7, 15, 16, 29)).quality == QUALITY_INTRADAY
    assert session_for(ny(2026, 7, 15, 16, 30)).quality == QUALITY_COMPLETE


def test_evening_run_is_still_that_new_york_day_though_utc_has_rolled():
    # Mon 21:00 ET = Tue 01:00 UTC
    assert session_for(ny(2026, 7, 13, 21, 0)) == SessionStamp(date(2026, 7, 13), QUALITY_COMPLETE)


def test_before_open_maps_to_previous_session():
    # Monday 09:29 -> Friday, complete (the Monday session has not started)
    assert session_for(ny(2026, 7, 13, 9, 29)) == SessionStamp(date(2026, 7, 10), QUALITY_COMPLETE)
    assert session_for(ny(2026, 7, 15, 0, 0)).session_date == date(2026, 7, 14)


def test_saturday_run_is_friday():
    assert session_for(ny(2026, 7, 18, 11, 0)) == SessionStamp(date(2026, 7, 17), QUALITY_COMPLETE)


def test_holiday_run_is_previous_session_not_the_holiday():
    # 2026-07-03 is the observed Independence Day: Thursday 07-02
    assert session_for(ny(2026, 7, 3, 12, 0)).session_date == date(2026, 7, 2)
    # Monday after a Friday holiday + weekend, before open
    assert session_for(ny(2026, 7, 6, 8, 0)).session_date == date(2026, 7, 2)


def test_early_close_day_is_treated_as_a_full_day():
    # 2026-11-27 (day after Thanksgiving) closes 13:00 ET; the module's documented
    # conservative rule keeps 14:00 intraday and 16:30 complete.
    assert session_for(ny(2026, 11, 27, 14, 0)).quality == QUALITY_INTRADAY
    assert session_for(ny(2026, 11, 27, 16, 30)) == SessionStamp(date(2026, 11, 27), QUALITY_COMPLETE)


def test_amsterdam_run_uses_new_york_date_not_amsterdam_date():
    # Tue 2026-07-14 00:30 Amsterdam (CEST) = Mon 18:30 ET -> Monday's session
    run = datetime(2026, 7, 14, 0, 30, tzinfo=AMS)
    assert session_for(run) == SessionStamp(date(2026, 7, 13), QUALITY_COMPLETE)
    # Sat 2026-07-18 03:00 Amsterdam = Fri 21:00 ET -> Friday
    assert session_for(datetime(2026, 7, 18, 3, 0, tzinfo=AMS)).session_date == date(2026, 7, 17)


def test_us_eu_dst_gap_weeks_follow_new_york_not_a_fixed_offset():
    # Late Oct 2026: EU back on CET (25 Oct), US still EDT until 1 Nov -> 5h apart, not 6h.
    # Wed 28 Oct 21:00 Amsterdam = 16:00 EDT: still inside the window.
    assert session_for(datetime(2026, 10, 28, 21, 0, tzinfo=AMS)).quality == QUALITY_INTRADAY
    # 22:00 Amsterdam = 17:00 EDT: complete.
    assert session_for(datetime(2026, 10, 28, 22, 0, tzinfo=AMS)).quality == QUALITY_COMPLETE
    # Regular weeks (6h): 22:00 Amsterdam = 16:00 ET -> still intraday.
    assert session_for(datetime(2026, 7, 15, 22, 0, tzinfo=AMS)).quality == QUALITY_INTRADAY
    # Spring gap: 10 Mar 2026 (US on EDT since 8 Mar, EU on CET until 29 Mar).
    assert session_for(datetime(2026, 3, 10, 21, 0, tzinfo=AMS)).quality == QUALITY_INTRADAY
    assert session_for(datetime(2026, 3, 10, 22, 0, tzinfo=AMS)).quality == QUALITY_COMPLETE


def test_us_fall_back_day_is_correct():
    # 1 Nov 2026 is a Sunday (fall back); Monday 2 Nov 14:00 EST = 19:00 UTC.
    run = datetime(2026, 11, 2, 19, 0, tzinfo=timezone.utc)
    assert session_for(run) == SessionStamp(date(2026, 11, 2), QUALITY_INTRADAY)
    # Fri 30 Oct 21:00 UTC = 17:00 EDT -> complete Friday
    assert session_for(datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)).session_date == date(2026, 10, 30)


def test_epoch_seconds_are_utc():
    ts = datetime(2026, 7, 17, 21, 0, tzinfo=timezone.utc).timestamp()   # Fri 17:00 ET
    assert session_for(ts) == SessionStamp(date(2026, 7, 17), QUALITY_COMPLETE)


def test_bool_is_not_epoch_seconds():
    with pytest.raises(ValueError):
        session_for(True)


def test_naive_datetime_is_refused():
    with pytest.raises(ValueError):
        session_for(datetime(2026, 7, 15, 12, 0))


def test_outside_calendar_raises_rather_than_guesses():
    with pytest.raises(ValueError):
        session_for(ny(2029, 3, 5, 12, 0))
    with pytest.raises(ValueError):
        session_for(ny(2024, 6, 5, 18, 0))
    # 2025-01-02 pre-open needs 2024-12-31: outside the table, so it raises
    with pytest.raises(ValueError):
        session_for(ny(2025, 1, 2, 8, 0))


def test_first_and_last_covered_sessions():
    assert session_for(ny(2025, 1, 2, 17, 0)).session_date == date(2025, 1, 2)
    assert session_for(ny(2028, 12, 29, 17, 0)).session_date == date(2028, 12, 29)


# ── read helper: old run-time columns -> session ────────────────────────────

def test_to_session_date_accepts_epoch_iso_text_and_dates():
    sat = datetime(2026, 7, 18, 15, 0, tzinfo=timezone.utc)
    assert to_session_date(sat.timestamp()) == date(2026, 7, 17)
    assert to_session_date(int(sat.timestamp())) == date(2026, 7, 17)
    assert to_session_date("2026-07-18T11:00:00-04:00") == date(2026, 7, 17)
    assert to_session_date("2026-07-18T15:00:00Z") == date(2026, 7, 17)
    assert to_session_date(sat) == date(2026, 7, 17)


def test_to_session_date_date_only_values_roll_back_to_a_session():
    assert to_session_date("2026-07-18") == date(2026, 7, 17)     # Saturday
    assert to_session_date(date(2026, 7, 3)) == date(2026, 7, 2)  # holiday
    assert to_session_date("2026-07-15") == date(2026, 7, 15)     # a session stays itself


def test_to_session_date_uses_the_run_time_not_a_text_prefix():
    # UTC text whose first 10 chars are Saturday but is Friday evening in New York
    assert to_session_date("2026-07-18T01:00:00+00:00") == date(2026, 7, 17)
    # and one whose prefix is Friday but is Saturday morning... in UTC; NY is still Friday
    assert to_session_date("2026-07-17T23:00:00+00:00") == date(2026, 7, 17)


def test_to_session_date_naive_text_is_read_as_new_york_time():
    assert to_session_date("2026-07-15 17:00:00") == date(2026, 7, 15)
    assert to_session_date("2026-07-15 08:00:00") == date(2026, 7, 14)
    # 13:00 is 17:00 UTC; read as UTC it would be 09:00 ET, before the open, and give 07-14
    assert to_session_date("2026-07-15 13:00:00") == date(2026, 7, 15)
    assert to_session_date(datetime(2026, 7, 15, 13, 0)) == date(2026, 7, 15)


def test_to_session_date_none_and_blank_are_none():
    assert to_session_date(None) is None
    assert to_session_date("") is None


def test_to_session_date_garbage_raises():
    with pytest.raises(ValueError):
        to_session_date("not a date")
    with pytest.raises(ValueError):
        to_session_date(True)
