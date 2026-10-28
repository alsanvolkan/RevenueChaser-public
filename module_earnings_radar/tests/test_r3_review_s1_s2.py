"""Fable review S1 and S2 (Earnings Radar, R3). Offline.

S1: the C6 (energy, utilities, materials) scorer counted four or five signals over a fixed three, so R3's coverage
read 1.33 and lifted composite confidence. The operating-margin branch now counts, the denominator is five, and
score() clamps coverage at 1.0 as the guard.

S2: the universal +15 operating-margin floor fired on OM +2 pts YoY alone. docs/rules/redesign.md R3 step 3 also
needs the last-4 median above the prior-4 median, which _universal_om already tests for its vote; the floor now
follows that vote.
"""
import pytest

from RevenueChaser.module_earnings_radar.rules import rule3_financial
from RevenueChaser.module_earnings_radar.rules.rule3_financial import _score_energy, score

ENERGY_LATEST = {"revenue": 1150, "gross_margin": 0.45, "operating_margin": 0.25, "cash_from_operations": 200,
                 "fiscal_date": "2026-03-31"}
ENERGY_PREV = {"revenue": 1000, "gross_margin": 0.30, "fiscal_date": "2025-03-31"}


def test_the_energy_scorer_counts_every_signal_it_scores():
    s, r, f, a, t = _score_energy(ENERGY_LATEST, [ENERGY_LATEST, ENERGY_PREV])
    assert (a, t) == (5, 5)


def test_r3_coverage_never_exceeds_one():
    r = score("UTL", "C6_energy_utilities", [ENERGY_LATEST, ENERGY_PREV])
    assert r.available and r.coverage == 1.0


def test_coverage_is_clamped_even_when_a_scorer_over_counts(monkeypatch):
    monkeypatch.setattr(rule3_financial, "_score_saas", lambda latest, history: (0.0, [], [], 4, 3))
    r = score("SAAS", "C1_saas", [{"revenue": 100, "fiscal_date": "2026-03-31"}])
    assert r.coverage == 1.0


QUARTERS = ["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31",
            "2024-12-31", "2024-09-30", "2024-06-30"]


def _history(oms):
    return [{"revenue": 100.0, "operating_margin": om, "fiscal_date": fd} for om, fd in zip(oms, QUARTERS)]


@pytest.fixture
def neutral_cluster(monkeypatch):
    monkeypatch.setattr(rule3_financial, "_score_saas", lambda latest, history: (0.0, [], [], 2, 2))


def test_om_up_yoy_without_the_median_test_gets_no_bull_floor(neutral_cluster):
    # five quarters: OM +2.5 pts on the year-ago quarter, but no prior-4 median exists, so the lock is not met
    r = score("AAA", "C1_saas", _history([0.125, 0.09, 0.09, 0.09, 0.10]))
    assert r.score == 0.0, "the +15 floor needs the last-4 median up too"


def test_om_up_yoy_with_the_last_four_median_up_keeps_the_floor(neutral_cluster):
    r = score("AAA", "C1_saas", _history([0.125, 0.12, 0.12, 0.12, 0.10, 0.10, 0.10, 0.10]))
    assert r.score == 15.0
    assert any("last-4 median improving" in reason for reason in r.reasons)


def test_om_up_yoy_while_the_median_falls_gets_no_bull_floor(neutral_cluster):
    r = score("AAA", "C1_saas", _history([0.125, 0.08, 0.08, 0.08, 0.10, 0.12, 0.12, 0.12]))
    assert r.score == 0.0
