"""DF06 (#35): the composite Confidence divides by ALL direction weights, not by the live ones.

Avo chose to reword, not to change the math. ``GOLDEN`` pins what ``compute_composite_scores`` returns today, so
a change of the denominator (or of any score) is red; the wording tests pin what the board and the rules doc now
say the number is. Offline.
"""
from datetime import date
from pathlib import Path

import pytest

from RevenueChaser.module_earnings_radar.composite import compute_composite_scores
from RevenueChaser.module_earnings_radar.models import RuleResult, TradeCard
from RevenueChaser.module_earnings_radar.report import _conf_cell, export_html


def _r(score, coverage=1.0, available=True):
    return RuleResult(score=score, available=available, coverage=coverage)


# name -> (rules, direction, confidence) as returned on main 33a5d49.
GOLDEN = {
    "all_live":         (dict(r2=_r(40), r3=_r(30), r7=_r(20), r8=_r(10)), 27.7, 100.0),
    "r8_missing":       (dict(r2=_r(40), r3=_r(30), r7=_r(20)), 30.9, 84.6),
    "only_r2":          (dict(r2=_r(40)), 40.0, 30.8),
    "r3_r7_oppose":     (dict(r2=_r(40), r3=_r(30), r7=_r(-20), r8=_r(10)), 18.5, 80.0),
    "half_coverage":    (dict(r2=_r(40, .5), r3=_r(30, .5), r7=_r(20, .5), r8=_r(10, .5)), 27.7, 50.0),
    "unavailable_flag": (dict(r2=_r(40), r3=_r(None, 0.0, False), r7=_r(20), r8=_r(10)), 26.7, 69.2),
    "nothing":          ({}, 0.0, 0.0),
}


@pytest.mark.parametrize("name", GOLDEN)
def test_the_composite_numbers_have_not_moved(name):
    rules, direction, confidence = GOLDEN[name]
    out = compute_composite_scores(**rules)
    assert out["direction_score"] == direction
    assert out["confidence_score"] == confidence


def _html():
    card = TradeCard(ticker="AAA", name="A Co", cluster="Technology", earnings_date=date(2026, 8, 11),
                     report_time_label="PM")
    return export_html([card], [], {}).read_text(encoding="utf-8")


def test_the_methodology_says_a_missing_rule_still_counts_against_coverage():
    html = _html()
    assert "the denominator is always all four direction rules" in html
    assert "still count against Coverage" in html
    assert "no 0 fill." not in html
    assert "R8 missing" in html and "84.6%" in html


def test_the_tooltip_says_the_denominator_is_every_direction_rule():
    tip = _conf_cell(84.6)
    assert "ALL four direction rules" in tip
    assert "A missing rule counts as 0" in tip


def test_the_rules_doc_states_what_the_code_divides_by():
    doc = (Path(__file__).resolve().parents[2] / "docs" / "rules" / "00_overview.md").read_text(encoding="utf-8")
    assert "Σ(all direction weights)" in doc
    assert "when R3 and R7 land on opposite signs" in doc
    assert "when available rules disagree on direction sign" not in doc
