"""Tests for composite / decision math (direction, confidence, bias, conviction, select_action)."""
import pytest

from RevenueChaser.module_earnings_radar.composite import (
    classify_bias,
    classify_conviction,
    compute_composite_scores,
    compute_confidence_score,
    compute_direction_score,
    recommend_options_structure,
    select_action,
)
from RevenueChaser.core.models.events import RuleResult


def _rr(score_val, available=True, coverage=0.6):
    return RuleResult(score=score_val, available=available, coverage=coverage)


class TestComputeDirectionScore:
    def test_all_unavailable_is_zero(self):
        assert compute_direction_score({"rule2_guidance": _rr(None, False)}) == 0.0

    def test_missing_rules_do_not_pull_to_zero(self):
        # rule2 weight 0.20, score 50 -> 50 (missing rules ignored)
        d = compute_direction_score({"rule2_guidance": _rr(50.0)})
        assert d == 50.0

    def test_weighted_average(self):
        d = compute_direction_score({
            "rule2_guidance": _rr(50.0),   # w 0.20
            "rule7_accounting": _rr(-20.0),  # w 0.15
        })
        assert d == pytest.approx((0.20 * 50 + 0.15 * -20) / 0.35)  # 22.9

    def test_clamped_to_range(self):
        d = compute_direction_score({"rule2_guidance": _rr(500.0)})
        assert d == 100.0

    def test_rounds_to_one_decimal(self):
        d = compute_direction_score({"rule2_guidance": _rr(33.33)})
        assert d == pytest.approx(33.3)


class TestComputeConfidenceScore:
    def test_none_available_zero(self):
        assert compute_confidence_score({"rule2_guidance": _rr(None, False)}) == 0.0

    def test_coverage_weighted(self):
        c = compute_confidence_score({"rule2_guidance": _rr(50.0, coverage=0.6)})
        # w=0.20*0.6 / total 0.65 (R2+R3+R7+R8, R5=tag) = 0.12/0.65
        assert c == pytest.approx(100 * 0.12 / 0.65, abs=0.1)

    def test_r5_tag_does_not_count_in_confidence(self):
        # R5 weight is 0 (tag), so its coverage must not inflate confidence.
        c = compute_confidence_score({
            "rule2_guidance": _rr(50.0, coverage=0.6),
            "rule5_positioning": _rr(30.0, coverage=1.0),  # tag, w 0
        })
        assert c == pytest.approx(100 * 0.12 / 0.65, abs=0.1)

    def test_disagreement_only_r3_vs_r7(self):
        # R2 bullish vs R3 bearish is NOT a confidence multiplier (headline only).
        c = compute_confidence_score({
            "rule2_guidance": _rr(50.0, coverage=1.0),
            "rule3_financial": _rr(-50.0, coverage=1.0),
        })
        assert c == pytest.approx(100 * (0.40 / 0.65), abs=0.1)

    def test_disagreement_r3_r7_opposite_signs_penalises(self):
        c = compute_confidence_score({
            "rule3_financial": _rr(-50.0, coverage=1.0),
            "rule7_accounting": _rr(20.0, coverage=1.0),
        })
        # (0.20 + 0.15) / 0.65 * (1 - 0.2)
        assert c == pytest.approx(100 * (0.35 / 0.65) * 0.8, abs=0.1)

    def test_same_sign_r3_r7_no_penalty(self):
        c = compute_confidence_score({
            "rule3_financial": _rr(-50.0, coverage=1.0),
            "rule7_accounting": _rr(-20.0, coverage=1.0),
        })
        assert c == pytest.approx(100 * (0.35 / 0.65), abs=0.1)


class TestClassifyBias:
    def test_bullish(self):
        assert classify_bias(12) == "BULLISH_LEAN"
        assert classify_bias(50) == "BULLISH_LEAN"

    def test_bearish(self):
        assert classify_bias(-12) == "BEARISH_LEAN"
        assert classify_bias(-50) == "BEARISH_LEAN"

    def test_neutral(self):
        assert classify_bias(0) == "NEUTRAL"
        assert classify_bias(11.9) == "NEUTRAL"


class TestClassifyConviction:
    def test_high(self):
        rr = {"rule2_guidance": _rr(30), "rule3_financial": _rr(30),
              "rule7_accounting": _rr(30)}
        assert classify_conviction(30, 70, rr) == "HIGH"

    def test_r5_tag_does_not_count_as_direction_vote(self):
        # R5 (tag, weight 0) must not push conviction HIGH on its own.
        rr = {"rule2_guidance": _rr(30), "rule5_positioning": _rr(30)}
        assert classify_conviction(30, 70, rr) == "LOW"

    def test_medium(self):
        rr = {"rule2_guidance": _rr(20), "rule3_financial": _rr(20)}
        assert classify_conviction(20, 50, rr) == "MEDIUM"

    def test_low(self):
        rr = {"rule2_guidance": _rr(5)}
        assert classify_conviction(5, 40, rr) == "LOW"


class TestSelectAction:
    def test_veto_forces_no_trade(self):
        assert select_action(80, 60, veto_flag=True, confidence_score=90) == "NO_TRADE"

    def test_small_direction_watch(self):
        assert select_action(11, 0, veto_flag=False, confidence_score=90) == "WATCH"

    def test_low_coverage_watch(self):
        assert select_action(20, 0, veto_flag=False, confidence_score=49) == "WATCH_LOW_COVERAGE"

    def test_options_bull(self):
        assert select_action(25, 60, veto_flag=False, confidence_score=60) == "OPTIONS_BULL"

    def test_options_gate_opens_exactly_at_vol_edge_50(self):
        assert select_action(25, 50, veto_flag=False, confidence_score=60) == "OPTIONS_BULL"
        assert select_action(25, 49, veto_flag=False, confidence_score=60) == "SHARES_BULL"

    def test_options_bear(self):
        assert select_action(-25, 60, veto_flag=False, confidence_score=60) == "OPTIONS_BEAR"

    def test_shares_bull(self):
        assert select_action(18, 0, veto_flag=False, confidence_score=60) == "SHARES_BULL"

    def test_shares_bear(self):
        assert select_action(-18, 0, veto_flag=False, confidence_score=60) == "SHARES_BEAR"

    def test_directional_watch(self):
        assert select_action(13, 0, veto_flag=False, confidence_score=60) == "DIRECTIONAL_WATCH"


