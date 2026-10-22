"""Public #17 (C1) — the Earnings Radar records live in `module_earnings_radar/models.py`.

Two invariants:
  1. the old home, `RevenueChaser.core.models.events`, is gone (no re-export shim:
     a shim keeps the board -> core edge the move exists to remove);
  2. the move was pure — every dataclass in the module keeps its field names,
     order and defaults, pinned below from the file as it stood on 3670e76.

Defaults are pinned by marker: `REQUIRED` (no default), `FACTORY:<name>`
(a `field(default_factory=...)` reference), or the literal default value.
"""
import dataclasses
import importlib

import pytest


def test_old_home_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("RevenueChaser.core.models.events")


def test_new_home_imports_the_records():
    from RevenueChaser.module_earnings_radar.models import RuleResult, TradeCard

    assert dataclasses.is_dataclass(TradeCard)
    assert dataclasses.is_dataclass(RuleResult)


def _spec(cls):
    """[(name, marker)] — REQUIRED / FACTORY:<name> / literal default."""
    out = []
    for f in dataclasses.fields(cls):
        if f.default is not dataclasses.MISSING:
            out.append((f.name, f.default))
        elif f.default_factory is not dataclasses.MISSING:
            out.append((f.name, "FACTORY:" + f.default_factory.__name__))
        else:
            out.append((f.name, "REQUIRED"))
    return out


# ---------------------------------------------------------------- expected ---

RULE_RESULT = [
    ("score", "REQUIRED"),
    ("available", "REQUIRED"),
    ("coverage", "REQUIRED"),
    ("reasons", "FACTORY:list"),
    ("flags", "FACTORY:list"),
    ("raw_metrics", "FACTORY:dict"),
]

EARNINGS_EVENT = [
    ("event_id", "REQUIRED"),
    ("ticker", "REQUIRED"),
    ("earnings_date", "REQUIRED"),
    ("earnings_time", "REQUIRED"),
    ("fiscal_quarter", "REQUIRED"),
    ("gics_sector", "REQUIRED"),
    ("industry_group", "REQUIRED"),
    ("sub_industry", "REQUIRED"),
    ("cluster", "REQUIRED"),
    ("report_status", "upcoming"),
    ("created_at", "FACTORY:now"),
    ("updated_at", "FACTORY:now"),
]

FUNDAMENTALS_QUARTERLY = [
    ("ticker", "REQUIRED"),
    ("fiscal_period", "REQUIRED"),
    ("as_of_timestamp", "REQUIRED"),
    ("revenue", None),
    ("revenue_growth_yoy", None),
    ("gross_margin", None),
    ("operating_margin", None),
    ("inventory", None),
    ("rpo", None),
    ("billings", None),
    ("deferred_revenue", None),
    ("backlog", None),
    ("guidance_revenue_low", None),
    ("guidance_revenue_high", None),
    ("cash_from_operations", None),
    ("net_income", None),
    ("total_assets", None),
    ("nim", None),
    ("delinquency_rate", None),
    ("net_charge_off_rate", None),
    ("efficiency_ratio", None),
    ("hash_rate", None),
    ("cost_to_mine_btc", None),
    ("cash_balance", None),
    ("btc_reserves", None),
    ("gmv", None),
    ("net_bookings", None),
    ("take_rate", None),
    ("cac", None),
    ("ltv", None),
]

OPTIONS_SNAPSHOT = [
    ("event_id", "REQUIRED"),
    ("captured_at", "REQUIRED"),
    ("underlying_price", "REQUIRED"),
    ("atm_straddle", "REQUIRED"),
    ("expected_move_pct", "REQUIRED"),
    ("implied_volatility", "REQUIRED"),
    ("realized_volatility", "REQUIRED"),
    ("iv_rv_ratio", "REQUIRED"),
    ("put_25_delta_iv", "REQUIRED"),
    ("call_25_delta_iv", "REQUIRED"),
    ("put_call_skew", "REQUIRED"),
    ("open_interest", None),
    ("call_put_ratio", None),
    ("gamma_exposure", None),
    ("delta_exposure", None),
]

FLOW_SNAPSHOT = [
    ("event_id", "REQUIRED"),
    ("captured_at", "REQUIRED"),
    ("institutional_call_delta", None),
    ("institutional_put_delta", None),
    ("retail_call_delta", None),
    ("retail_put_delta", None),
    ("sweep_premium", None),
    ("block_premium", None),
    ("dark_pool_notional", None),
    ("dark_pool_volume", None),
    ("large_lot_call_share", None),
    ("large_lot_put_share", None),
    ("small_lot_call_share", None),
    ("small_lot_put_share", None),
]

RULE_SCORES = [
    ("event_id", "REQUIRED"),
    ("as_of_timestamp", "REQUIRED"),
    ("rule_1_score", 0.0),
    ("rule_2_score", 0.0),
    ("rule_3_score", 0.0),
    ("rule_4_volatility_score", 0.0),
    ("rule_5_score", 0.0),
    ("rule_6_macro_score", 0.0),
    ("rule_7_score", 0.0),
    ("rule_8_score", 0.0),
    ("direction_score", 0.0),
    ("volatility_edge_score", 0.0),
    ("confidence_score", 0.0),
    ("veto_flag", False),
    ("veto_reasons", ""),
    ("final_action", "WATCH_ONLY"),
    ("score_version", 1),
]

