"""L2 contract — TradeCard / RuleResult schema for the 30-col radar.

Pin: every column the radar renders maps to a TradeCard dataclass field (all
defaulted, so `TradeCard()` always constructs), and RuleResult invariants
(unavailable rules carry `score=None`, never 0; 0 is display-only).
"""
from dataclasses import fields

from RevenueChaser.core.models.events import RuleResult, TradeCard

# Field names required by the 30-col grouped radar report.
RADAR_FIELDS = {
    # Identity
    "ticker", "name", "company", "cluster", "sector", "rotation_quadrant",
    "earnings_date", "earnings_time", "report_time_label",
    # Decision
    "confidence_score", "inst_flow_label", "bias_label", "final_action",
    "suggested_structure", "key_thesis",
    # Earnings setup
    "expected_move_pct", "edge_ratio", "eps_estimate",
    "earn_m4_pct", "earn_m3_pct", "earn_m2_pct", "earn_m1_pct", "earn_current_pct",
    "earn_m4_eps", "earn_m3_eps", "earn_m2_eps", "earn_m1_eps",
    "since_er_chart",
    # GEX & liquidity
    "gex_call_wall", "gex_put_wall", "gex_gamma_flip", "gex_gamma_magnet",
    "liquidity_label", "market_cap", "stock_volume", "option_volume",
    "total_open_interest",
    # Price history + AGH context (replaces TW candles)
    "past_1d", "past_1w", "past_2w", "return_4w",
    "agh_points", "agh_accel", "agh_owners", "agh_dilution",
    # Decision packet extras
    "direction_score", "volatility_edge_score", "macro_veto", "attention_score",
    # LEAPS label age (C26)
    "leaps_as_of",
}


def test_radar_fields_present_on_tradecard():
    present = {f.name for f in fields(TradeCard)}
    missing = RADAR_FIELDS - present
    assert not missing, f"radar fields missing from TradeCard: {sorted(missing)}"


def test_tradecard_constructs_with_defaults():
    card = TradeCard()
    assert card.ticker == ""
    assert card.final_action == "WATCH_ONLY"
    assert card.direction_score == 0.0


def test_ruleresult_unavailable_is_none_not_zero():
    r = RuleResult(score=None, available=False, coverage=0)
    assert r.score is None  # unavailable, never 0
    assert r.display_score() == 0.0  # 0 only for display


def test_ruleresult_available_score_used():
    r = RuleResult(score=45.0, available=True, coverage=0.6)
    assert r.display_score() == 45.0
