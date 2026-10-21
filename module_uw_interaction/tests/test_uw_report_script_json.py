"""UW Interaction board: the embedded catalog cannot blank the script (ADR-X1 F3, #144)."""
from __future__ import annotations

from RevenueChaser.core.tests.board_js import PROBE, assert_scripts_intact
from RevenueChaser.module_uw_interaction import report


def test_a_comment_opener_or_placeholder_in_the_catalog_keeps_the_script_intact(
        tmp_path, monkeypatch):
    real = report.catalog_payload

    def _probe_catalog():
        cat = real()
        next(iter(cat.values()))[0]["help"] = PROBE
        return cat
    monkeypatch.setattr(report, "catalog_payload", _probe_catalog)
    assert_scripts_intact(report.render_board(None), tmp_path)
