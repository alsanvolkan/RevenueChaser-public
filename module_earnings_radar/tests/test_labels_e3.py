"""E3 (#372): label fixes — ER-C, ER-4/E, DF05, ER-8, ER-9, X-4.

Text and sort labels only: the Coverage formula renders its direction-weight sum from the config,
the Sector cell shows the spine sector the card was scored against, the dead Rotation sort goes,
$Vol is named as OptVol x 100, Last sync stamps the run time in New York, and the Owners header
names its real source (AG). Offline.
"""
from datetime import date, datetime, timezone

from RevenueChaser.core.models.events import TradeCard
from RevenueChaser.module_earnings_radar.report import export_html


def _card(**overrides):
    defaults = {"ticker": "AAA", "name": "A Co", "cluster": "Technology",
                "earnings_date": date(2026, 8, 11), "report_time_label": "PM"}
    defaults.update(overrides)
    return TradeCard(**defaults)


def test_the_sector_cell_shows_the_spine_sector_not_the_calendar():
    card = _card(cluster="", sector="Industrials")
    events = [{"ticker": "AAA", "sector": "Technology"}]
    content = export_html([card], events, {}).read_text(encoding="utf-8")
    assert "Industrials" in content
    assert "Technology" not in content


def test_last_sync_stamps_the_run_time_in_new_york():
    run_at = datetime(2026, 10, 15, 20, 42, tzinfo=timezone.utc).timestamp()
    content = export_html([_card()], [], {}, run_at=run_at).read_text(encoding="utf-8")
    assert "Last sync: Thursday, October 15, 2026 16:42 ET" in content
    assert "EOD" not in content.split("Last sync:", 1)[1][:60]


def test_the_coverage_formula_renders_the_direction_weight_sum():
    content = export_html([_card()], [], {}).read_text(encoding="utf-8")
    assert "Σ all direction weights (0.65)" in content


def test_the_dead_rotation_sort_is_gone():
    content = export_html([_card()], [], {}).read_text(encoding="utf-8")
    assert "data-sort-rot" not in content
    assert 'data-sort-key="rot"' not in content


def test_the_dollar_volume_help_names_its_optvol_identity():
    stats = {"total": 1, "passed": 1, "bypassed": 0, "filtered": 0, "by_reason": {}, "excluded": []}
    content = export_html([_card()], [], {}, filter_stats=stats).read_text(encoding="utf-8")
    assert "$Vol (= OptVol × 100)" in content
    assert "adds nothing to OptVol" in content
    assert "$Vol≥$50K (= OptVol×100)" in content


def test_the_owners_header_names_the_asymmetric_growth_source():
    content = export_html([_card()], [], {}).read_text(encoding="utf-8")
    assert "Owners (AG)" in content
    assert "not this board's 13F period" in content
    assert "Owners (13F" not in content


def test_the_pipeline_card_carries_the_spine_sector_it_was_scored_against(monkeypatch):
    """The board cell reads ``card.sector``: the pipeline sets it from the spine, not from the calendar row."""
    from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
    from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW
    from RevenueChaser.core import universe as spine

    from RevenueChaser.core.gics_map import GicsPath
    monkeypatch.setattr(spine, "gics_of", lambda t, g=None: GicsPath("Industrials", "Machinery"))
    cal = {"AAA": {"earnings_date": "2026-08-14", "full_name": "AAA", "sector": "Technology",
                   "expected_move_perc": 0.05}}
    p = EarningsPipeline(["AAA"], uw=_FakeUW(), cal_meta=cal)
    p._dgs10_cache = {}
    (card,) = p.run(use_preselect=False)
    assert card.sector == "Industrials"
