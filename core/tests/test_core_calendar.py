"""core.calendar — NYSE closure table, the federal holidays EDGAR keeps, + trade-by date. No network."""
from datetime import date

import pytest

from RevenueChaser.core.calendar import (
    MARKET_HOLIDAYS, federal_holidays, is_federal_holiday, is_trading_day, market_holidays,
    previous_trading_day, next_trading_day, trade_by_date,
)


def test_2026_holiday_table_content():
    h = market_holidays(2026)
    for iso in ("2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
                "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
                "2026-11-26", "2026-12-25"):
        assert date.fromisoformat(iso) in h
    # July 4 2026 is a Saturday, observed Friday July 3.
    assert date(2026, 7, 4).weekday() == 5
    assert date(2026, 7, 3) in h


def test_years_past_table_raise_rather_than_guess():
    with pytest.raises(ValueError):
        market_holidays(max(MARKET_HOLIDAYS) + 1)


def test_is_trading_day_weekend_and_holiday():
    assert is_trading_day(date(2026, 7, 15)) is True
    assert is_trading_day(date(2026, 7, 18)) is False   # Saturday
    assert is_trading_day(date(2026, 7, 3)) is False    # holiday
    assert is_trading_day(date(2026, 7, 6)) is True     # Monday after


def test_previous_and_next_trading_day():
    assert previous_trading_day(date(2026, 7, 15)) == date(2026, 7, 15)
    # 2026-07-03 is a holiday; the latest trading day <= it is 07-02.
    assert previous_trading_day(date(2026, 7, 3)) == date(2026, 7, 2)
    assert next_trading_day(date(2026, 7, 2)) == date(2026, 7, 6)      # holiday Monday gap


def test_trade_by_pm_is_same_day():
    assert trade_by_date(date(2026, 7, 16), False) == date(2026, 7, 16)


def test_trade_by_am_is_previous_session():
    assert trade_by_date(date(2026, 7, 16), True) == date(2026, 7, 15)


def test_trade_by_am_rolls_over_weekend_and_holiday():
    # Monday 2026-07-06 (after July 4 observed on Friday) -> prior close Thursday 07-02.
    assert trade_by_date(date(2026, 7, 6), True) == date(2026, 7, 2)


def test_trade_by_none_inputs_return_none():
    assert trade_by_date(None, True) is None
    assert trade_by_date(date(2026, 7, 16), None) is None
    assert trade_by_date(None, None) is None


# ── New York session helpers (#128: the forecast ledger's "complete session" rule) ──

from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

from RevenueChaser.core import calendar as cal  # noqa: E402


