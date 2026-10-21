"""Rotation board: one theme label cannot blank the script (ADR-X1 F3, #144).

Coarse theme labels come from LLM-named NMF runs and are embedded as
``THEME_ETFS``; holdings fragments carry provider names.
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.core.models.rotation import ETFRotationSnapshot
from RevenueChaser.core.tests.board_js import PROBE, assert_scripts_intact


def _snap(ticker: str, quadrant: str = "LEADING") -> ETFRotationSnapshot:
    return ETFRotationSnapshot(
        as_of_date=date(2026, 8, 14), ticker=ticker, group_name="sector",
        close_price=100.0, rs_1w=1.0, rs_1m=2.0, rs_3m=3.0,
        rs_ratio=2.0, rs_momentum=1.0, rs_ratio_z=1.0, rs_momentum_z=0.5,
        rrg_quadrant=quadrant)


def test_a_comment_opener_or_placeholder_in_a_theme_keeps_the_script_intact(
        tmp_path, monkeypatch, rawstore_tmp_db):
    import RevenueChaser.module_sector_rotation.report as rd
    monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(rd, "coarse_theme_catalog", lambda: {PROBE: ["XLK"]})
    holdings = {"XLK": [{"ticker": "UNH", "name": PROBE, "weight": 8.5, "close": 500.0,
                         "rs_1w": 1.0, "rs_1m": 2.0, "rs_3m": 3.0}]}
    p = rd.export_rotation_dashboard([_snap("XLK"), _snap("XLV", "IMPROVING")],
                                     filename="probe.html", uw=None, holdings=holdings)
    assert_scripts_intact(p.read_text(encoding="utf-8"), tmp_path)


def test_the_legend_says_dot_size_is_fixed(tmp_path, monkeypatch, rawstore_tmp_db):
    """DF72 / SR-1: no stored 30d ADV, so every dot is one size on Fetch and Refresh alike; the legend says so."""
    import RevenueChaser.module_sector_rotation.report as rd
    monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
    p = rd.export_rotation_dashboard([_snap("XLK"), _snap("XLV", "IMPROVING")], filename="legend.html", uw=None)
    html = p.read_text(encoding="utf-8")
    assert "Dot size fixed (no stored 30d ADV" in html
    assert "Dot size \u221d" not in html
