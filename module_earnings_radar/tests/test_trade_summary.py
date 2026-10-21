"""Tests for Phase 1B trade summary helpers (edge, liquidity, trade read, attention)."""
from datetime import date

from RevenueChaser.module_earnings_radar.pipeline import _liquidity_label, _compute_edge, _trade_read, _attention_score


class TestLiquidityLabel:
    def test_excellent(self):
        assert _liquidity_label(20000, 20000) == "Excellent"

    def test_tradeable(self):
        assert _liquidity_label(1000, 500) == "Tradeable"

    def test_thin(self):
        assert _liquidity_label(100, 100) == "Thin"


class TestComputeEdge:
    def test_edge_ratio(self):
        hist = [
            {"report_date": "2026-05-01", "actual_eps": 0.1, "post_earnings_move_1d": 0.05},
            {"report_date": "2026-02-01", "actual_eps": 0.1, "post_earnings_move_1d": -0.03},
            {"report_date": "2025-11-01", "actual_eps": 0.1, "post_earnings_move_1d": 0.04},
        ]
        median, ratio = _compute_edge(hist, 10.0)
        assert median == 4.0
        assert abs(ratio - 0.4) < 1e-9

    def test_insufficient_actuals(self):
        hist = [{"report_date": "2026-05-01", "actual_eps": 0.1, "post_earnings_move_1d": 0.05}]
        median, ratio = _compute_edge(hist, 10.0)
        assert median is None and ratio is None

    def test_no_implied(self):
        hist = [
            {"report_date": "2026-05-01", "actual_eps": 0.1, "post_earnings_move_1d": 0.05},
            {"report_date": "2026-02-01", "actual_eps": 0.1, "post_earnings_move_1d": -0.03},
            {"report_date": "2025-11-01", "actual_eps": 0.1, "post_earnings_move_1d": 0.04},
        ]
        median, ratio = _compute_edge(hist, 0)
        assert median is None and ratio is None

    def test_median_absolute_not_signed(self):
        hist = [
            {"report_date": "2026-05-01", "actual_eps": 0.1, "post_earnings_move_1d": 0.12},
            {"report_date": "2026-02-01", "actual_eps": 0.1, "post_earnings_move_1d": -0.12},
            {"report_date": "2025-11-01", "actual_eps": 0.1, "post_earnings_move_1d": 0.01},
        ]
        median, _ = _compute_edge(hist, 10.0)
        assert median == 12.0


class TestTradeRead:
    def test_avoid_binary(self):
        assert _trade_read(20, 60, False, True, "Excellent") == "AVOID"

    def test_avoid_veto(self):
        assert _trade_read(20, 60, True, False, "Excellent") == "AVOID"

    def test_avoid_thin(self):
        assert _trade_read(20, 60, False, False, "Thin") == "AVOID"

    def test_bullish(self):
        assert _trade_read(20, 60, False, False, "Excellent") == "BULLISH"

    def test_bearish(self):
        assert _trade_read(-20, 60, False, False, "Tradeable") == "BEARISH"

    def test_neutral(self):
        assert _trade_read(5, 60, False, False, "Excellent") == "NEUTRAL"

    def test_watch_low_confidence(self):
        assert _trade_read(20, 30, False, False, "Excellent") == "WATCH"


class TestAttentionScore:
    def test_today_leading_excellent(self):
        today = date.today()
        # 0.35*100 + 0.25*100 + 0.20*100 + 0.10*100 + 0.10*100 = 100
        assert _attention_score(100, 100, "Excellent", "LEADING", today) == 100.0

    def test_low_everything(self):
        today = date.today()
        # 0.35*0 + 0.25*0 + 0.20*20 + 0.10*0 + 0.10*100 = 4 + 10 = 14
        assert _attention_score(0, 0, "Thin", "LAGGING", today) == 14.0

    def test_far_future_proximity_zero(self):
        future = date.today().replace(year=date.today().year + 1)
        # 0.35*100 + 0.25*0 + 0.20*100 + 0.10*100 + 0.10*0 = 35 + 20 + 10 = 65
        assert _attention_score(100, 0, "Excellent", "LEADING", future) == 65.0
