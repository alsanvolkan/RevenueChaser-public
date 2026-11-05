"""A1 (Fable report review, 5 Nov 2026): the week-gamma profile, its turning point and the zone map.

The weekly reports read gamma only over the expiries that end by the target Friday, find the turning point where
the week profile's cumulative net gamma changes sign nearest spot, and walk the strike ladder in zones. The reports
name the zones (dense block, braked staircase, half-full, air gap) but print no thresholds, so the board's rule is
stated with the table: negative gamma is an air gap; positive gamma is a dense block at 2x or more of the window's
mean |gamma|, a braked staircase from 0.5x to 2x, and half-full below 0.5x.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_spx_gamma import week_gamma


def _r(expiry, strike, g):
    return {"expiry": expiry, "strike": float(strike), "net_gamma": float(g)}


ROWS = [
    _r("2026-11-05", 7500, -40), _r("2026-11-05", 7525, -30), _r("2026-11-05", 7550, 10),
    _r("2026-11-06", 7575, 5), _r("2026-11-06", 7600, 30), _r("2026-11-06", 7625, 80),
    _r("2026-11-06", 7650, 12),
    _r("2026-12-18", 7600, 9999),            # after the target Friday: not in the week
    _r("2026-11-03", 7600, -9999),           # expired
]


def test_week_profile_sums_only_the_weeks_expiries():
    prof = week_gamma.week_profile(ROWS, "2026-11-04", "2026-11-06")
    assert prof == [(7500.0, -40.0), (7525.0, -30.0), (7550.0, 10.0), (7575.0, 5.0), (7600.0, 30.0),
                    (7625.0, 80.0), (7650.0, 12.0)]


def test_zones_follow_the_stated_rule():
    prof = week_gamma.week_profile(ROWS, "2026-11-04", "2026-11-06")
    z = week_gamma.zones(prof, 7580.0)
    # mean |g| = 207 / 7 = 29.57: 80 is a dense block (2.7x), 30 and 12? 12/29.57 = 0.41 -> half-full
    # 25-point buckets equal the strikes here; a zone's range is [lo, hi) in index points
    assert [(x["kind"], x["lo"], x["hi"]) for x in z] == [
        ("Air gap", 7500.0, 7550.0), ("Half-full", 7550.0, 7600.0), ("Braked staircase", 7600.0, 7625.0),
        ("Dense block", 7625.0, 7650.0), ("Half-full", 7650.0, 7675.0)]
    assert sum(x["share_pct"] for x in z) == pytest.approx(100.0)


def test_five_point_strikes_are_bucketed_to_25_points():
    prof = [(7600.0, 10.0), (7605.0, 10.0), (7610.0, -5.0), (7625.0, -40.0)]
    assert week_gamma.bucketed(prof) == [(7600.0, 15.0), (7625.0, -40.0)]


def test_turning_point_is_the_cumulative_crossing_nearest_spot():
    m = week_gamma.week_map(ROWS, "2026-11-04", 7580.0)
    assert m["target"] == "2026-11-06"
    assert m["expiries"] == ["2026-11-05", "2026-11-06"]
    # cumulative: -40, -70, -60, -55, -25, +55 -> crosses between 7600 and 7625
    assert 7600.0 < m["turning_point"] < 7625.0
    assert m["net_gamma"] == pytest.approx(67.0)


def test_no_rows_no_map():
    assert week_gamma.week_map([], "2026-11-04", 7580.0) is None


def test_snapshot_and_page_carry_the_week_map():
    from RevenueChaser.module_spx_gamma import render
    from .conftest import make_snapshot
    snap = make_snapshot()                     # asof 2026-08-14, target 2026-08-21
    wm = snap["week_gamma"]
    assert wm["target"] == "2026-08-21" and "2026-08-21" in wm["expiries"]
    html = render.render_html(snap)
    assert "Week gamma map" in html and "Turning point" in html
