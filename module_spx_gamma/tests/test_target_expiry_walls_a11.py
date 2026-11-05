"""A11 (Fable report review, 5 Nov 2026): the walls of the week's target expiry, beside the DTE bands.

The reports name the call and put wall of the SPXW expiry the week resolves on (the next Friday, as the Brier
scoring), not only band aggregates. Same rule as the bands: the largest positive net gamma below / above spot.
"""
from __future__ import annotations

from RevenueChaser.module_spx_gamma import levels, render
from .conftest import make_snapshot


def test_walls_of_one_expiry():
    rows = [{"expiry": "2026-08-21", "strike": 7600.0, "net_gamma": -295.0},
            {"expiry": "2026-08-21", "strike": 7750.0, "net_gamma": 40.0},
            {"expiry": "2026-08-21", "strike": 7900.0, "net_gamma": 10.0},
            {"expiry": "2026-09-18", "strike": 7950.0, "net_gamma": 900.0}]
    w = levels.expiry_walls(rows, "2026-08-21", 7784.0)
    assert w["expiry"] == "2026-08-21"
    assert w["put_wall"] == 7750.0 and w["call_wall"] == 7900.0


def test_snapshot_carries_the_target_expiry_walls():
    snap = make_snapshot()          # asof Fri 2026-08-14 -> target Fri 2026-08-21
    w = snap["target_expiry_walls"]
    assert w["expiry"] == "2026-08-21"
    assert w["call_wall"] == 7900.0
    md = render.render_markdown(snap)
    assert "Target expiry 2026-08-21" in md
