"""Rotation label defects: DF73 ('2-day' legend), DF67 (null RS)."""
from __future__ import annotations

from RevenueChaser.module_sector_rotation import report


def test_df73_early_turn_legend_names_snapshots_not_days():
    src = open(report.__file__, encoding="utf-8").read()
    assert "2-day momentum reversal" not in src
    assert "two prior stored snapshots" in src


def test_df67_a_missing_rs_prints_a_dash_not_plus_zero(tmp_path, monkeypatch, rawstore_tmp_db):
    """DF67: a null RS cell showed '+0.0%' (``or 0``), identical to a real zero."""
    from datetime import date

    from RevenueChaser.core.models.rotation import ETFRotationSnapshot

    monkeypatch.setattr(report, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(report, "coarse_theme_catalog", lambda: {})
    snap = ETFRotationSnapshot(
        as_of_date=date(2026, 8, 14), ticker="XLK", group_name="sector", close_price=100.0,
        rs_1w=None, rs_1m=0.0, rs_3m=2.0, rs_ratio=1.0, rs_momentum=1.0,
        rs_ratio_z=1.0, rs_momentum_z=0.5, rrg_quadrant="LEADING")
    out = report.export_rotation_dashboard([snap], filename="df67.html", uw=None)
    html = out.read_text(encoding="utf-8")

    def cell(col: str) -> str:
        import re
        return re.search(rf'<td[^>]*data-col="{col}"[^>]*>([^<]*)</td>', html).group(1)

    assert cell("rs_1w") == "—"
    assert cell("rs_1m") == "+0.0%"        # a real zero still prints as zero
    assert cell("rs_3m") == "+2.0%"
