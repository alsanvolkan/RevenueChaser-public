"""Shared ticker store testleri (raw.duckdb tmp path)."""
from __future__ import annotations

import pytest

from RevenueChaser.core import ticker_store


@pytest.fixture
def store(rawstore_tmp_db):
    return ticker_store


def _cards():
    return [
        {
            "ticker": "ETON", "name": "Eton Pharmaceuticals, Inc.", "sector": "Healthcare",
            "industry": "Biotechnology", "price": 63.47, "market_cap": 1.8e9,
            "ps_ratio": 22.69, "rev_growth": 1.049, "gross_margin": 0.5345,
            "fcf": 1.02e7, "runway_years": 999.0, "rd_intensity": 0.097,
            "dilution_yoy": 0.039, "earnings_date": 1794513600.0, "total_points": 9,
            "themes": ["Biotechnology"], "tech_breakthrough": False,
            "dilution_warning": False, "us_listed": True, "source": "sec_edgar",
            "inst_holders": None, "inst_new": None, "inst_exited": None,
            "inst_buy_flow": None, "inst_sell_flow": None, "inst_shares": None,
            "inst_shares_delta_pct": None, "inst_ownership_pct": 0.45,
            "signals": [{"key": "growth", "name": "Accelerated Growth", "passed": 3, "total": 3,
                         "subs": [{"label": "Revenue growth ≥ 20%", "value": 1.049, "unit": "pct",
                                   "threshold": "≥ 20%", "op": "gte", "param": "rev_growth", "passed": True}]}],
        },
        {
            "ticker": "AAPL", "name": "Apple Inc.", "sector": "Technology",
            "industry": "Consumer Electronics", "price": 250.0, "market_cap": 3.7e12,
            "ps_ratio": 9.0, "rev_growth": 0.05, "gross_margin": 0.46,
            "fcf": 1.0e11, "runway_years": 999.0, "rd_intensity": 0.08,
            "dilution_yoy": -0.02, "earnings_date": 1794513600.0, "total_points": 5,
            "themes": [], "tech_breakthrough": False,
            "dilution_warning": False, "us_listed": True, "source": "sec_edgar",
            "inst_holders": 6232, "inst_new": 185, "inst_exited": 253,
            "inst_buy_flow": 1.3e9, "inst_sell_flow": 2.2e9, "inst_shares": 9.46e9,
            "inst_shares_delta_pct": -0.01, "inst_ownership_pct": None,
            "signals": [],
        },
    ]


def test_write_and_get(store):
    store.write_from_cards(_cards(), asof="2026-08-22T00:00:00+00:00")
    assert store.count() == 2
    assert store.asof() == "2026-08-22T00:00:00+00:00"

    eton = store.get("ETON")
    assert eton is not None
    assert eton["total_points"] == 9
    assert eton["themes"] == ["Biotechnology"]
    assert eton["tech_breakthrough"] is False
    assert eton["us_listed"] is True
    assert eton["signals"][0]["passed"] == 3
    assert eton["signals"][0]["subs"][0]["value"] == 1.049

    aapl = store.get("aapl")  # case-insensitive
    assert aapl["inst_holders"] == 6232
    assert aapl["inst_shares_delta_pct"] == -0.01


def test_get_missing(store):
    store.write_from_cards(_cards(), asof="2026-08-22T00:00:00+00:00")
    assert store.get("NOPE") is None


def test_list_tickers(store):
    store.write_from_cards(_cards(), asof="2026-08-22T00:00:00+00:00")
    rows = store.list_tickers()
    assert {r["ticker"] for r in rows} == {"ETON", "AAPL"}
    assert rows[0]["ticker"] == "AAPL"  # sorted


def test_overwrite_replaces(store):
    store.write_from_cards(_cards(), asof="2026-08-22T00:00:00+00:00")
    store.write_from_cards(_cards()[:1], asof="2026-08-23T00:00:00+00:00")
    assert store.count() == 1
    assert store.get("AAPL") is None
    assert store.asof() == "2026-08-23T00:00:00+00:00"


def test_empty_write_does_not_wipe_the_store(store):
    """A cold-cache model-only scan returns no cards; it must not clear data."""
    store.write_from_cards(_cards(), asof="2026-08-22T00:00:00+00:00")
    store.write_from_cards([], asof="2026-08-23T00:00:00+00:00")
    assert store.count() == 2
    assert store.get("AAPL") is not None
    assert store.asof() == "2026-08-22T00:00:00+00:00"  # asof not advanced either

