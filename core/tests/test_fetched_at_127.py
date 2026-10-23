"""``fetched_at`` on the six Stage-2 tables that had no fetch stamp (#127).

Additive ``DOUBLE``, no schema bump, no backfill. A row that is not rewritten stays
NULL (unknown, never 0). Each writer stamps the moment it fetches.
"""
from __future__ import annotations

import time

import pytest

from RevenueChaser.core import rawstore

TABLES = (
    "chain_meta", "leaps_cache", "rotation_holdings",
    "sec13f_security_meta", "swing_info", "ticker_themes",
)


def _forget():
    rawstore._TABLE_READY.clear()
    rawstore._ENSURED.clear()


def _columns(table: str) -> dict[str, str]:
    import duckdb
    db = "edgar" if table == "chain_meta" else "raw"
    con = duckdb.connect(str(rawstore._db_path(db)), read_only=True)
    try:
        return dict(con.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ?",
            [table]).fetchall())
    finally:
        con.close()


def test_the_six_tables_declare_fetched_at_as_double_without_a_bump():
    for name in TABLES:
        decl = {d.split()[0]: d for d in rawstore.MANIFEST[name]["columns"]}
        assert decl.get("fetched_at") == "fetched_at DOUBLE", name
    assert rawstore.schema_version() == "12"


@pytest.mark.parametrize("name,row", [
    ("chain_meta", {"key": "last_sync", "value": "2026-01-01"}),
    ("leaps_cache", {"ticker": "AAA", "payload": {"latest": {"label": "Quiet"}}}),
    ("rotation_holdings", {"ticker": "XLK", "date": "2026-01-01", "rows": []}),
    ("sec13f_security_meta", {"ticker": "AAA", "entry": {"name": "Aaa"}}),
    ("swing_info", {"ticker": "AAA", "payload": {"sector": "Tech"}, "cached_at": 1.0}),
    ("ticker_themes", {"ticker": "AAA", "theme_id": "t", "theme": "T", "level": "fine",
                        "theme_run_id": "r1"}),
])
def test_an_old_row_keeps_a_null_fetched_at(isolate_rawstore, name, row):
    spec = rawstore.MANIFEST[name]
    mp = pytest.MonkeyPatch()
    try:
        mp.setitem(rawstore.MANIFEST, name, {
            **spec, "columns": [c for c in spec["columns"] if not c.startswith("fetched_at")]})
        _forget()
        rawstore.ensure_table(name)
        rawstore.write_rows(name, [row])
    finally:
        mp.undo()
    assert "fetched_at" not in _columns(name)
    _forget()
    rawstore.ensure_table(name)
    assert _columns(name)["fetched_at"] == "DOUBLE"
    got = rawstore.get_all(name)
    assert len(got) == 1
    assert got[0]["fetched_at"] is None
    assert rawstore.schema_version() == "12"


def _recent(stamp, before: float) -> None:
    assert isinstance(stamp, float) and stamp > 0
    assert before <= stamp <= time.time() + 1


def test_chain_meta_writer_stamps_fetched_at(rawstore_tmp_db):
    from RevenueChaser.module_value_chain.engine import record_misses, miss_row
    before = time.time()
    record_misses([miss_row("AAA", "no_filing", "1")])
    row = rawstore.get("chain_meta", "miss:AAA")
    _recent(row["fetched_at"], before)


def test_leaps_cache_writer_stamps_fetched_at(rawstore_tmp_db):
    from RevenueChaser.module_leaps.engine import save_cache
    before = time.time()
    save_cache("aaa", {"label": "Quiet"})
    row = rawstore.get("leaps_cache", "AAA")
    _recent(row["fetched_at"], before)
    assert row["payload"]["latest"]["label"] == "Quiet"


def test_rotation_holdings_writer_stamps_fetched_at(rawstore_tmp_db):
    from RevenueChaser.module_sector_rotation.store import save_holdings_cache
    before = time.time()
    save_holdings_cache("xlk", [{"ticker": "AAPL"}])
    row = rawstore.get("rotation_holdings", "XLK")
    _recent(row["fetched_at"], before)


def test_sec13f_security_meta_writer_stamps_fetched_at(rawstore_tmp_db):
    from RevenueChaser.module_sec_13f.engine import save_sec13f_meta
    before = time.time()
    save_sec13f_meta({"AAA": {"name": "Aaa", "sector": "Tech"}})
    row = rawstore.get("sec13f_security_meta", "AAA")
    _recent(row["fetched_at"], before)
    assert row["entry"]["name"] == "Aaa"


def test_swing_info_writer_stamps_fetched_at(rawstore_tmp_db, monkeypatch):
    import yfinance
    class _T:
        info = {"shortName": "Aaa"}
    monkeypatch.setattr(yfinance, "Ticker", lambda ticker: _T())
    from RevenueChaser.module_swing_potential.indicators import load_ticker_info
    before = time.time()
    assert load_ticker_info("AAA")["shortName"] == "Aaa"
    row = rawstore.get("swing_info", "AAA")
    _recent(row["fetched_at"], before)
    assert row["payload"]["shortName"] == "Aaa"


def test_ticker_themes_backfill_stamps_fetched_at(rawstore_tmp_db):
    from RevenueChaser.core import ticker_store, ticker_themes
    ticker_store.write_from_cards([{"ticker": "ETON", "themes": ["Biotechnology"]}])
    before = time.time()
    ticker_themes.backfill_legacy()
    row = rawstore.get_all("ticker_themes")[0]
    _recent(row["fetched_at"], before)
    assert row["ticker"] == "ETON"


def test_a_label_sync_does_not_wipe_fetched_at(rawstore_tmp_db):
    """``naming._sync_ticker_themes`` rewrites the label through write_rows. The stamp must survive."""
    from RevenueChaser.module_relatedness.naming import _sync_ticker_themes
    rawstore.write_rows("ticker_themes", [{
        "ticker": "AAA", "theme_id": "r1:f0", "theme": "old", "level": "fine",
        "theme_run_id": "r1", "fetched_at": 1_700_000_000.0,
    }])
    n = _sync_ticker_themes("r1", {"r1:f0": {"label": "New label"}})
    assert n == 1
    row = rawstore.get_all("ticker_themes")[0]
    assert row["theme"] == "New label"
    assert row["fetched_at"] == 1_700_000_000.0
