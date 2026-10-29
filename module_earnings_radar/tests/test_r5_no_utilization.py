"""Fable review Part B (Earnings Radar, R5; #256). Offline.

``_parse_short_row`` made up a "utilization", short interest / (short interest + shares available), and R5 Z-scored
it at weight 0.2. UW's short screener serves no utilization field, and docs/rules/redesign.md R5 says "there is no
utilization field, never invent one". Since G9 the squeeze is a none / fuel tag from the ownership store's inputs
(``test_r5_ownership_store_g9``), so R5 takes no utilization either.
"""
import inspect

from RevenueChaser.core.providers.unusual_whales import UWClient
from RevenueChaser.module_earnings_radar.rules.rule5_positioning import score as r5_score


def test_the_short_row_carries_no_utilization():
    row = {"si_float": "0.017", "fee_rate": "0.2782", "days_to_cover": "2.1", "short_interest": "75000",
           "short_shares_available": "10000000"}
    assert sorted(UWClient._parse_short_row(row)) == ["borrow_fee", "days_to_cover", "si_pct"]
    assert UWClient._parse_short_row({}) == {"si_pct": None, "borrow_fee": None, "days_to_cover": None}
    assert "utilization" not in inspect.signature(r5_score).parameters

