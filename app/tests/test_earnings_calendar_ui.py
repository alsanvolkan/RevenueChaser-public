"""The earnings picker selects individual weekdays, not whole ISO weeks.

The old week-row picker always fetched a contiguous Mon–Sun span, so there was
no way to load e.g. the 7th, 9th and 11th. The replacement is a month day-grid:
toggle weekdays, click the month name for every weekday, then Load selected.
"""
from __future__ import annotations

import re

import pytest

from RevenueChaser.app import dashboard


@pytest.fixture(scope="module")
def index_html() -> str:
    return dashboard.app.test_client().get("/").get_data(as_text=True)


def _fn_body(html: str, name: str) -> str:
    """Code of one top-level JS function, up to the next declaration.

    Line comments are stripped: they legitimately mention the old buggy call by
    name to explain why it is gone, which would defeat an absence assertion.
    """
    start = html.index(f"function {name}(")
    rest = html[start + 1:]
    end = rest.find("\nfunction ")
    body = rest[:end if end != -1 else len(rest)]
    return "\n".join(re.sub(r"//.*$", "", line) for line in body.splitlines())


class TestDayGrid:
    def test_the_window_still_spans_both_sides_of_today(self, index_html):
        body = _fn_body(index_html, "earnWindowMonths")
        assert "EARN_WEEKS_BACK" in body
        assert "EARN_WEEKS_FWD" in body

    def test_forward_weeks_are_configured_and_non_zero(self, index_html):
        m = re.search(r"var EARN_WEEKS_FWD = (\d+);", index_html)
        assert m, "EARN_WEEKS_FWD must be declared"
        assert int(m.group(1)) > 0, "an earnings calendar must reach the week ahead"

    def test_past_weeks_are_still_available(self, index_html):
        m = re.search(r"var EARN_WEEKS_BACK = (\d+);", index_html)
        assert m and int(m.group(1)) > 0

    def test_today_is_marked_on_the_day_cell(self, index_html):
        assert "cal-day" in index_html
        assert "today" in _fn_body(index_html, "buildEarningsCalendar")
        assert ".cal-day.today" in index_html

    def test_weekends_are_not_selectable(self, index_html):
        body = _fn_body(index_html, "buildEarningsCalendar")
        assert "weekend" in body
        assert "toggleEarnDay" in body
        assert "toggleEarnMonth" in body

    def test_load_posts_dates_not_a_week_span(self, index_html):
        body = _fn_body(index_html, "runEarningsRange")
        assert "dates" in body
        assert "days.join" in body
        assert "EARN_START" not in body

    def test_week_row_click_handler_is_gone(self, index_html):
        assert "function clickEarningsWeek(" not in index_html
        assert "cal-week-row" not in _fn_body(index_html, "buildEarningsCalendar")

    def test_load_selected_button_exists(self, index_html):
        assert "btn-earnings-load-selected" in _fn_body(index_html, "buildEarningsCalendar")

    def test_load_selected_is_pinned_before_month_grids(self, index_html):
        body = _fn_body(index_html, "buildEarningsCalendar")
        assert body.index("btn-earnings-load-selected") < body.index("earnWindowMonths")

    def test_opening_the_picker_scrolls_to_today(self, index_html):
        body = _fn_body(index_html, "toggleCal")
        assert "earnings-cal-popover" in body
        assert "cal-day.today" in body
        assert "scrollIntoView" in body

    def test_topbar_load_selected_button_is_present_and_wired(self, index_html):
        # A prominent, always-in-the-topbar fetch button, not just the one buried
        # at the bottom of the popover.
        assert 'id="btn-earnings-load"' in index_html
        assert "getElementById('btn-earnings-load')" in index_html
        assert "addEventListener('click', runEarningsRange)" in index_html

    def test_topbar_button_visibility_tracks_the_selection(self, index_html):
        body = _fn_body(index_html, "updateEarningsRangeLabel")
        assert "btn-earnings-load" in body
        assert "style.display = 'none'" in body
        assert "days.length" in body


class TestNoTimezoneDrift:
    def test_iso_week_info_formats_dates_locally(self, index_html):
        body = _fn_body(index_html, "isoWeekInfo")
        assert "fmtLocal(monday)" in body and "fmtLocal(sunday)" in body

    def test_iso_week_info_no_longer_uses_toisostring(self, index_html):
        assert "toISOString" not in _fn_body(index_html, "isoWeekInfo")

    def test_the_day_grid_formats_locally(self, index_html):
        assert "toISOString" not in _fn_body(index_html, "buildEarningsCalendar")
        assert "fmtLocal" in _fn_body(index_html, "buildEarningsCalendar")


class TestEmptyRunFeedback:
    def test_a_job_message_is_shown_to_the_user(self, index_html):
        """A run can succeed with nothing to show; the UI must say so."""
        body = _fn_body(index_html, "poll")
        assert "j.message" in body
