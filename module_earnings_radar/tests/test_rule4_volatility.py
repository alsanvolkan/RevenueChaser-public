"""Tests for R4 volatility edge.

Pins: EM buckets, IV/RV polarity (CHEAP vol => edge, RICH vol => penalty for a
premium buyer), GEX short-gamma boost / long-gamma dampen only when spot is
real (no fake $100 regime), historical straddle-payoff term, and coverage
counting which of {EM, IV/RV, GEX, history} were present.
"""
from RevenueChaser.module_earnings_radar.rules.rule4_volatility import score as r4_score


class TestRule4Availability:
    def test_no_options_data_unavailable(self):
        r = r4_score(None)
        assert r.available is False
        assert r.score is None

    def test_no_expected_move_unavailable(self):
        # IV/GEX alone cannot gate options vs shares — an EM is required.
        r = r4_score({"underlying_price": 40.0, "implied_volatility": 0.3})
        assert r.available is False
        assert r.score is None


class TestExpectedMove:
    def test_em_buckets(self):
        assert r4_score({"expected_move_pct": 13.0}).score == 25.0
        assert r4_score({"expected_move_pct": 8.0}).score == 15.0
        assert r4_score({"expected_move_pct": 5.0}).score == 5.0
        assert r4_score({"expected_move_pct": 1.5}).score == 0.0  # 5 - 10 clamped
        assert r4_score({"expected_move_pct": 5.0}).coverage == 0.25


class TestIvRvPolarity:
    def test_rich_vol_is_a_penalty_for_premium_buyer(self):
        # EM 5 (+5); IV/RV = 3.0 >= 1.25 -> -5 (rich) => 0
        r = r4_score({"expected_move_pct": 5.0,
                      "implied_volatility": 0.9, "realized_volatility": 0.3})
        assert r.score == 0.0
        assert any("rich" in x for x in r.reasons)

    def test_rich_ceiling_is_calibrated_1_25_not_1_5(self):
        # IV/RV 1.25 exactly -> the skill-calibrated rich penalty fires now.
        r = r4_score({"expected_move_pct": 5.0,
                      "implied_volatility": 0.375, "realized_volatility": 0.3})
        assert r.score == 0.0  # +5 EM, -5 rich
        assert any("rich" in x for x in r.reasons)

    def test_ivrv_between_1_0_and_1_25_is_neutral_but_present(self):
        # IV/RV = 1.1 -> no rich/cheap points, yet the pair still counts as an
        # IV/RV coverage part (data was present, verdict neutral).
        r = r4_score({"expected_move_pct": 5.0,
                      "implied_volatility": 0.33, "realized_volatility": 0.3})
        assert r.score == 5.0
        assert not any("IV/RV" in x for x in r.reasons)
        assert r.coverage == 0.5

    def test_cheap_vol_is_the_edge(self):
        # EM 5 (+5); IV/RV = 0.67 < 1.0 -> +10 (cheap) => 15
        r = r4_score({"expected_move_pct": 5.0,
                      "implied_volatility": 0.2, "realized_volatility": 0.3})
        assert r.score == 15.0
        assert any("cheap" in x for x in r.reasons)

    def test_missing_iv_or_rv_skips_the_ratio(self):
        r = r4_score({"expected_move_pct": 5.0, "implied_volatility": 0.3})
        assert r.score == 5.0  # EM only
        assert not any("IV/RV" in x for x in r.reasons)
        assert r.coverage == 0.25


class TestRule4GexRegime:
    def test_short_gamma_boosts_edge(self):
        r = r4_score({"underlying_price": 40.0, "expected_move_pct": 5.0, "gamma_flip": "50"})
        assert r.score == 20.0  # +5 EM, +15 short gamma
        assert any("short gamma" in x for x in r.reasons)

    def test_long_gamma_dampens_edge(self):
        r = r4_score({"underlying_price": 60.0, "expected_move_pct": 5.0, "gamma_flip": "50"})
        assert r.score == 0.0  # +5 EM, -10 long gamma -> clamped to 0
        assert any("long gamma" in x for x in r.reasons)

    def test_no_spot_skips_gamma_entirely(self):
        # No fake $100: without a real spot there is no GEX regime.
        r = r4_score({"expected_move_pct": 5.0, "gamma_flip": "50"})
        assert r.score == 5.0
        assert not any("gamma" in x for x in r.reasons)


