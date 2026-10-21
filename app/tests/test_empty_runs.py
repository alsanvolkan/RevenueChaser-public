"""A run that finds nothing must SAY so — and still write its artifact.

The old ``run_earnings_range`` returned early when the calendar was empty: the
job reported success, the stable HTML was never written, and the tab silently
did not change. "Load Earnings Today" on a Saturday looked like a broken button.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage


@pytest.fixture
def latest(tmp_path, monkeypatch):
    """Redirect the stable v1 artifact into a temp dir."""
    path = tmp_path / "earnings_radar_latest.html"
    monkeypatch.setattr(runners, "EARNINGS_LATEST", path)
    return path


def _no_events(monkeypatch):
    monkeypatch.setattr(runners, "_fetch_calendar_range", lambda uw, s, e: ([], {}))


class TestEmptyEarningsRange:
    def test_signals_the_caller_instead_of_reporting_plain_success(self, latest, monkeypatch):
        _no_events(monkeypatch)
        with pytest.raises(JobMessage) as exc:
            runners.run_earnings_range(None, date(2026, 9, 5), date(2026, 9, 5))
        assert "No earnings events" in exc.value.message

    def test_writes_the_artifact_so_the_tab_visibly_updates(self, latest, monkeypatch):
        _no_events(monkeypatch)
        with pytest.raises(JobMessage):
            runners.run_earnings_range(None, date(2026, 9, 5), date(2026, 9, 5))
        assert latest.exists()
        html = latest.read_text(encoding="utf-8")
        assert "No earnings events scheduled for 2026-09-05" in html
        assert "Earnings Radar" in html

    def test_weekend_says_it_is_a_weekend(self, latest, monkeypatch):
        _no_events(monkeypatch)
        with pytest.raises(JobMessage):
            runners.run_earnings_range(None, date(2026, 9, 5), date(2026, 9, 5))
        assert "weekend" in latest.read_text(encoding="utf-8")

    def test_weekday_suggests_checking_the_range_or_the_key(self, latest, monkeypatch):
        _no_events(monkeypatch)
        with pytest.raises(JobMessage):
            runners.run_earnings_range(None, date(2026, 9, 3), date(2026, 9, 3))
        html = latest.read_text(encoding="utf-8")
        assert "weekend" not in html
        assert "Unusual Whales key" in html

    def test_a_range_is_reported_as_a_range(self, latest, monkeypatch):
        _no_events(monkeypatch)
        with pytest.raises(JobMessage) as exc:
            runners.run_earnings_range(None, date(2026, 9, 7), date(2026, 9, 11))
        assert "2026-09-07 → 2026-09-11" in exc.value.message

    def test_stale_artifact_is_overwritten_not_left_in_place(self, latest, monkeypatch):
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text("<html>OLD BOARD WITH 40 TICKERS</html>", encoding="utf-8")
        _no_events(monkeypatch)
        with pytest.raises(JobMessage):
            runners.run_earnings_range(None, date(2026, 9, 5), date(2026, 9, 5))
        assert "OLD BOARD" not in latest.read_text(encoding="utf-8")


class TestNoEventsText:
    def test_weekend_hint_only_for_a_single_weekend_day(self):
        _, hint = runners._no_events_text(date(2026, 9, 5), date(2026, 9, 5))
        assert "weekend" in hint
        # A range that merely spans a weekend is not itself a weekend problem.
        _, hint = runners._no_events_text(date(2026, 9, 4), date(2026, 9, 7))
        assert "weekend" not in hint

    def test_sunday_counts_as_a_weekend(self):
        _, hint = runners._no_events_text(date(2026, 9, 6), date(2026, 9, 6))
        assert "weekend" in hint

    def test_sparse_days_are_listed_not_a_range(self):
        msg, _ = runners._no_events_text_days([date(2026, 9, 7), date(2026, 9, 9)])
        assert "2026-09-07, 2026-09-09" in msg
        assert "→" not in msg

    def test_contiguous_days_still_use_a_range(self):
        msg, _ = runners._no_events_text_days([date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9)])
        assert "2026-09-07 → 2026-09-09" in msg


class TestCoerceEarningsDays:
    def test_dates_string_is_sorted_unique(self):
        assert runners._coerce_earnings_days(dates="2026-09-11,2026-09-07,2026-09-07") == [
            date(2026, 9, 7), date(2026, 9, 11),
        ]

    def test_start_end_expands_a_contiguous_span(self):
        assert runners._coerce_earnings_days(start=date(2026, 9, 7), end=date(2026, 9, 9)) == [
            date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9),
        ]


class TestFetchCalendarDays:
    def test_only_requested_weekdays_are_fetched(self):
        seen = []

        class UW:
            def calendar_day_raw(self, d, session):
                seen.append((d, session))
                return []

        days = [date(2026, 9, 7), date(2026, 9, 9), date(2026, 9, 11)]
        runners._fetch_calendar_days(UW(), days)
        assert sorted({d for d, _ in seen}) == days
        assert len(seen) == 6

    def test_weekends_in_the_list_are_skipped(self):
        seen = []

        class UW:
            def calendar_day_raw(self, d, session):
                seen.append(d)
                return []

        runners._fetch_calendar_days(UW(), [date(2026, 9, 5), date(2026, 9, 7)])
        assert seen == [date(2026, 9, 7), date(2026, 9, 7)]


class TestEmptyDiscreteDates:
    def test_sparse_empty_run_lists_the_days(self, latest, monkeypatch):
        monkeypatch.setattr(runners, "_fetch_calendar_days", lambda uw, days: ([], {}))
        with pytest.raises(JobMessage) as exc:
            runners.run_earnings_range(None, dates=[date(2026, 9, 7), date(2026, 9, 9)])
        assert "2026-09-07, 2026-09-09" in exc.value.message
        assert "→" not in exc.value.message


class TestEmptyBoardRender:
    def test_is_valid_standalone_html_with_a_timestamp(self, tmp_path):
        p = runners._write_empty_board(tmp_path / "b.html", "T", "M", "H")
        html = p.read_text(encoding="utf-8")
        assert html.startswith("<!DOCTYPE html>")
        assert "<title>T</title>" in html
        assert "M" in html and "H" in html
        assert "Generated " in html

    def test_creates_missing_parent_directories(self, tmp_path):
        p = runners._write_empty_board(tmp_path / "deep" / "x" / "b.html", "T", "M")
        assert p.exists()
