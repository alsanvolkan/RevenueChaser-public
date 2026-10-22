from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional, Any


@dataclass
class RuleResult:
    score: Optional[float]
    available: bool
    coverage: float
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    raw_metrics: dict[str, Any] = field(default_factory=dict)

    def display_score(self) -> float:
        return self.score if self.score is not None else 0.0


@dataclass
class EarningsEvent:
    event_id: str
    ticker: str
    earnings_date: date
    earnings_time: str
    fiscal_quarter: str
    gics_sector: str
    industry_group: str
    sub_industry: str
    cluster: str
    report_status: str = "upcoming"
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class FundamentalsQuarterly:
    ticker: str
    fiscal_period: str
    as_of_timestamp: datetime
    revenue: Optional[float] = None
    revenue_growth_yoy: Optional[float] = None
    gross_margin: Optional[float] = None
    operating_margin: Optional[float] = None
    inventory: Optional[float] = None
    rpo: Optional[float] = None
    billings: Optional[float] = None
    deferred_revenue: Optional[float] = None
    backlog: Optional[float] = None
    guidance_revenue_low: Optional[float] = None
    guidance_revenue_high: Optional[float] = None
    cash_from_operations: Optional[float] = None
    net_income: Optional[float] = None
    total_assets: Optional[float] = None
    nim: Optional[float] = None
    delinquency_rate: Optional[float] = None
    net_charge_off_rate: Optional[float] = None
    efficiency_ratio: Optional[float] = None
    hash_rate: Optional[float] = None
    cost_to_mine_btc: Optional[float] = None
    cash_balance: Optional[float] = None
    btc_reserves: Optional[float] = None
    gmv: Optional[float] = None
    net_bookings: Optional[float] = None
    take_rate: Optional[float] = None
    cac: Optional[float] = None
    ltv: Optional[float] = None


@dataclass
class OptionsSnapshot:
    event_id: str
    captured_at: datetime
    underlying_price: float
    atm_straddle: float
    expected_move_pct: float
    implied_volatility: float
    realized_volatility: float
    iv_rv_ratio: float
    put_25_delta_iv: float
    call_25_delta_iv: float
    put_call_skew: float
    open_interest: Optional[float] = None
    call_put_ratio: Optional[float] = None
    gamma_exposure: Optional[float] = None
    delta_exposure: Optional[float] = None


@dataclass
class FlowSnapshot:
    event_id: str
    captured_at: datetime
    institutional_call_delta: Optional[float] = None
    institutional_put_delta: Optional[float] = None
    retail_call_delta: Optional[float] = None
    retail_put_delta: Optional[float] = None
    sweep_premium: Optional[float] = None
    block_premium: Optional[float] = None
    dark_pool_notional: Optional[float] = None
    dark_pool_volume: Optional[float] = None
    large_lot_call_share: Optional[float] = None
    large_lot_put_share: Optional[float] = None
    small_lot_call_share: Optional[float] = None
    small_lot_put_share: Optional[float] = None


@dataclass
class RuleScores:
    event_id: str
    as_of_timestamp: datetime
    rule_1_score: float = 0.0
    rule_2_score: float = 0.0
    rule_3_score: float = 0.0
    rule_4_volatility_score: float = 0.0
    rule_5_score: float = 0.0
    rule_6_macro_score: float = 0.0
    rule_7_score: float = 0.0
    rule_8_score: float = 0.0
    direction_score: float = 0.0
    volatility_edge_score: float = 0.0
    confidence_score: float = 0.0
    veto_flag: bool = False
    veto_reasons: str = ""
    final_action: str = "WATCH_ONLY"
    score_version: int = 1


@dataclass
class PostEarningsOutcome:
    event_id: str
    actual_revenue: Optional[float] = None
    revenue_surprise_pct: Optional[float] = None
    actual_eps: Optional[float] = None
    eps_surprise_pct: Optional[float] = None
    guidance_change: Optional[float] = None
    after_hours_return_pct: Optional[float] = None
    next_open_return_pct: Optional[float] = None
    next_close_return_pct: Optional[float] = None
    day_5_return_pct: Optional[float] = None
    day_20_return_pct: Optional[float] = None
    realized_earnings_move_pct: Optional[float] = None
    prediction_correct_direction: bool = False
    expected_move_accuracy: Optional[float] = None


