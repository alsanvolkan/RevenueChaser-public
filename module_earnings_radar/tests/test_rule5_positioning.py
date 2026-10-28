"""Tests for R5 positioning scoring (flow asymmetry + peer-relative squeeze).

Pin: the institutional lean (bullish minus bearish print-side premium, review S4) scales with the sided premium
and is Thin under $25k; retail-call euphoria is
NOT scored here (it lives in R8); unavailable is None when no signal exists;
peer Z-scores renormalize over present metrics; <2 in-sector peers never mark
the rule available; the absolute short-interest fallback scores only when it
actually fires (SI > 10%).
"""
import pytest

from RevenueChaser.module_earnings_radar.rules.rule5_positioning import (
    score as r5_score,
    register_peer_stats,
    reset_peer_stats,
    _z_peer,
    PEER_STATS,
)
from RevenueChaser.core.config import RULE_WEIGHTS


class TestRule5Positioning:
    def test_institutional_bullish_lean(self):
        r = r5_score(institutional_net_delta=100_000.0)
        assert r.available
        assert r.score == 20.0
        assert "institutional bullish lean" in r.reasons
        assert r.coverage == pytest.approx(0.5)

    def test_institutional_bearish_lean(self):
        r = r5_score(institutional_net_delta=-100_000.0)
        assert r.score == -20.0
        assert "institutional bearish lean" in r.reasons

    def test_a_thin_lean_is_present_and_scores_nothing(self):
        r = r5_score(institutional_net_delta=24_999.0)
        assert r.available and r.score == 0.0 and r.coverage == pytest.approx(0.5)
        assert r.reasons == ["thin institutional lean"]

    def test_no_signal_unavailable_not_zero(self):
        """No flow, no short interest, no peer stats -> unavailable (None, never 0)."""
        r = r5_score()
        assert not r.available
        assert r.score is None
        assert r.coverage == 0.0

    def test_weight_config_is_consistent(self):
        assert RULE_WEIGHTS["rule1_drivers"] == 0.0
        # R5 is a TAG (no direction vote): the composite totals only R2+R3+R7+R8.
        assert abs(sum(w for w in RULE_WEIGHTS.values() if w > 0) - 0.65) < 1e-9

    def test_positioning_weight_is_zero_tag(self):
        # R5 no longer votes direction; its read is a drawer tag.
        assert RULE_WEIGHTS["rule5_positioning"] == 0.0
        assert RULE_WEIGHTS["rule8_sentiment"] == 0.10


class TestInstitutionalFlowScale:
    def test_scaled_by_imbalance(self):
        # net = 100k, sided total = 1m -> imbalance 0.1 -> +2.0
        r = r5_score(institutional_net_delta=100_000.0,
                     institutional_call_premium=550_000.0, institutional_put_premium=450_000.0)
        assert r.score == 2.0
        assert r.raw_metrics["inst_flow_imbalance"] == pytest.approx(0.1)

    def test_full_call_imbalance(self):
        r = r5_score(institutional_net_delta=100_000.0,
                     institutional_call_premium=100_000.0, institutional_put_premium=0.0)
        assert r.score == 20.0

    def test_fallback_flat_when_no_premiums(self):
        r = r5_score(institutional_net_delta=100_000.0)
        assert r.score == 20.0

    def test_zero_net_flow_is_not_a_signal(self):
        r = r5_score(institutional_net_delta=0.0)
        assert not r.available
        assert r.score is None


class TestNoRetailEuphoriaInR5:
    """Retail-call euphoria is scored by R8 only — R5 must not double-fade it."""

    def test_retail_share_args_are_not_accepted(self):
        import inspect
        params = inspect.signature(r5_score).parameters
        assert "retail_net_delta" not in params


class TestZPeer:
    def test_none_with_fewer_than_two_peer_values(self):
        assert _z_peer(5.0, [5.0]) is None

    def test_none_when_value_is_none(self):
        assert _z_peer(None, [5.0, 6.0]) is None

    def test_zero_when_peer_values_have_no_spread(self):
        assert _z_peer(5.0, [5.0, 5.0, 5.0]) == 0.0


class TestPeerStatsRegistration:
    def setup_method(self):
        reset_peer_stats()

    def teardown_method(self):
        reset_peer_stats()

    def test_register_accumulates_per_sector(self):
        register_peer_stats("AAA", "Widgets", {"si_pct": 5.0, "borrow_fee": 1.0,
                                               "utilization": 20.0, "days_to_cover": 1.0})
        register_peer_stats("BBB", "Widgets", {"si_pct": 6.0, "borrow_fee": 1.2,
                                               "utilization": 22.0, "days_to_cover": 1.2})
        assert PEER_STATS["Widgets"]["si"] == [5.0, 6.0]
        assert PEER_STATS["Widgets"]["borrow"] == [1.0, 1.2]

    def test_reset_clears_all_groups(self):
        register_peer_stats("AAA", "Widgets", {"si_pct": 5.0})
        reset_peer_stats()
        assert PEER_STATS == {}

    def test_register_skips_missing_keys(self):
        register_peer_stats("AAA", "Widgets", {"si_pct": 5.0})
        assert PEER_STATS["Widgets"]["si"] == [5.0]
        assert PEER_STATS["Widgets"]["borrow"] == []