def _utc(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


def test_new_york_zone_resolves_and_tzdata_is_declared():
    """The freeze must not depend on pandas dragging tzdata in (Windows has no system tz database)."""
    from zoneinfo import ZoneInfo
    assert ZoneInfo("America/New_York").key == "America/New_York"
    reqs = (Path(__file__).resolve().parents[2] / "requirements.txt").read_text(encoding="utf-8")
    assert any(line.split(">")[0].split("=")[0].strip() == "tzdata" for line in reqs.splitlines())


def test_session_close_is_1600_new_york_in_both_dst_regimes():
    assert cal.session_close(date(2026, 10, 26)) == _utc(2026, 10, 26, 20, 0)   # EDT, UTC-4
    assert cal.session_close(date(2026, 11, 2)) == _utc(2026, 11, 2, 21, 0)     # EST, UTC-5
    assert cal.session_close(date(2026, 10, 26)).tzinfo is not None


def test_session_close_rejects_a_non_trading_day():
    with pytest.raises(ValueError):
        cal.session_close(date(2026, 10, 24))     # Saturday
    with pytest.raises(ValueError):
        cal.session_close(date(2026, 11, 26))     # Thanksgiving


def test_latest_complete_session_waits_30_minutes_after_the_close():
    # 2026-10-26 closes 20:00Z; the session counts as complete from 20:30Z.
    assert cal.latest_complete_session(_utc(2026, 10, 26, 20, 29, 59)) == date(2026, 10, 23)
    assert cal.latest_complete_session(_utc(2026, 10, 26, 20, 30)) == date(2026, 10, 26)


def test_latest_complete_session_is_the_new_york_date_not_the_utc_date():
    # 00:30Z on Tue 10-27 is still Monday evening in New York: session 10-26, not 10-27.
    assert cal.latest_complete_session(_utc(2026, 10, 27, 0, 30)) == date(2026, 10, 26)
    # Saturday 00:30Z is Friday evening in New York.
    assert cal.latest_complete_session(_utc(2026, 10, 24, 0, 30)) == date(2026, 10, 23)


def test_latest_complete_session_across_us_eu_dst_gap_weeks():
    # Oct 25 - Nov 1 2026: Europe is already on winter time, the US is still on EDT.
    assert cal.latest_complete_session(_utc(2026, 10, 28, 21, 0)) == date(2026, 10, 28)
    assert cal.latest_complete_session(_utc(2026, 10, 29, 3, 0)) == date(2026, 10, 28)
    # After the US change (Nov 1): 21:00Z is 16:00 EST, the close; not complete until 21:30Z.
    assert cal.latest_complete_session(_utc(2026, 11, 2, 21, 0)) == date(2026, 10, 30)
    assert cal.latest_complete_session(_utc(2026, 11, 2, 21, 30)) == date(2026, 11, 2)


def test_latest_complete_session_skips_weekends_and_holidays():
    # Monday morning after the Friday 2026-07-03 holiday: the last complete session is Thursday 07-02.
    assert cal.latest_complete_session(_utc(2026, 7, 6, 13, 0)) == date(2026, 7, 2)
    assert cal.latest_complete_session(_utc(2026, 7, 6, 21, 0)) == date(2026, 7, 6)
    # Thanksgiving Thursday 2026-11-26: Wednesday's session is the latest.
    assert cal.latest_complete_session(_utc(2026, 11, 26, 23, 0)) == date(2026, 11, 25)


def test_latest_complete_session_uses_the_clock_seam_by_default(monkeypatch):
    monkeypatch.setattr(cal, "_now", lambda: _utc(2026, 10, 26, 22, 0))
    assert cal.latest_complete_session() == date(2026, 10, 26)
    assert cal.in_us_session_window() is False


def test_latest_complete_session_raises_past_the_holiday_table():
    with pytest.raises(ValueError):
        cal.latest_complete_session(_utc(2029, 3, 1, 22, 0))


def test_in_us_session_window_is_0930_to_1630_new_york():
    # EDT: 13:30Z = 09:30 ET, 20:30Z = 16:30 ET.
    assert cal.in_us_session_window(_utc(2026, 10, 26, 13, 29)) is False
    assert cal.in_us_session_window(_utc(2026, 10, 26, 13, 30)) is True
    assert cal.in_us_session_window(_utc(2026, 10, 26, 20, 29)) is True
    assert cal.in_us_session_window(_utc(2026, 10, 26, 20, 30)) is False
    # EST (after Nov 1): the same wall-clock window is one hour later in UTC.
    assert cal.in_us_session_window(_utc(2026, 11, 2, 14, 15)) is False   # 09:15 ET
    assert cal.in_us_session_window(_utc(2026, 11, 2, 14, 45)) is True    # 09:45 ET
    assert cal.in_us_session_window(_utc(2026, 11, 2, 21, 15)) is True    # 16:15 ET
    assert cal.in_us_session_window(_utc(2026, 11, 2, 21, 45)) is False   # 16:45 ET


def test_in_us_session_window_is_false_on_weekends_and_holidays():
    assert cal.in_us_session_window(_utc(2026, 10, 24, 15, 0)) is False   # Saturday
    assert cal.in_us_session_window(_utc(2026, 11, 26, 15, 0)) is False   # Thanksgiving


def test_in_us_session_window_raises_past_the_table():
    """A weekday past the holiday table cannot be called a trading day without guessing."""
    with pytest.raises(ValueError):
        cal.in_us_session_window(_utc(2029, 3, 1, 15, 0))


def test_bars_complete_needs_the_30_minute_margin():
    close = cal.session_close(date(2026, 10, 26))
    assert cal.bars_complete(date(2026, 10, 26), (close + timedelta(minutes=29, seconds=59)).timestamp()) is False
    assert cal.bars_complete(date(2026, 10, 26), (close + timedelta(minutes=30)).timestamp()) is True
    # an intraday write (11:35 ET) of that session's bar is incomplete
    assert cal.bars_complete(date(2026, 10, 26), _utc(2026, 10, 26, 15, 35).timestamp()) is False
    # a later re-download still counts
    assert cal.bars_complete(date(2026, 10, 26), _utc(2026, 10, 28, 3, 0).timestamp()) is True


# ── Federal holidays: the days EDGAR is closed ─────────────────────────────────────────────────────────────

def test_federal_holidays_follow_the_statute_and_its_weekend_shift():
    h = federal_holidays(2026)
    for iso in ("2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25", "2026-06-19",
                "2026-07-03", "2026-09-07", "2026-10-12", "2026-11-11", "2026-11-26", "2026-12-25"):
        assert date.fromisoformat(iso) in h, iso
    assert len(h) == 11
    # Columbus Day and Veterans Day close EDGAR although the NYSE trades.
    assert date(2026, 10, 12) not in market_holidays(2026)
    assert date(2026, 11, 11) not in market_holidays(2026)
    # Good Friday closes the NYSE and not the federal offices: EDGAR published the 2026-04-03 index.
    assert date(2026, 4, 3) in market_holidays(2026) and date(2026, 4, 3) not in h


def test_a_fixed_date_holiday_on_a_saturday_is_observed_the_friday_before():
    # 2028-01-01 is a Saturday, observed Friday 2027-12-31; it is not a 2028 closure.
    assert date(2028, 1, 1).weekday() == 5
    assert date(2027, 12, 31) in federal_holidays(2027)
    assert date(2028, 1, 1) not in federal_holidays(2028)
    # 2034-01-01 is a Sunday, observed Monday 2034-01-02.
    assert date(2034, 1, 1).weekday() == 6
    assert date(2034, 1, 2) in federal_holidays(2034) and date(2034, 1, 1) not in federal_holidays(2034)


def test_a_closure_by_executive_order_is_in_the_table():
    """Measured against sec.gov on 2026-10-30: the daily index of each of these days is absent."""
    for iso in ("2025-01-09", "2025-12-24", "2025-12-26"):
        assert is_federal_holiday(date.fromisoformat(iso)), iso


def test_is_federal_holiday_of_an_ordinary_weekday():
    assert is_federal_holiday(date(2026, 10, 13)) is False
    assert is_federal_holiday(date(2026, 10, 12)) is True
