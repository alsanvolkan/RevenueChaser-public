"""The "themed" overlay is named for what it counts (Asymmetric Growth review N-3, PR 6).

The board called it "Disruptive Theme" / "Disruptive Themed", but it counts every card with any pinned theme of the
rotation map (``engine._summary``: ``c["themes"]`` non-empty); no theme is graded as disruptive. The labels now say
"Themed" and what that means. Restricting it to a set of disruptive themes would be a rule change for avo.
"""
from __future__ import annotations

from RevenueChaser.module_asymmetric_growth import engine, report


def test_the_board_does_not_call_any_theme_disruptive(rawstore_tmp_db):
    page = report.render_asymmetric_growth_hunter_html(engine.stored_result())
    assert "Disruptive" not in page
    assert "Themed</div>" in page and "any pinned theme of the rotation map" in page