POST_EARNINGS_OUTCOME = [
    ("event_id", "REQUIRED"),
    ("actual_revenue", None),
    ("revenue_surprise_pct", None),
    ("actual_eps", None),
    ("eps_surprise_pct", None),
    ("guidance_change", None),
    ("after_hours_return_pct", None),
    ("next_open_return_pct", None),
    ("next_close_return_pct", None),
    ("day_5_return_pct", None),
    ("day_20_return_pct", None),
    ("realized_earnings_move_pct", None),
    ("prediction_correct_direction", False),
    ("expected_move_accuracy", None),
]

TRADE_CARD = [
    ("event_id", ""),
    ("ticker", ""),
    ("name", ""),
    ("company", ""),
    ("earnings_date", "FACTORY:today"),
    ("earnings_time", ""),
    ("report_time_label", ""),
    ("fiscal_quarter", ""),
    ("cluster", ""),
    ("sector", ""),
    ("final_action", "WATCH_ONLY"),
    ("direction_score", 0.0),
    ("r2_score", 0.0),
    ("r3_score", 0.0),
    ("r5_score", 0.0),
    ("r7_score", 0.0),
    ("r8_score", 0.0),
    ("r4_score", 0.0),
    ("bias_label", ""),
    ("conviction", ""),
    ("rule_audit", "FACTORY:dict"),
    ("volatility_edge_score", 0.0),
    ("confidence_score", 0.0),
    ("macro_veto", False),
    ("veto_reasons", ""),
    ("rule_contributions", "FACTORY:dict"),
    ("rule_availability", "FACTORY:dict"),
    ("expected_move_pct", 0.0),
    ("historical_realized_move_pct", 0.0),
    ("expected_move_zscore", 0.0),
    ("iv_rv_ratio", 0.0),
    ("put_call_skew", 0.0),
    ("institutional_net_delta", 0.0),
    ("retail_net_delta", 0.0),
    ("key_thesis", ""),
    ("invalidation_conditions", ""),
    ("suggested_structure", ""),
    ("data_completeness", 0.0),
    ("nrc_score", 0.0),
    ("nrc_tier", ""),
    ("eps_estimate", ""),
    ("earn_m4_pct", ""),
    ("earn_m3_pct", ""),
    ("earn_m2_pct", ""),
    ("earn_m1_pct", ""),
    ("earn_m4_eps", ""),
    ("earn_m3_eps", ""),
    ("earn_m2_eps", ""),
    ("earn_m1_eps", ""),
    ("earn_current_pct", ""),
    ("pre_score", 0.0),
    ("filter_status", ""),
    ("filter_reasons", ""),
    ("last_er_label", ""),
    ("return_since_er", ""),
    ("since_er_chart", ""),
    ("past_1d", ""),
    ("past_1w", ""),
    ("past_2w", ""),
    ("return_4w", ""),
    ("last_beat", ""),
    ("stock_volume", ""),
    ("option_volume", ""),
    ("market_cap", ""),
    ("total_open_interest", ""),
    ("rotation_quadrant", ""),
    ("rotation_score", 0.0),
    ("current_price", ""),
    ("gex_call_wall", ""),
    ("gex_put_wall", ""),
    ("gex_gamma_flip", ""),
    ("gex_gamma_magnet", ""),
    ("detail_json", ""),
    ("trade_read", ""),
    ("trade_read_reason", ""),
    ("edge_ratio", 0.0),
    ("edge_median_realized", 0.0),
    ("liquidity_label", ""),
    ("badges_json", ""),
    ("attention_score", 0.0),
    ("inst_flow_label", ""),
    ("inst_flow_net", 0.0),
    ("inst_flow_tone", ""),
    ("inst_flow_prints", 0),
    ("inst_flow_span_hours", None),
    ("gex_structure", ""),
    ("inst_call_premium", 0.0),
    ("inst_put_premium", 0.0),
    ("sweep_premium", 0.0),
    ("block_premium", 0.0),
    ("raw_packet_json", ""),
    ("flow_source", ""),
    ("agh_points", None),
    ("agh_accel", None),
    ("agh_owners", ""),
    ("agh_dilution", False),
    ("leaps_label", ""),
    ("leaps_as_of", ""),
    ("leaps_score", 0),
    ("leaps_far_call_oi", 0),
    ("leaps_far_put_oi", 0),
    ("leaps_far_call_share", None),
    ("leaps_growth_1d_pct", None),
    ("leaps_growth_q_pct", None),
    ("leaps_iv_rank", None),
]

EXPECTED = {
    "RuleResult": RULE_RESULT,
    "EarningsEvent": EARNINGS_EVENT,
    "FundamentalsQuarterly": FUNDAMENTALS_QUARTERLY,
    "OptionsSnapshot": OPTIONS_SNAPSHOT,
    "FlowSnapshot": FLOW_SNAPSHOT,
    "RuleScores": RULE_SCORES,
    "PostEarningsOutcome": POST_EARNINGS_OUTCOME,
    "TradeCard": TRADE_CARD,
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_field_parity(name):
    import RevenueChaser.module_earnings_radar.models as models

    assert _spec(getattr(models, name)) == EXPECTED[name]
