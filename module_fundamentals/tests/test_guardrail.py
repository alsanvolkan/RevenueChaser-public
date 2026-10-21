"""Fundamentals [DISTORTED] guardrail — table-driven, no network.

Pin: is_distorted (abs growth > 200% or TTM revenue < $10M), and that a
distorted candidate is FLAGGED but NOT dropped by build_payload.
"""
import pytest

from RevenueChaser.module_fundamentals.pipeline import build_payload
from RevenueChaser.module_fundamentals.screeners import is_distorted


@pytest.mark.parametrize("growth,rev,expected", [
    (2.5, 1e9, True),    # absurd growth
    (-2.5, 1e9, True),   # absurd decline
    (0.20, 5e6, True),   # micro revenue base < $10M
    (0.20, 1e9, False),  # healthy
    (1.9, 1e9, False),   # high but under threshold
    (None, None, False), # unknown data
    (2.0, 1e9, False),   # exactly at threshold (strict >)
])
def test_is_distorted(growth, rev, expected):
    assert is_distorted({"rev_growth_ttm": growth, "ttm_revenue": rev}) is expected


def test_distorted_not_dropped_from_payload():
    m = {
        "rev_growth_ttm": 0.25, "fcf_margin": 0.25, "ro40": 50.0, "ttm_revenue": 5e6,
        "gross_margin": 0.35, "roic": 0.2, "net_debt_ebitda": 1.0,
        "pe": 20.0, "pb": 3.0, "dist_52w_pct": 0.9, "rs_6m": 0.05, "above_200d": True,
    }
    row = {"ticker": "ZZZ", "name": "Z Corp", "price": 100.0, "market_cap": 5e8,
           "sector": "Technology", "industry": "Software", "themes": ["Software"],
           "earnings_date": None, "period": "Q2/2026 vs Q2/2025", "metrics": m}
    payload = build_payload([row], top_n=10)
    assert len(payload["candidates"]) == 1
    c = payload["candidates"][0]
    assert c["ticker"] == "ZZZ"
    assert c["distorted"] is True  # routed to manual review, not dropped
    assert payload["distorted_count"] == 1


def test_healthy_candidate_not_distorted():
    m = {
        "rev_growth_ttm": 0.25, "fcf_margin": 0.25, "ro40": 50.0, "ttm_revenue": 2e9,
        "gross_margin": 0.35, "roic": 0.2, "net_debt_ebitda": 1.0,
        "pe": 20.0, "pb": 3.0, "dist_52w_pct": 0.9, "rs_6m": 0.05, "above_200d": True,
    }
    row = {"ticker": "AAA", "name": "A Corp", "price": 100.0, "market_cap": 5e8,
           "sector": "Technology", "industry": "Software", "themes": ["Software"],
           "earnings_date": None, "period": "Q2/2026 vs Q2/2025", "metrics": m}
    payload = build_payload([row], top_n=10)
    assert payload["candidates"][0]["distorted"] is False
    assert payload["distorted_count"] == 0
