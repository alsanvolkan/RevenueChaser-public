"""The funnel marks Fundamentals' own rules as rules, not data gaps (Fundamentals review N3, PR 6).

A name dropped for its sector (financials, real estate) or for a stale latest annual report left the scan by a
board rule, but the funnel showed both as data gaps "that need a look", under their raw keys. They are now listed
with plain labels and marked by design. The Rule of 40 barrier stays a data gap on purpose: the code drops an
implausible Rule of 40 as a data problem (a figure to check), not as a pick rule.
"""
from __future__ import annotations

from RevenueChaser.core import funnel
from RevenueChaser.core import screen_common as sc
from RevenueChaser.module_fundamentals import config as fconfig


def _steps(dropped):
    return {s["key"]: s for s in sc.funnel_steps(universe=10, excluded_types=0, prefiltered=sc.Prefiltered([], {}),
                                                  dropped=dropped, scored=10 - sum(map(len, dropped.values())),
                                                  passed=0, shown=0, cfg=fconfig)}


def test_the_sector_and_stale_annual_rules_are_by_design_with_labels():
    steps = _steps({"sector_excluded": ["BANK"], "stale_annual": ["OLD"], "ro40_barrier": ["ODD"],
                    "edgar_fetch_failed": ["ERR"]})
    assert steps["sector_excluded"]["by_design"] is True
    assert steps["sector_excluded"]["label"] == "Excluded sector (Financial Services, Real Estate)"
    assert steps["stale_annual"]["by_design"] is True
    assert steps["stale_annual"]["label"] == "Latest annual report ended more than 470 days ago"
    assert steps["ro40_barrier"]["by_design"] is False
    assert steps["ro40_barrier"]["label"] == "Rule of 40 above 150: an implausible figure, check the filing"
    assert steps["edgar_fetch_failed"]["label"] == "EDGAR request failed" and not steps["edgar_fetch_failed"]["by_design"]
    assert funnel.validate(list(steps.values())) == []