class TestHistoricalTerm:
    def test_straddles_paid_historically_adds_edge(self):
        r = r4_score({"expected_move_pct": 5.0, "straddle_payoff_median": 3.0})
        assert r.score == 15.0  # +5 EM, +10 straddles paid
        assert any("paid" in x for x in r.reasons)

    def test_straddle_buyers_lost_historically_penalises(self):
        r = r4_score({"expected_move_pct": 5.0, "straddle_payoff_median": -2.0})
        assert r.score == 0.0  # +5 EM, -10 -> clamped 0
        assert any("lost" in x for x in r.reasons)

    def test_fallback_hist_move_ratio(self):
        r = r4_score({"expected_move_pct": 5.0, "hist_move_ratio": 1.4})
        assert r.score == 15.0  # +5 EM, +10 history > implied
        assert any("moves > implied" in x for x in r.reasons)

    def test_coverage_counts_history_part(self):
        # EM + straddle history -> 2/4 = 0.5
        r = r4_score({"expected_move_pct": 5.0, "straddle_payoff_median": 3.0})
        assert r.coverage == 0.5


class TestIvRankInformational:
    def test_high_iv_rank_adds_reason_not_points(self):
        r = r4_score({"expected_move_pct": 5.0, "iv_rank": 92.0})
        assert r.score == 5.0  # no points from iv_rank
        assert any("iv_rank" in x for x in r.reasons)


def _profile(regime=None, magnet=None, charm=None, vanna=None, flip=None):
    p = {}
    if regime:
        p["regime"] = regime
    if flip is not None:
        p["flip"] = flip
    if magnet:
        p["magnet"] = magnet
    if charm:
        p["charm"] = charm
    if vanna:
        p["vanna"] = vanna
    return p


class TestRule4GexProfile:
    def test_volume_book_short_gamma_regime(self):
        # profile regime short wins over an OI-flip that would say long.
        r = r4_score({
            "underlying_price": 66.83, "expected_move_pct": 7.5,
            "gamma_flip": "40",  # misleading OI flip far below spot
            "gex_profile": _profile(regime="short"),
        })
        assert r.score == 30.0  # +15 EM +15 short gamma
        assert any("volume book" in x and "short" in x for x in r.reasons)

    def test_volume_book_long_gamma_regime(self):
        r = r4_score({
            "underlying_price": 66.83, "expected_move_pct": 7.5,
            "gamma_flip": "90",  # misleading OI flip far above spot
            "gex_profile": _profile(regime="long"),
        })
        assert r.score == 5.0  # +15 EM -10 long gamma
        assert any("volume book" in x and "long" in x for x in r.reasons)

    def test_magnet_below_spot_adds_points(self):
        r = r4_score({
            "underlying_price": 66.83, "expected_move_pct": 5.0,
            "gex_profile": _profile(magnet={
                "strike": 65.0, "side": "below", "distance_pct": -0.027,
            }),
        })
        # +5 EM +8 magnet below = 13 (no regime -> no clamp interaction)
        assert r.score == 13.0
        assert any("magnet" in x and "below" in x for x in r.reasons)

    def test_magnet_above_spot_penalises(self):
        r = r4_score({
            "underlying_price": 66.83, "expected_move_pct": 5.0,
            "gex_profile": _profile(regime="short", magnet={
                "strike": 70.0, "side": "above", "distance_pct": 0.047,
            }),
        })
        # +5 EM +15 short gamma -8 magnet above = 12
        assert r.score == 12.0
        assert any("magnet" in x and "above" in x for x in r.reasons)

    def test_far_magnet_is_reason_only(self):
        r = r4_score({
            "underlying_price": 66.83, "expected_move_pct": 5.0,
            "gex_profile": _profile(regime="short", magnet={
                "strike": 80.0, "side": "above", "distance_pct": 0.197,
            }),
        })
        # magnet beyond 5% -> no points, reason only
        assert r.score == 20.0  # +5 EM +15 short gamma
        assert any("0.197" in x or "away" in x for x in r.reasons)

    def test_gex_block_clamped(self):
        # regime short + magnet below would be +23 raw -> clamp to +20.
        r = r4_score({
            "underlying_price": 66.83, "expected_move_pct": 5.0,
            "gex_profile": _profile(regime="short", magnet={
                "strike": 65.0, "side": "below", "distance_pct": -0.027,
            }),
        })
        assert r.score == 25.0  # +5 EM +20 (clamped GEX block)

    def test_charm_and_vanna_reasons_no_points(self):
        r = r4_score({
            "underlying_price": 66.83, "expected_move_pct": 5.0,
            "gex_profile": _profile(
                regime="short",
                charm={"label": "Charm Peak", "strike": 80.0, "signed": -88121.0},
                vanna={"put_peak": {"strike": 60.0, "signed": -2403.0},
                       "call_peak": {"strike": 80.0, "signed": 252055.0}},
            ),
        })
        assert r.score == 20.0  # +5 EM +15 short gamma
        assert any("charm peak" in x and "80.00" in x for x in r.reasons)
        assert any("vanna" in x and "put lobe" in x and "60.00" in x for x in r.reasons)

    def test_profile_coverage_counts_as_gex_part(self):
        r = r4_score({
            "underlying_price": 66.83, "expected_move_pct": 5.0,
            "gex_profile": _profile(regime="short"),
        })
        assert r.coverage == 0.5  # EM + GEX


