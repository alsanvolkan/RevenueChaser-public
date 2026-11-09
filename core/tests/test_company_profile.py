"""``company_profile`` VIEW — grain + scalar-join contract."""
from __future__ import annotations

from RevenueChaser.core import company_profile as cp
from RevenueChaser.core import rawstore


def _seed():
    rawstore.replace_all("equity_universe", [
        {"ticker": "AAPL", "name": "Apple", "sector": "Technology"},
        {"ticker": "MSFT", "name": "Microsoft", "sector": "Technology"},
    ])
    rawstore.write_rows("ticker_scores", [{"ticker": "AAPL", "total_points": 10}])
    rawstore.write_rows("fundamentals_scores", [{"ticker": "AAPL", "ro40": 50.0}])
    rawstore.write_rows("swing_scores", [{"ticker": "AAPL", "quality": 80.0}])


def test_one_row_per_universe_ticker(rawstore_tmp_db):
    _seed()
    n = cp.count()
    distinct = rawstore.query(
        "SELECT count(DISTINCT ticker) n FROM company_profile")[0]["n"]
    assert n == distinct == rawstore.count("equity_universe") == 2


def test_scalar_joins(rawstore_tmp_db):
    _seed()
    a = cp.get("aapl")
    assert a["name"] == "Apple"
    assert a["ro40"] == 50.0
    assert a["swing_quality"] == 80.0
    assert a["total_points"] == 10
    # a universe ticker with no score rows still gets one row (LEFT JOINs)
    m = cp.get("MSFT")
    assert m is not None and m["total_points"] is None and m["ro40"] is None


def test_query_by_tickers(rawstore_tmp_db):
    _seed()
    rows = cp.query(["aapl", "msft"])
    assert {r["ticker"] for r in rows} == {"AAPL", "MSFT"}
