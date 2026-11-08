"""A6 (Fable report review, 5 Nov 2026): the session-by-session hedge-flow table of the week.

One row per expiry from the board's day through the target Friday (each session's expiring book), from UW's
greek-exposure/expiry rows: net gamma, the hedge for a 50-point move (the reports' |Net GEX per 1 %| x move %),
net charm and net vanna, and the effect in words. Expiring open interest needs OI per expiry and is phase 2.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_spx_gamma import expiry_table

ROWS = expiry_table.normalize_expiry_rows({"data": [
    {"expiry": "2026-11-04", "dte": 0, "call_gex": "81763.8", "put_gex": "-25107.4", "call_charm": "29023001802.8",
     "put_charm": "8741667014.8", "call_vanna": "-52729.3", "put_vanna": "-25418.6"},
    {"expiry": "2026-11-05", "dte": 1, "call_gex": "14207.3", "put_gex": "-8006.5", "call_charm": "-39403022.2",
     "put_charm": "60759735.9", "call_vanna": "1932654.5", "put_vanna": "-1127147.0"},
    {"expiry": "2026-11-06", "dte": 2, "call_gex": "-100", "put_gex": "-200", "call_charm": "-5", "put_charm": "-5"},
    {"expiry": "2026-11-09", "dte": 5, "call_gex": "1", "put_gex": "0"},
]})


def test_one_row_per_session_of_the_week():
    rows = expiry_table.session_hedge_flow(ROWS, "2026-11-04", 7700.0)
    assert [r["expiry"] for r in rows] == ["2026-11-04", "2026-11-05", "2026-11-06"]
    thu = rows[1]
    assert thu["net_gex"] == pytest.approx(14207.3 - 8006.5)
    assert thu["hedge_50pt"] == pytest.approx(abs(14207.3 - 8006.5) * 50 / 7700.0 * 100)
    assert thu["net_charm"] == pytest.approx(-39403022.2 + 60759735.9)
    assert "dampens" in thu["effect"] and "buys into the close" in thu["effect"]
    assert "amplifies" in rows[2]["effect"] and "sells into the close" in rows[2]["effect"]


def test_snapshot_and_page():
    from RevenueChaser.module_spx_gamma import render
    from .conftest import make_snapshot
    snap = make_snapshot()
    assert snap["session_flow"] and snap["session_flow"][0]["expiry"] == "2026-08-14"
    assert "Session hedge flow" in render.render_html(snap)
