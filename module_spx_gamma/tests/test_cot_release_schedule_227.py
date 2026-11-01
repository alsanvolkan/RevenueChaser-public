"""#227: a COT report counts as released on CFTC's own release day, read from the stored schedule; a report the
schedule does not cover keeps the report + ``RELEASE_DAYS`` estimate and the board says it is one.

November 2026: Veterans Day moves the 10 Nov report to Monday 16 Nov, and Thanksgiving the 24 Nov one to Monday
30 Nov (CFTC's page, fetched 1 Nov 2026). The old rule showed each of them on the Friday before.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.module_spx_gamma import store_reads
from RevenueChaser.rcdata import needs

from . import rcdata_fixture as fx

REPORTS = (date(2026, 11, 3), date(2026, 11, 10), date(2026, 11, 17), date(2026, 11, 24))
RELEASES = (date(2026, 11, 6), date(2026, 11, 16), date(2026, 11, 20), date(2026, 11, 30))


@pytest.fixture
def november(rcdata_market):
    fx.serve(cot_reports=REPORTS, releases=RELEASES, series_end=date(2026, 11, 30))


@pytest.mark.parametrize("day, report, released", [
    (date(2026, 11, 13), "2026-11-03", "2026-11-06"),     # the 10 Nov report is out on Mon 16 Nov, not Fri 13
    (date(2026, 11, 16), "2026-11-10", "2026-11-16"),
    (date(2026, 11, 27), "2026-11-17", "2026-11-20"),     # the issue's example: 24 Nov is out on Mon 30 Nov
    (date(2026, 11, 30), "2026-11-24", "2026-11-30"),
])
def test_a_report_counts_from_cftcs_release_day(november, day, report, released):
    out = store_reads.cot_tff(day)
    cot = out["payload"]
    assert (cot["report_date"], cot["release_date"], cot["release_estimated"]) == (report, released, False)
    assert f"released {released} (CFTC schedule)" in out["label"]


def test_a_report_the_schedule_does_not_cover_keeps_the_estimate_and_says_so(rcdata_market):
    out = store_reads.cot_tff(fx.LAST)
    assert out["payload"]["release_estimated"] is True
    assert "released ~2026-10-23 (report + 3 days; holiday weeks can differ)" in out["label"]


def test_the_schedule_is_read_through_its_own_need(rcdata_market):
    need = needs.NEEDS["market.cot_release"]
    assert need.table == "market_series" and need.filter_values == ("CFTC_COT_RELEASE_LAG",)
    assert need.providers == ("cftc",)
    from RevenueChaser.module_spx_gamma import READS
    assert "market.cot_release" in READS


@pytest.mark.parametrize("releases, day, shown, released, estimated", [
    # A Monday-dated report (Tuesday a holiday) maps to its week's Tuesday row: 9 Nov + Tue 10 Nov's 6 days.
    (RELEASES, date(2026, 11, 13), "2026-11-02", "2026-11-06", False),
    (RELEASES, date(2026, 11, 16), "2026-11-09", "2026-11-16", False),
    # Review N1: without a schedule the estimate is the week's Friday (Tuesday + 3), not the Monday + 3.
    ((), date(2026, 11, 12), "2026-11-02", "~2026-11-06", True),
    ((), date(2026, 11, 13), "2026-11-09", "~2026-11-13", True),
])
def test_a_monday_dated_report_is_released_with_its_week(rcdata_market, releases, day, shown, released, estimated):
    """Review S2: the Monday branch of ``_release_of``."""
    mondays = tuple(r - timedelta(days=1) for r in REPORTS)
    fx.serve(cot_reports=mondays, releases=releases, series_end=date(2026, 11, 30))
    out = store_reads.cot_tff(day)
    assert (out["payload"]["report_date"], out["payload"]["release_estimated"]) == (shown, estimated)
    assert f"released {released}" in out["label"]
