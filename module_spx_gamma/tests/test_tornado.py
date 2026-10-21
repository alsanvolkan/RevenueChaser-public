"""Tests for the tornado (signed Net GEX) profile and overflow clamping."""
from __future__ import annotations

import plotly.graph_objects as go

from RevenueChaser.module_spx_gamma.charts import tornado
from .conftest import make_snapshot


def test_tornado_rows_windows_around_spot(snapshot):
    rows = tornado.tornado_rows(snapshot)
    assert rows
    spot = snapshot["current_level"]
    for r in rows:
        assert spot * (1 - tornado.TORNADO_WINDOW_PCT) <= r["strike"] <= spot * (1 + tornado.TORNADO_WINDOW_PCT)


def test_tornado_rows_sorted_ascending(snapshot):
    rows = tornado.tornado_rows(snapshot)
    strikes = [r["strike"] for r in rows]
    assert strikes == sorted(strikes)


def test_tornado_range_symmetric_cap(snapshot):
    rows = tornado.tornado_rows(snapshot)
    lo, hi = tornado.tornado_range(rows)
    assert lo < 0 < hi
    assert abs(abs(lo) - hi) < 1e-6  # symmetric


def test_bar_value_and_overflow_within_cap():
    v, ovf = tornado.bar_value_and_overflow(100.0, cap=200.0)
    assert v == 100.0 and ovf is False


def test_bar_value_and_overflow_clamps():
    v, ovf = tornado.bar_value_and_overflow(500.0, cap=200.0)
    assert v == 200.0 and ovf is True
    v, ovf = tornado.bar_value_and_overflow(-500.0, cap=200.0)
    assert v == -200.0 and ovf is True


def test_build_tornado_fig_returns_figure(snapshot):
    fig = tornado.build_tornado_fig(snapshot)
    assert fig is not None
    assert any(isinstance(t, go.Bar) for t in fig.data)


def test_build_tornado_fig_none_without_rows():
    snap = make_snapshot()
    snap["strike_exposures"] = []
    assert tornado.build_tornado_fig(snap) is None


def test_level_entries_skip_out_of_range(snapshot):
    snap = make_snapshot()
    snap["charm_qualification"]["strike"] = 9000.0  # far outside the 12% window
    entries = tornado._level_entries(snap, strike_lo=7000, strike_hi=8000)
    assert not any("Charm" in e["label"] for e in entries)
