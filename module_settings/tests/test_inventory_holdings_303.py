"""Data Health row "Rotation ETF holdings" (#303, D25): store only, zero network."""
from __future__ import annotations

from datetime import datetime, timezone

from RevenueChaser.core import rawstore
from RevenueChaser.module_settings import inventory
from RevenueChaser.module_settings.tests.test_inventory import FIXED, _rows, _seed

HOLD = [{"ticker": "NVDA", "name": "NVIDIA", "weight": 14.2}]
FRI = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc).timestamp()      # Friday after the close: session 10-09


def test_holdings_row_on_an_empty_store(rawstore_tmp_db):
    _seed()
    row = _rows(inventory.collect(now=FIXED))["etf_holdings"]
    assert row["label"] == "Rotation ETF holdings"
    assert row["tables"] == ["etf_holdings", "etf_holdings_changes"]
    assert row["rows"] == 0 and row["last_date"] == "—" and row["fetch"] is None
    assert row["holes"] == 39 and row["hole_label"] == "ETFs with no holdings"
    assert "no holdings yet" in row["note"]


def test_holdings_row_on_a_seeded_store(rawstore_tmp_db):
    from RevenueChaser.module_sector_rotation import store as rotation_store
    _seed()
    rawstore.write_rows("etf_holdings", [
        {"ticker": "XLK", "entry": HOLD, "provider": "uw", "fetched_at": FRI},
        {"ticker": "SKYY", "entry": HOLD, "provider": "yfinance", "fetched_at": FRI},
        {"ticker": "XLE", "entry": HOLD, "provider": "uw", "fetched_at": FRI - 7 * 86400},
    ])
    rotation_store.save_holdings_cache("SKYY", [{"ticker": "NVDA", "rs_1m": 1.0}])
    row = _rows(inventory.collect(now=FIXED))["etf_holdings"]
    assert row["rows"] == 3 and row["last_date"] == "2026-10-09"
    assert row["holes"] == 36
    assert "2 of 39 ETFs on session 2026-10-09" in row["note"]
    assert "UW 2 / yfinance 1" in row["note"]
    assert "RS stored 1 of 28 theme ETFs" in row["note"]
    assert "no holdings: AIQ" in row["note"]                             # an ETF with no row is named


def test_a_failed_summary_says_so(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_sector_rotation import etf_holdings
    _seed()
    monkeypatch.setattr(etf_holdings, "health_summary", lambda: 1 / 0)
    row = _rows(inventory.collect(now=FIXED))["etf_holdings"]
    assert "summary failed" in row["note"] and "no holdings yet" not in row["note"]


def test_holdings_queries_are_pinned():
    pairs = {("etf_holdings", "ticker"), ("etf_holdings", "entry"), ("etf_holdings", "provider"), ("etf_holdings", "fetched_at"),
             ("rotation_holdings", "ticker"), ("rotation_holdings", "rows")}
    assert pairs <= set(inventory.QUERIED_COLUMNS)
