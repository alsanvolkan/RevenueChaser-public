"""R7 — Accounting quality. Table-driven, no network.

Pin: signed Sloan (positive accruals penalize, negative do not);
>10% -> -30 + flag, >5% -> -10, else +20; cash-rich (accruals < -5%) is
+20 with a distinct reason; no deferred-revenue term; unavailable is None.
"""
import pytest

from RevenueChaser.module_earnings_radar.rules.rule7_accounting import score


def test_empty_history_unavailable():
    r = score([])
    assert r.available is False
    assert r.score is None
    assert r.coverage == 0


def test_no_sloan_inputs_unavailable_not_zero():
    r = score([{"revenue": 1000}])
    assert r.available is False
    assert r.score is None
    assert r.coverage == 0


def test_zero_total_assets_unavailable():
    r = score([{"net_income": 100, "cash_from_operations": 90, "total_assets": 0}])
    assert r.available is False
    assert r.score is None


def test_low_accruals_positive():
    r = score([{"net_income": 100, "cash_from_operations": 90, "total_assets": 1000}])
    assert r.available is True
    assert r.score == 20.0
    assert r.coverage == 1.0
    assert any("low accruals" in x for x in r.reasons)


def test_moderate_positive_accruals_penalty():
    r = score([{"net_income": 100, "cash_from_operations": 40, "total_assets": 1000}])
    assert r.available is True
    assert r.score == -10.0  # accruals 0.06 > 0.05
    assert any("elevated accruals" in x for x in r.reasons)


def test_high_positive_accruals_heavy_penalty_and_flag():
    r = score([{"net_income": 110, "cash_from_operations": 50, "total_assets": 500}])
    assert r.available is True
    assert r.score == -30.0  # accruals 0.12 > 0.10
    assert any("Sloan accruals" in f for f in r.flags)


def test_cash_rich_negative_accruals_are_not_penalized():
    """CFO >> NI used to hit abs() and look like manipulation. It is +20."""
    r = score([{"net_income": 20, "cash_from_operations": 100, "total_assets": 1000}])
    assert r.available is True
    assert r.score == 20.0  # accruals -0.08
    assert any("cash well above earnings" in x for x in r.reasons)
    assert r.flags == []


def test_deferred_revenue_is_ignored():
    """DR lag is R3's job — R7 must not double-count it."""
    hist = [
        {"revenue": 1000, "deferred_revenue": 110,
         "net_income": 100, "cash_from_operations": 90, "total_assets": 1000},
        {"revenue": 800, "deferred_revenue": 100},
    ]
    r = score(hist)
    assert r.available is True
    assert r.score == 20.0
    assert not any("deferred" in x.lower() for x in r.reasons + r.flags)


def test_period_mismatch_unavailable():
    """IS net_income and cash-flow net_income disagree -> YTD-vs-quarter mix."""
    hist = [{
        "net_income": 52.9, "cash_net_income": -77.9,
        "cash_from_operations": 443.7, "total_assets": 12482.3,
    }]
    r = score(hist)
    assert r.available is False
    assert r.score is None
    assert any("period mismatch" in x for x in r.reasons)


def test_period_match_still_scores():
    """Same NI on IS and CF (COO Q2: -77.9 both) -> normal +20 path."""
    hist = [{
        "net_income": -77.9, "cash_net_income": -77.9,
        "cash_from_operations": 182.8, "total_assets": 12482.3,
    }]
    r = score(hist)
    assert r.available is True
    assert r.score == 20.0  # negative accruals -> cash rich


def test_missing_cash_net_income_does_not_block():
    """Cash-flow net_income absent (legacy payload) -> no cross-check, score on."""
    hist = [{"net_income": 100, "cash_from_operations": 90, "total_assets": 1000}]
    r = score(hist)
    assert r.available is True
    assert r.score == 20.0
