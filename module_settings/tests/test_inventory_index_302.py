"""Data Health: the index lists row (#302, D24): each list against its floor, the last change, Nasdaq vs SEC."""
from __future__ import annotations

from RevenueChaser.core import rawstore
from RevenueChaser.module_settings import inventory


def _rows(payload):
    return {r["key"]: r for r in payload["rows"]}


def test_index_row_on_an_empty_store(rawstore_tmp_db):
    row = _rows(inventory.collect())["index"]
    assert row["tables"] == ["index_constituents", "index_constituents_changes"]
    assert row["last_date"] == inventory.DASH
    assert row["note"] == ("sp500 0 / floor 400 · dow 0 / 25 · nasdaq 0 / 1,000 · sp600 0 / 480; "
                           "no change recorded; Nasdaq vs SEC exchange file: not stored yet")


def test_index_row_shows_each_list_its_last_change_and_the_sec_nasdaq_count(rawstore_tmp_db):
    from RevenueChaser.core import index_registry as ir
    with rawstore.change_label("uw", run_id="r1"):
        ir.save_index("sp500", {"AAPL", "MSFT"})
    with rawstore.change_label("uw", run_id="r2"):
        ir.save_index("sp500", {"AAPL", "NVDA"})            # NVDA joins, MSFT leaves: 2 rows in run r2
        ir.save_index("dow", {"AAPL"})                      # same run: 1 more
    rawstore.write_rows("sec_exchange_file", [
        {"ticker": "AAPL", "exchange": "Nasdaq", "status": "present", "run_at": 1.0},
        {"ticker": "MSFT", "exchange": "Nasdaq", "status": "present", "run_at": 1.0},
        {"ticker": "MSFT", "exchange": "Nasdaq", "status": "absent", "run_at": 2.0},
        {"ticker": "IBM", "exchange": "NYSE", "status": "present", "run_at": 1.0},
    ])
    row = _rows(inventory.collect())["index"]
    newest = rawstore.query("SELECT max(run_at) AS m FROM index_constituents_changes")[0]["m"]
    assert row["last_date"] == inventory._fmt_date(newest) != inventory.DASH
    assert row["note"] == (f"sp500 2 / floor 400 · dow 1 / 25 · nasdaq 0 / 1,000 · sp600 0 / 480; "
                           f"last change {row['last_date']} (+3); Nasdaq vs SEC exchange file: 0 / 1")


def test_index_row_queries_are_pinned():
    pairs = {("index_constituents", "slug"), ("index_constituents_changes", "run_id"),
             ("index_constituents_changes", "run_at"), ("sec_exchange_file", "ticker"),
             ("sec_exchange_file", "exchange"), ("sec_exchange_file", "status"), ("sec_exchange_file", "run_at")}
    assert pairs <= set(inventory.QUERIED_COLUMNS)