@dataclass
class TradeCard:
    event_id: str = ""
    ticker: str = ""
    name: str = ""
    company: str = ""
    earnings_date: date = field(default_factory=date.today)
    earnings_time: str = ""
    report_time_label: str = ""
    fiscal_quarter: str = ""
    cluster: str = ""
    # The spine sector the card was scored against (E3, #372 ER-4): not the UW calendar's sector.
    sector: str = ""
    final_action: str = "WATCH_ONLY"
    direction_score: float = 0.0
    r2_score: float = 0.0
    r3_score: float = 0.0
    r5_score: float = 0.0
    r7_score: float = 0.0
    r8_score: float = 0.0
    r4_score: float = 0.0
    bias_label: str = ""
    conviction: str = ""
    rule_audit: dict = field(default_factory=dict)
    volatility_edge_score: float = 0.0
    confidence_score: float = 0.0
    macro_veto: bool = False
    veto_reasons: str = ""
    rule_contributions: dict = field(default_factory=dict)
    rule_availability: dict = field(default_factory=dict)
    expected_move_pct: float = 0.0
    historical_realized_move_pct: float = 0.0
    expected_move_zscore: float = 0.0
    iv_rv_ratio: float = 0.0
    put_call_skew: float = 0.0
    institutional_net_delta: float = 0.0
    retail_net_delta: float = 0.0
    key_thesis: str = ""
    invalidation_conditions: str = ""
    suggested_structure: str = ""
    data_completeness: float = 0.0
    nrc_score: float = 0.0
    nrc_tier: str = ""
    eps_estimate: str = ""
    earn_m4_pct: str = ""
    earn_m3_pct: str = ""
    earn_m2_pct: str = ""
    earn_m1_pct: str = ""
    earn_m4_eps: str = ""
    earn_m3_eps: str = ""
    earn_m2_eps: str = ""
    earn_m1_eps: str = ""
    earn_current_pct: str = ""
    pre_score: float = 0.0
    filter_status: str = ""
    filter_reasons: str = ""
    last_er_label: str = ""
    return_since_er: str = ""
    since_er_chart: str = ""
    past_1d: str = ""
    past_1w: str = ""
    past_2w: str = ""
    return_4w: str = ""
    last_beat: str = ""
    stock_volume: str = ""
    option_volume: str = ""
    market_cap: str = ""
    total_open_interest: str = ""
    rotation_quadrant: str = ""
    rotation_score: float = 0.0
    current_price: str = ""
    gex_call_wall: str = ""
    gex_put_wall: str = ""
    gex_gamma_flip: str = ""
    gex_gamma_magnet: str = ""
    detail_json: str = ""
    trade_read: str = ""
    trade_read_reason: str = ""
    edge_ratio: float = 0.0
    edge_median_realized: float = 0.0
    liquidity_label: str = ""
    badges_json: str = ""
    attention_score: float = 0.0
    inst_flow_label: str = ""
    inst_flow_net: float = 0.0
    inst_flow_tone: str = ""
    inst_flow_prints: int = 0
    inst_flow_span_hours: Optional[float] = None
    gex_structure: str = ""
    inst_call_premium: float = 0.0
    inst_put_premium: float = 0.0
    sweep_premium: float = 0.0
    block_premium: float = 0.0
    raw_packet_json: str = ""
    flow_source: str = ""
    agh_points: Optional[int] = None
    agh_accel: Optional[bool] = None
    agh_owners: str = ""
    agh_dilution: bool = False
    leaps_label: str = ""
    leaps_as_of: str = ""
    leaps_score: int = 0
    leaps_far_call_oi: int = 0
    leaps_far_put_oi: int = 0
    leaps_far_call_share: Optional[float] = None
    leaps_growth_1d_pct: Optional[float] = None
    leaps_growth_q_pct: Optional[float] = None
    leaps_iv_rank: Optional[float] = None