class TestTermStructureSlope:
    """Term-structure slope points (backwardation = buyer drag, contango = edge).

    Scored but deliberately NOT a coverage part — coverage stays the shipped
    four-part {EM, IV/RV, GEX, history} set.
    """

    def test_deep_backwardation_is_a_buyer_penalty(self):
        # EM 8 (+15); slope -0.005 <= -0.00406 -> -8 => 7
        r = r4_score({"expected_move_pct": 8.0, "ts_slope": -0.005})
        assert r.score == 7.0
        assert any("backwardated" in x for x in r.reasons)

    def test_mild_backwardation_is_a_smaller_penalty(self):
        # EM 8 (+15); -0.003 in (-0.00406, -0.002] -> -3 => 12
        r = r4_score({"expected_move_pct": 8.0, "ts_slope": -0.003})
        assert r.score == 12.0
        assert any("mildly backwardated" in x for x in r.reasons)

    def test_contango_is_the_edge(self):
        # EM 8 (+15); slope +0.002 > 0 -> +5 => 20
        r = r4_score({"expected_move_pct": 8.0, "ts_slope": 0.002})
        assert r.score == 20.0
        assert any("contango" in x for x in r.reasons)

    def test_near_flat_slope_gets_no_points(self):
        # -0.001 is above -0.002 and not > 0 -> nothing.
        r = r4_score({"expected_move_pct": 8.0, "ts_slope": -0.001})
        assert r.score == 15.0
        assert not any("term structure" in x for x in r.reasons)

    def test_missing_slope_contributes_nothing(self):
        r = r4_score({"expected_move_pct": 8.0})
        assert r.score == 15.0
        assert not any("term structure" in x for x in r.reasons)
        assert r.raw_metrics.get("ts_slope") is None

    def test_slope_does_not_change_coverage(self):
        r = r4_score({"expected_move_pct": 8.0, "ts_slope": -0.005})
        assert r.coverage == 0.25  # EM alone still covers 1 of 4 parts

    def test_garbage_slope_is_ignored(self):
        r = r4_score({"expected_move_pct": 8.0, "ts_slope": "n/a"})
        assert r.score == 15.0
        assert r.raw_metrics.get("ts_slope") is None
