"""F5 (Fable report review, 5 Nov 2026): the vega-flow side verdict survives the largest minute, or there is none.

The 2 Oct report (p.7) reads the sign of the day's directional vega flow only when one bucket does not carry it:
if taking out the largest minute flips the sign, the day has "no side verdict".
"""
from __future__ import annotations

from RevenueChaser.module_spx_gamma import normalize as nm


def _flow(values):
    return {"data": [{"timestamp": f"2026-11-04T14:{i:02d}:00Z", "dir_vega_flow": str(v), "total_vega_flow": "1"}
                     for i, v in enumerate(values)]}


def test_a_broad_sign_keeps_its_verdict():
    v = nm.normalize_vega_flow(_flow([100, 120, -30, 90, 80]))
    assert v["side"] == "buyers" and v["largest_minute"] == 120.0 and v["without_largest"] == 240.0


def test_one_minute_carrying_the_sign_gives_no_verdict():
    v = nm.normalize_vega_flow(_flow([-50, -40, 500, -60, -30]))
    assert v["dir_vega_flow"] == 320.0
    assert v["without_largest"] == -180.0
    assert v["side"] is None and v["verdict"] == "no side verdict"


def test_negative_flow_reads_sellers():
    assert nm.normalize_vega_flow(_flow([-100, -80, 10, -60]))["side"] == "sellers"
