"""``update_columns`` — the UPDATE-only helper Phase B backfills through.

`equity_universe` is ``write: "upsert"``, so passing a partial row
(``{ticker, market_cap, price}``) to ``write_rows`` would overwrite *every*
non-key column from ``r.get(c)`` and NULL name/sector/GICS/quote (see
docs/data_health.md, "Feasibility review"). The backfill must instead touch only
the named columns, and only on rows whose key already exists — an absent key is
a no-op, never an insert.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore


def _seed() -> None:
    rawstore.replace_all("equity_universe", [
        {"ticker": "MU", "market_cap": 1.2e12, "sector": "Information Technology",
         "price": 900.0, "name": "Micron Technology, Inc.",
         "industry": "Semiconductors",
         "industry_group": "Semiconductors & Semiconductor Equipment",
         "sub_industry": "Semiconductors",
         "quote": {"symbol": "MU", "regularMarketPrice": 900.0}},
        {"ticker": "AAA", "market_cap": 1e9, "sector": "Technology", "price": 10.0,
         "name": "AAA Corp", "quote": {"symbol": "AAA"}},
    ])


def test_update_columns_changes_only_the_named_columns(rawstore_tmp_db):
    _seed()
    n = rawstore.update_columns("equity_universe", "ticker",
                                [{"ticker": "MU", "market_cap": 1.25e12, "price": 925.57}],
                                ["market_cap", "price"])
    assert n == 1
    row = rawstore.get("equity_universe", "MU")
    assert row["market_cap"] == 1.25e12
    assert row["price"] == 925.57
    # untouched columns keep their stored values
    assert row["name"] == "Micron Technology, Inc."
    assert row["sector"] == "Information Technology"
    assert row["industry_group"] == "Semiconductors & Semiconductor Equipment"
    assert row["sub_industry"] == "Semiconductors"
    assert row["quote"] == {"symbol": "MU", "regularMarketPrice": 900.0}


def test_update_columns_does_not_insert_an_unknown_key(rawstore_tmp_db):
    _seed()
    rawstore.update_columns("equity_universe", "ticker",
                            [{"ticker": "NOPE", "market_cap": 1.0, "price": 2.0}],
                            ["market_cap", "price"])
    assert rawstore.count("equity_universe") == 2
    assert rawstore.get("equity_universe", "NOPE") is None


def test_update_columns_rejects_an_unknown_column(rawstore_tmp_db):
    _seed()
    with pytest.raises(rawstore.RawStoreError):
        rawstore.update_columns("equity_universe", "ticker",
                                [{"ticker": "MU", "nope": 1.0}], ["nope"])


def test_update_columns_rejects_an_unknown_key_column(rawstore_tmp_db):
    _seed()
    with pytest.raises(rawstore.RawStoreError):
        rawstore.update_columns("equity_universe", "nope",
                                [{"ticker": "MU", "market_cap": 1.0}], ["market_cap"])


def test_update_columns_returns_zero_for_no_rows(rawstore_tmp_db):
    _seed()
    assert rawstore.update_columns("equity_universe", "ticker", [], ["market_cap"]) == 0
