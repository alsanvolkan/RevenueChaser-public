"""Tests for the single-panel SPX critical thresholds chart."""
from __future__ import annotations

import plotly.graph_objects as go

from RevenueChaser.module_spx_gamma.charts import position_map
from .conftest import make_snapshot


def test_has_candlestick_and_tornado_panels(snapshot):
    fig = position_map.build(snapshot)
    assert any(isinstance(t, go.Candlestick) for t in fig.data)
    assert any(isinstance(t, go.Bar) for t in fig.data), "tornado bar panel present"
    # stacked layout: candlestick on row 1, bar on row 2
    rows = {t.xaxis for t in fig.data}
    assert len(rows) >= 2, "candles and tornado on separate rows"


def test_regime_shading(snapshot):
    fig = position_map.build(snapshot)
    rects = [s for s in fig.layout.shapes if s.type == "rect"]
    assert len(rects) >= 2  # two gamma-regime zones + expected-move band


def test_charm_level_plotted_when_in_range(snapshot):
    fig = position_map.build(snapshot)
    texts = [a.text for a in fig.layout.annotations if a.text]
    assert any("Charm Peak" in t for t in texts)


def test_charm_far_otm_not_plotted():
    snap = make_snapshot()
    snap["charm_qualification"]["strike"] = 9000.0
    fig = position_map.build(snap)
    texts = [a.text for a in fig.layout.annotations if a.text]
    assert not any("Charm Peak" in t for t in texts), "charm level must not be drawn when far OTM"


def test_merged_duplicate_levels(snapshot):
    fig = position_map.build(snapshot)
    texts = [a.text for a in fig.layout.annotations if a.text]
    assert any("Call Wall / Gamma Magnet" in t for t in texts)


def test_vanna_peaks_plotted(snapshot):
    fig = position_map.build(snapshot)
    texts = [a.text for a in fig.layout.annotations if a.text]
    assert any("Vanna Call Peak" in t for t in texts)
    assert any("Vanna Put Peak" in t for t in texts)


def test_vanna_far_otm_not_plotted():
    snap = make_snapshot()
    snap["vanna_peaks"]["call_peak"]["strike"] = 9000.0
    snap["vanna_peaks"]["put_peak"]["strike"] = 6000.0
    fig = position_map.build(snap)
    texts = [a.text for a in fig.layout.annotations if a.text]
    assert not any("Vanna Call Peak" in t for t in texts)
    assert not any("Vanna Put Peak" in t for t in texts)


def test_x_range_matches_sessions(snapshot):
    fig = position_map.build(snapshot)
    dates = [c["date"] for c in snapshot["candles"]]
    assert list(fig.layout.xaxis.range) == [dates[0], dates[-1]]


def test_no_candles_returns_none():
    snap = make_snapshot()
    snap["candles"] = []
    assert position_map.build(snap) is None


def test_two_gamma_flip_lines(snapshot):
    fig = position_map.build(snapshot)
    texts = [a.text for a in fig.layout.annotations if a.text]
    assert any("Gamma Flip (all-tenor)" in t for t in texts)
    assert any("Gamma Flip (0DTE)" in t for t in texts)


def test_put_wall_uses_181_365_band(snapshot):
    fig = position_map.build(snapshot)
    texts = [a.text for a in fig.layout.annotations if a.text]
    assert any("Put Wall" in t and "7,500" in t for t in texts)


def test_the_one_panel_map_is_700_px_tall(snapshot):
    """#195 review MUST 1: height=None shrank the PNG export (700x500) and the shell's desktop map."""
    fig = position_map.build(snapshot, include_tornado=False)
    assert fig.layout.height == 700 and fig.layout.autosize is True
