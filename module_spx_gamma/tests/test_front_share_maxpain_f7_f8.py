"""F7 and F8 (Fable report review, 5 Nov 2026).

F7: the 30 Oct report (p.9) gives the front expiry's share of the total Net GEX (17.18 %).
F8: §5.2 Max Pain counts how many expiries' max pain sit below and above spot.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_spx_gamma import expiry_table, render
from .conftest import make_snapshot


def _rows():
    return expiry_table.normalize_expiry_rows({"data": [
        {"expiry": "2026-11-03", "dte": -1, "call_gex": "900", "put_gex": "0"},
        {"expiry": "2026-11-04", "dte": 0, "call_gex": "30", "put_gex": "-10"},
        {"expiry": "2026-11-05", "dte": 1, "call_gex": "50", "put_gex": "-10"},
        {"expiry": "2026-12-18", "dte": 44, "call_gex": "40", "put_gex": "0"},
    ]})


def test_front_share_is_front_net_over_total_net():
    s = expiry_table.front_expiry_share(_rows(), "2026-11-04")
    assert s["expiry"] == "2026-11-04"
    assert s["front_net_gex"] == 20.0 and s["total_net_gex"] == 100.0   # the expired series is not counted
    assert s["share_pct"] == pytest.approx(20.0)


def test_front_share_is_none_without_a_total():
    assert expiry_table.front_expiry_share([], "2026-11-04") is None


def test_snapshot_and_page_show_the_front_share():
    snap = make_snapshot()
    assert snap["front_expiry_share"]["share_pct"] is not None
    html = render.render_html(snap)
    assert "Front-expiry share of Net GEX" in html


def test_max_pain_counts_below_and_above_spot():
    snap = make_snapshot()
    snap["max_pain"] = [{"expiry": "a", "max_pain": 7700.0, "distance_pct": -1.0},
                        {"expiry": "b", "max_pain": 7750.0, "distance_pct": -0.4},
                        {"expiry": "c", "max_pain": 7900.0, "distance_pct": 1.5}]
    ctx = render.build_context(snap)
    assert ctx["max_pain_count"] == {"below": 2, "above": 1, "at": 0}
    assert "2 below spot · 1 above" in render.render_markdown(snap)