class TestSqueezeZScoreBuckets:
    """Peer-relative short-interest/borrow/utilization/DTC Z-score squeeze logic."""

    def setup_method(self):
        reset_peer_stats()
        for si, borrow, util, dtc in [(5, 1, 20, 1), (6, 1.2, 22, 1.2), (5.5, 0.9, 21, 1.1)]:
            register_peer_stats("PEER", "Widgets",
                                {"si_pct": si, "borrow_fee": borrow,
                                 "utilization": util, "days_to_cover": dtc})

    def teardown_method(self):
        reset_peer_stats()

    def test_extreme_squeeze_bucket(self):
        r = r5_score(short_interest_pct_float=9, days_to_cover=2, borrow_fee=2, utilization=30,
                     sector="Widgets")
        assert r.available
        assert r.raw_metrics["squeeze_z_score"] > 3
        assert r.score == 40.0
        assert any("extreme squeeze potential" in reason for reason in r.reasons)

    def test_mild_positive_squeeze_bucket(self):
        r = r5_score(short_interest_pct_float=6.7, days_to_cover=1.15, borrow_fee=1.13, utilization=22,
                     sector="Widgets")
        assert r.available
        assert 1 < r.raw_metrics["squeeze_z_score"] <= 2
        assert r.score == 15.0
        assert any("mild squeeze risk" in reason for reason in r.reasons)

    def test_negative_squeeze_bucket(self):
        r = r5_score(short_interest_pct_float=3, days_to_cover=0.5, borrow_fee=0.5, utilization=15,
                     sector="Widgets")
        assert r.available
        assert r.raw_metrics["squeeze_z_score"] < -1
        assert r.score == -10.0
        assert any("no squeeze pressure" in reason for reason in r.reasons)

    def test_another_sector_does_not_borrow_the_peers(self):
        """#266: the peer group is the ticker's sector; a sector nobody registered in has no Z."""
        r = r5_score(short_interest_pct_float=9, days_to_cover=2, borrow_fee=2, utilization=30,
                     sector="Gadgets")
        assert not r.available
        assert not any(k.startswith("z_") or k == "squeeze_z_score" for k in r.raw_metrics)

    def test_combined_flow_and_squeeze_full_coverage(self):
        r = r5_score(short_interest_pct_float=9, days_to_cover=2, borrow_fee=2, utilization=30,
                     sector="Widgets", institutional_net_delta=100_000.0)
        assert r.score == pytest.approx(60.0)  # +40 squeeze + 20 flow
        assert r.coverage == 1.0

    def test_renormalizes_weights_when_metric_missing(self):
        """Only SI peers present -> squeeze_z is just Z(short_interest)."""
        r = r5_score(short_interest_pct_float=9, days_to_cover=None, borrow_fee=None,
                     utilization=None, sector="Widgets")
        assert r.available
        assert "z_borrow" not in r.raw_metrics
        assert r.raw_metrics["squeeze_z_score"] == pytest.approx(r.raw_metrics["z_si"])

    def test_single_peer_sector_falls_back_to_absolute_si(self):
        """One in-sector peer cannot build a Z -> absolute SI fallback governs (#266: peers are per sector)."""
        reset_peer_stats()
        register_peer_stats("SOLO", "SmallGroup",
                            {"si_pct": 5.0, "borrow_fee": 1.0,
                             "utilization": 20.0, "days_to_cover": 1.0})
        low = r5_score(short_interest_pct_float=5.0, sector="SmallGroup")
        assert not low.available
        assert low.score is None
        high = r5_score(short_interest_pct_float=25.0, sector="SmallGroup")
        assert high.available
        assert high.score == 15.0
        assert any("high short interest" in reason for reason in high.reasons)


class TestAbsoluteShortInterestFallback:
    """No sector/peer stats -> absolute SI scores only above 10%."""

    def test_high_short_interest_scores(self):
        r = r5_score(short_interest_pct_float=25.0)
        assert r.available
        assert r.score == 15.0
        assert any("high short interest" in reason for reason in r.reasons)

    def test_moderate_short_interest_scores(self):
        r = r5_score(short_interest_pct_float=12.0)
        assert r.available
        assert r.score == 5.0
        assert any("moderate short interest" in reason for reason in r.reasons)

    def test_low_short_interest_is_not_a_signal(self):
        r = r5_score(short_interest_pct_float=5.0)
        assert not r.available
        assert r.score is None

    def test_no_si_returns_unavailable(self):
        r = r5_score()
        assert not r.available
        assert r.score is None
