"""The default $ cap has to cover one full round of profiles (#712).

Before this, the cap was 8.0 USD at an unmeasured 0.006 USD a call, so a run stopped after 1,333 calls
and looked finished. A full round is every company with usable text: ~4,750 names at 1.67 calls each.
"""
from RevenueChaser.module_relatedness import config

FULL_ROUND_CALLS = 7_900


def test_default_cap_covers_a_full_round():
    assert int(config.USD_CAP / config.EST_USD_PER_CALL) >= FULL_ROUND_CALLS
