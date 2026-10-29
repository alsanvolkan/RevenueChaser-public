"""Fable review S5 (Earnings Radar, R3). Offline.

R3's four-quarter gate guarded only the universal operating-margin layer, so a cluster scorer still voted on one
quarter's levels at full coverage: a C3 name with one quarterly row scored -75 at coverage 1.0. docs/rules/redesign.md
R3 locks "usable quarters >= 4, take last 8; coverage buckets (3-4 / 5-7 / 8+)". Fewer than four usable quarters (a
row with revenue) is now unavailable, and the coverage is the quarter bucket, 0.40 / 0.60 / 0.80 as R2 has it; the
share of the cluster's signals stays as the raw metric ``signal_coverage``.

Default chosen, change if avo prefers: the alternative is docs/rules/r3_financial.md's older "two features" rule,
which would then be written into redesign.md instead.
"""
import pytest

from RevenueChaser.module_earnings_radar.rules.rule3_financial import score

QUARTERS = ["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31", "2024-12-31", "2024-09-30",
            "2024-06-30", "2024-03-31"]


def _history(n):
    return [{"revenue": 1000.0, "gross_margin": 0.60, "operating_margin": 0.20, "fiscal_date": d}
            for d in QUARTERS[:n]]


def test_one_quarter_no_longer_votes():
    one = [{"revenue": 1000, "gross_margin": 0.05, "operating_margin": 0.05, "receivables": 500,
            "fiscal_date": "2026-03-31"}]
    r = score("X", "C3_fintech_lending", one)
    assert r.available is False and r.score is None, "was -75 at coverage 1.0 off one quarter's levels"
    assert r.reasons == ["fewer than 4 usable quarters (1)"]
    assert r.raw_metrics["usable_quarters"] == 1


def test_a_row_without_revenue_is_not_a_usable_quarter():
    rows = _history(3) + [{"operating_margin": 0.2, "fiscal_date": QUARTERS[3]}]
    r = score("X", "unmapped", rows)
    assert not r.available and r.reasons == ["fewer than 4 usable quarters (3)"]


@pytest.mark.parametrize("n,coverage", [(4, 0.40), (5, 0.60), (7, 0.60), (8, 0.80), (9, 0.80)])
def test_the_coverage_is_the_quarter_bucket(n, coverage):
    r = score("X", "unmapped", _history(n))
    assert r.available is True and r.coverage == coverage
    assert r.raw_metrics["usable_quarters"] == min(n, 8), "the last eight are read"
    assert 0 < r.raw_metrics["signal_coverage"] <= 1.0
