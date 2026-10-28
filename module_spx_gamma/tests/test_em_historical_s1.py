"""Fable review S1 (SPX Gamma): ``interpolated_iv.json`` is a live file (snapshot.LIVE_FILES), so in a backfilled or
re-fetched past day it holds a later session's IV. The skew panel was already marked "n/a (historical)"; the
expected move read from the same file, its band on the position map and the iron butterfly's wing width (sized from
it) were shown as the day's own. They are now marked or left out the same way. Offline."""
from __future__ import annotations

import pytest

from RevenueChaser.module_spx_gamma import render
from RevenueChaser.module_spx_gamma import run as spx_run
from RevenueChaser.module_spx_gamma.charts import position_map

from .conftest import make_snapshot

LATER_IV = ["interpolated_iv.json"]


@pytest.fixture(autouse=True)
def _no_local_state(monkeypatch):
    monkeypatch.setattr(spx_run, "_load_history", lambda *a, **k: {})
    monkeypatch.setattr(render, "scorecard", lambda *a, **k: {
        "average": None, "n": 0, "verdict": "No scored weeks yet", "calibrated": None, "records": []})


def _em_band(fig) -> list:
    return [s for s in fig.layout.shapes if s.line and s.line.dash == "dot" and s.fillcolor == "rgba(230,159,0,0.08)"]


def test_the_days_own_iv_keeps_its_expected_move():
    s = make_snapshot()
    assert s["expected_move"], "the fixture has an expected move"
    ctx = render.build_context(s)
    assert ctx["header_strip"]["expected_move"] != "N/A" and "historical" not in ctx["header_strip"]["expected_move"]
    assert _em_band(position_map.build(s, include_tornado=False))


def test_a_later_sessions_iv_shows_no_expected_move():
    s = make_snapshot()
    s["historical_live_files"] = LATER_IV
    ctx = render.build_context(s)
    assert ctx["header_strip"]["expected_move"] == "n/a (historical)"
    row = next(r for r in ctx["details"] if r["level"] == "One-Day Implied Range (1σ)")
    assert row["value"] == "n/a (historical)" and "n/a (historical)" in row["method"]
    assert not _em_band(position_map.build(s, include_tornado=False))


def test_the_iron_butterfly_sized_from_a_later_iv_is_marked_historical():
    s = make_snapshot()
    s["historical_live_files"] = LATER_IV
    assert render._historical_panels(s)["iron_butterfly"] is True
    s["historical_live_files"] = []
    assert render._historical_panels(s)["iron_butterfly"] is False
