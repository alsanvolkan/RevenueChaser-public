"""The RS rating and the direction rule are described as what they are (Swing review findings 5 and 6, PR 6).

5: the RS rating is a percentile, among the names this scan scored, of the 12-month return against SPY, and since
#65 it ranks (a quality point) without deciding Minervini; Settings called it a "floor" and config "percentile vs
SPY". 6: for a Minervini pass the direction rule's "50-day MA at or above the 200-day MA" is already implied (50 >
150 > 200), so only its 3-month return half adds anything there; the board now says so.
"""
from __future__ import annotations

from RevenueChaser.core import board_settings as bs
from RevenueChaser.module_settings import boards
from RevenueChaser.module_swing_potential import pipeline, report
from RevenueChaser.module_swing_potential.tests.test_swing_settings_s4 import _rows

B = "module_swing_potential"


def test_settings_call_rs_a_ranking_percentile_not_a_floor(rawstore_tmp_db):
    label = next(r["label"] for r in bs.describe(B) if r["key"] == "RS_MIN")
    assert "floor" not in label and "among the scanned names" in label and "ranks only" in label
    line = next(p for p in boards._swing_params() if p.startswith("Relative strength"))
    assert "ranks only" in line


def test_the_settings_line_names_the_volume_window(rawstore_tmp_db):
    assert any("on one of the last 5 sessions" in p for p in boards._swing_params())


def test_the_board_says_what_the_rs_rating_is_and_where_direction_adds_something(rawstore_tmp_db):
    page = report.render_swing_dashboard(pipeline.build_payload(_rows(), top_n=10))
    assert "percentile, among the names scanned, of the 12-month return vs SPY" in page
    assert "for a Minervini pass the first half is already implied" in page
