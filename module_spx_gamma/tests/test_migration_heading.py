"""DF33: the level-migration table compares with the newest earlier snapshot, not a week ago."""
from __future__ import annotations

import pytest

from RevenueChaser.module_spx_gamma import render, run
from .conftest import make_snapshot


@pytest.fixture(autouse=True)
def _no_local_state(monkeypatch):
    monkeypatch.setattr(run, "_load_history", lambda: {})
    monkeypatch.setattr(render, "scorecard", lambda: {
        "average": None, "n": 0, "verdict": "No scored weeks yet", "calibrated": None, "records": []})


def test_html_and_markdown_headings_do_not_claim_week_over_week():
    snap = make_snapshot()
    snap["api_snapshot_time"] = "2026-08-14T21:00:00Z"
    html = render.render_html(snap, chart_png="c.png", chart_html="", tornado_html="", envelope_html="")
    md = render.render_markdown(snap, chart_png="c.png")
    for text in (html, md):
        assert "Week-over-Week" not in text
        assert "Level Migration vs Prior Snapshot" in text
