"""Data Health row "Rotation ETF holdings" (#303, D25; #642 PR 5: the spine's lists): store only, zero network."""
from __future__ import annotations

from RevenueChaser.module_settings import inventory
from RevenueChaser.module_settings.tests.test_inventory import FIXED, _rows, _seed
from RevenueChaser.rcdata.tests import spine_fixture

HOLD = [{"ticker": "NVDA", "name": "NVIDIA", "weight": 14.2}]


def test_holdings_row_on_an_empty_store(rawstore_tmp_db):
    _seed()
    row = _rows(inventory.collect(now=FIXED))["etf_holdings"]
    assert row["label"] == "Rotation ETF holdings"
    assert row["tables"] == ["spine etf_holdings"]
    assert row["rows"] == 0 and row["last_date"] == "—" and row["fetch"] is None
    assert row["holes"] == 34 and row["hole_label"] == "ETFs with no holdings"
    assert "no holdings yet" in row["note"]


def test_holdings_row_on_a_seeded_spine(rawstore_tmp_db):
    from RevenueChaser.module_sector_rotation import store as rotation_store
    _seed()
    spine_fixture.write_etf_holdings({"XLK": HOLD}, as_of="2026-10-09", provider="ssga")
    spine_fixture.write_etf_holdings({"SKYY": HOLD}, as_of="2026-10-09", provider="uw")
    spine_fixture.write_etf_holdings({"XLE": HOLD}, as_of="2026-10-02", provider="ssga")
    rotation_store.save_holdings_cache("SKYY", [{"ticker": "NVDA", "rs_1m": 1.0}])
    row = _rows(inventory.collect(now=FIXED))["etf_holdings"]
    assert row["rows"] == 3 and row["last_date"] == "2026-10-09"
    assert row["holes"] == 31
    assert "2 of 34 ETFs on session 2026-10-09" in row["note"]
    assert "SPDR 2 / Unusual Whales 1" in row["note"]
    assert "relative strength stored 1 of 23 theme ETFs" in row["note"]
    assert "no holdings: AIQ" in row["note"]                             # an ETF with no list is named


def test_a_failed_summary_says_so(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_sector_rotation import etf_holdings
    _seed()
    monkeypatch.setattr(etf_holdings, "health_summary", lambda: 1 / 0)
    row = _rows(inventory.collect(now=FIXED))["etf_holdings"]
    assert "summary failed" in row["note"] and "no holdings yet" not in row["note"]


def test_holdings_queries_are_pinned():
    assert {("rotation_holdings", "ticker"), ("rotation_holdings", "rows")} <= set(inventory.QUERIED_COLUMNS)
