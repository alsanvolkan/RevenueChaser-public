"""Tests for R5 positioning scoring (the institutional lean; the squeeze tag is in ``test_r5_ownership_store_g9``).

Pin: the institutional lean (bullish minus bearish print-side premium, review S4) scales with the sided premium
and is Thin under $25k; retail-call euphoria is
NOT scored here (it lives in R8); unavailable is None when no signal exists.
"""
import pytest

from RevenueChaser.module_earnings_radar.rules.rule5_positioning import score as r5_score
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
        """No flow and no squeeze field -> unavailable (None, never 0)."""
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

