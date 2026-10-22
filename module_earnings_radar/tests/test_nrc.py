"""NRC — live slots only (no 50-fills). Tiers A≥70 / B 55–69 / C 40–54 / Ignore <40.
"""
import pytest

from RevenueChaser.module_earnings_radar.models import TradeCard
from RevenueChaser.module_earnings_radar.pipeline import (
    compute_nrc_score, nrc_acceleration, nrc_quality, nrc_from_card, _agh_context,
)


def test_no_slots_is_ignore():
    nrc, tier = compute_nrc_score()
    assert nrc == 0.0
    assert tier == "Ignore"


def test_all_100_tier_a():
    nrc, tier = compute_nrc_score(growth=100, quality=100, beat=100, acceleration=100)
    assert nrc == 100.0
    assert tier == "Tier A — Revenue Catalyst"


def test_tier_boundaries():
    assert compute_nrc_score(growth=70, quality=70, beat=70, acceleration=70)[1] == "Tier A — Revenue Catalyst"
    assert compute_nrc_score(growth=69, quality=69, beat=69, acceleration=69)[1] == "Tier B — Watchlist"
    assert compute_nrc_score(growth=40, quality=40, beat=40, acceleration=40)[1] == "Tier C — Emerging"
    assert compute_nrc_score(growth=39, quality=39, beat=39, acceleration=39)[1] == "Ignore"


def test_missing_accel_does_not_fill_50():
    """Only growth=100 → NRC 100, not pulled toward 50."""
    nrc, _ = compute_nrc_score(growth=100)
    assert nrc == 100.0


def test_quality_below_12_caps_at_49():
    nrc, tier = compute_nrc_score(growth=100, quality=5, beat=100, acceleration=100)
    assert nrc == 49.0
    assert tier == "Tier C — Emerging"


def test_quality_at_12_no_cap():
    nrc, _ = compute_nrc_score(growth=100, quality=12, beat=100, acceleration=100)
    assert nrc > 49.0


def test_rotation_bonus_applied():
    nrc, _ = compute_nrc_score(growth=100, quality=100, beat=100, acceleration=100,
                               rotation_bonus=10)
    assert nrc == 100.0  # clamped at 100


def test_clamped_to_100():
    nrc, _ = compute_nrc_score(growth=200, quality=200, beat=200)
    assert nrc == 100.0


def test_nrc_acceleration_from_agh_flag():
    assert nrc_acceleration(TradeCard(agh_accel=True)) == 80.0
    assert nrc_acceleration(TradeCard(agh_accel=False)) == 25.0
    assert nrc_acceleration(TradeCard()) is None


def test_nrc_quality_from_r7():
    card = TradeCard(rule_audit={"rule7_accounting": {"available": True, "score": 20}})
    assert nrc_quality(card) == pytest.approx(60.0)  # (20+100)/2
    assert nrc_quality(TradeCard()) is None


def test_nrc_from_card_skips_unknown_accel(rawstore_tmp_db):
    card = TradeCard(ticker="AAA", direction_score=0, key_thesis="", rotation_quadrant="")
    d = nrc_from_card(card)
    assert d["acceleration"] is None
    assert d["quality"] is None
    assert d["growth"] == 50.0


def test_agh_context_from_ticker_store(rawstore_tmp_db):
    from RevenueChaser.core import ticker_store
    ticker_store.write_from_cards([{
        "ticker": "AAA", "name": "Aaa", "sector": "Technology",
        "total_points": 7, "dilution_warning": True,
        "inst_buy_flow": 100, "inst_sell_flow": 10,
        "signals": [{"key": "growth", "subs": [
            {"label": "Growth accelerating YoY", "param": "accelerating", "passed": True},
        ]}],
    }])
    ctx = _agh_context("AAA")
    assert ctx["agh_points"] == 7
    assert ctx["agh_accel"] is True
    assert ctx["agh_owners"] == "Accumulating"
    assert ctx["agh_dilution"] is True
    empty = _agh_context("ZZZ")
    assert empty["agh_points"] is None
    assert empty["agh_accel"] is None