class TestComputeCompositeScores:
    def test_single_available_rule(self):
        out = compute_composite_scores(r2=_rr(50.0, coverage=0.6))
        assert out["direction_score"] == 50.0
        assert out["volatility_edge_score"] == 0.0
        assert out["rule_results"]["rule2_guidance"].score == 50.0

    def test_r4_volatility_edge(self):
        out = compute_composite_scores(r4=RuleResult(score=80, available=True, coverage=1.0))
        assert out["volatility_edge_score"] == 80.0

    def test_rule1_zero_weight_does_not_move_direction(self):
        # rule1_drivers is reserved at weight 0.0: a score from it alone must not
        # move direction. Restoring a weight > 0 in RULE_WEIGHTS re-activates it.
        out = compute_composite_scores(r1=_rr(100.0))
        assert out["direction_score"] == 0.0


class TestRecommendOptionsStructure:
    def test_no_gex_returns_empty(self):
        assert recommend_options_structure(20, 50.0, {}) == ""

    def test_no_spot_returns_empty(self):
        assert recommend_options_structure(20, None, {"gamma_flip": "50"}) == ""

    def test_short_gamma_bullish(self):
        r = recommend_options_structure(20, 40.0, {"gamma_flip": "50"})
        assert r == "LONG CALL / CALL DEBIT SPREAD"

    def test_short_gamma_bearish(self):
        r = recommend_options_structure(-20, 40.0, {"gamma_flip": "50"})
        assert r == "LONG PUT / PUT DEBIT SPREAD"

    def test_short_gamma_neutral(self):
        r = recommend_options_structure(0, 40.0, {"gamma_flip": "50"})
        assert r == "LONG STRADDLE / STRANGLE"

    def test_long_gamma_bullish(self):
        r = recommend_options_structure(20, 60.0, {"gamma_flip": "50"})
        assert r == "BULL PUT SPREAD"

    def test_long_gamma_bearish(self):
        r = recommend_options_structure(-20, 60.0, {"gamma_flip": "50"})
        assert r == "BEAR CALL SPREAD"

    def test_long_gamma_neutral(self):
        r = recommend_options_structure(0, 60.0, {"gamma_flip": "50"})
        assert r == "IRON CONDOR / SELL STRADDLE"

    def test_profile_regime_short_wins_over_stale_oi_flip(self):
        # gex-levels flip says long (spot 66.8 > 40), volume profile says short.
        r = recommend_options_structure(-20, 66.83, {"gamma_flip": "40"},
                                        {"regime": "short"})
        assert r == "LONG PUT / PUT DEBIT SPREAD"

    def test_profile_bearish_target_band_appended(self):
        r = recommend_options_structure(-20, 66.83, {"gamma_flip": "40"},
                                        {"regime": "short",
                                         "magnet": {"strike": 65.0, "side": "below"},
                                         "vanna": {"put_peak": {"strike": 60.0, "signed": -2403.0}}})
        assert "LONG PUT / PUT DEBIT SPREAD" in r
        assert "target magnet $65.00, put wall/lobe $60.00" in r

    def test_profile_no_regime_falls_back_to_flip(self):
        # spot 66.83 > gex-levels flip 40 -> long gamma -> BEAR CALL SPREAD.
        r = recommend_options_structure(-20, 66.83, {"gamma_flip": "40"},
                                        {"magnet": {"strike": 65.0, "side": "below"}})
        assert r == "BEAR CALL SPREAD · target magnet $65.00"


class TestRuleWeightGuard:
    def test_every_weighted_rule_has_a_scorer(self):
        # A rule with weight > 0 but no scorer silently cuts every card's confidence
        # (its weight stays in the denominator and no result ever fills it).
        import importlib
        from RevenueChaser.core.config import RULE_WEIGHTS

        for name, weight in RULE_WEIGHTS.items():
            if weight <= 0:
                continue
            try:
                mod = importlib.import_module(f"RevenueChaser.module_earnings_radar.rules.{name}")
            except ImportError:
                pytest.fail(f"{name} has weight {weight} but no scorer module rules/{name}.py")
            assert callable(getattr(mod, "score", None)), f"{name} has weight {weight} but no score()"

    def test_rule1_stays_at_zero_until_it_has_a_scorer(self):
        import importlib
        from RevenueChaser.core.config import RULE_WEIGHTS

        try:
            importlib.import_module("RevenueChaser.module_earnings_radar.rules.rule1_drivers")
        except ImportError:
            assert RULE_WEIGHTS["rule1_drivers"] == 0.0

    def test_rule1_drivers_is_logged_only(self):
        """E6 (#372): the scorer exists and reads estimates, but the composite weight stays 0 until the decision
        to wire it — a weight > 0 would move every card as soon as a snapshot is stored."""
        import importlib
        from RevenueChaser.core.config import RULE_WEIGHTS

        mod = importlib.import_module("RevenueChaser.module_earnings_radar.rules.rule1_drivers")
        assert callable(getattr(mod, "score", None))
        assert RULE_WEIGHTS["rule1_drivers"] == 0.0
