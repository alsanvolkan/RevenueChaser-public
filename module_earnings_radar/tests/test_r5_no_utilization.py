"""Fable review Part B (Earnings Radar, R5; #256). Offline.

``_parse_short_row`` made up a "utilization", short interest / (short interest + shares available), and R5 Z-scored
it at weight 0.2. UW's short screener serves no utilization field, and docs/rules/redesign.md R5 says "there is no
utilization field, never invent one". The squeeze is now 0.4 SI, 0.3 borrow fee, 0.1 days to cover, renormalized
over what is present. Default chosen, change if avo prefers: the alternative is a utilization field UW really serves,
should one appear.
"""
import inspect

import pytest

from RevenueChaser.core.providers.unusual_whales import UWClient
from RevenueChaser.module_earnings_radar.rules.rule5_positioning import register_peer_stats, reset_peer_stats
from RevenueChaser.module_earnings_radar.rules.rule5_positioning import score as r5_score


def test_the_short_row_carries_no_utilization():
    row = {"si_float": "0.017", "fee_rate": "0.2782", "days_to_cover": "2.1", "short_interest": "75000",
           "short_shares_available": "10000000"}
    assert sorted(UWClient._parse_short_row(row)) == ["borrow_fee", "days_to_cover", "si_pct"]
    assert UWClient._parse_short_row({}) == {"si_pct": None, "borrow_fee": None, "days_to_cover": None}


def test_the_squeeze_is_si_borrow_and_days_to_cover_only():
    reset_peer_stats()
    try:
        for t, si, fee, dtc in (("BBB", 5, 1, 1), ("CCC", 6, 1.2, 1.2), ("DDD", 5.5, 0.9, 1.1)):
            register_peer_stats(t, "Widgets", {"si_pct": si, "borrow_fee": fee, "days_to_cover": dtc,
                                               "utilization": 50.0})
        r = r5_score(short_interest_pct_float=9, days_to_cover=2, borrow_fee=2, sector="Widgets", ticker="AAA")
        z = r.raw_metrics
        assert sorted(k for k in z if k.startswith("z_")) == ["z_borrow", "z_dtc", "z_si"]
        assert z["squeeze_z_score"] == pytest.approx(
            (0.4 * z["z_si"] + 0.3 * z["z_borrow"] + 0.1 * z["z_dtc"]) / 0.8, abs=0.01)
        assert "utilization" not in inspect.signature(r5_score).parameters
    finally:
        reset_peer_stats()
