"""Schema 11 key rebuilds: ``price_bars_daily`` and ``orphan_lookups`` change PRIMARY KEY (history redesign C, J6 §5).

The two tables have no writer and no reader at v10, but a store may still hold rows. The first v11 call rebuilds a table
still at its v10 key, keeps every row, and is a no-op the second time. Since #156 (avo 2026-10-09) the same call then
moves the rebuilt ``price_bars_daily`` from raw.duckdb to prices.duckdb, so its rows are read there. Temp stores only.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore

V10_DDL = {
    "price_bars_daily": (
        "CREATE TABLE price_bars_daily (ticker TEXT, date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
        "adj_close DOUBLE, volume DOUBLE, split_ratio DOUBLE, source TEXT, fetched_at DOUBLE, "
        "PRIMARY KEY (ticker, date))"),
    "orphan_lookups": (
        "CREATE TABLE orphan_lookups (ticker TEXT PRIMARY KEY, sec_name TEXT, query TEXT, match TEXT, "
        "provider_status TEXT, n_rows INTEGER, candidates JSON, error TEXT, fetched_at DOUBLE)"),
}
FETCHED = 1_780_000_000.0      # 2026-05-28: inside the holiday table


@pytest.fixture
def v10_store(tmp_path, monkeypatch):
    """A raw store as v10 left it: the two tables at their old keys, holding rows; nothing else applied yet."""
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "raw.duckdb")
    rawstore._TABLE_READY.clear()
    rawstore._ENSURED.clear()
    with rawstore.connect() as conn:
        for ddl in V10_DDL.values():
            conn.execute(ddl)
        conn.execute("INSERT INTO price_bars_daily VALUES ('AAA', '2026-05-26', 1, 2, 0.5, 1.5, 1.4, 100, NULL, "
                     "'yf', ?), ('AAA', '2026-05-27', 1, 2, 0.5, 1.6, 1.5, 120, 2.0, 'yf', NULL)", [FETCHED])
        conn.execute("INSERT INTO orphan_lookups VALUES ('AAA', 'Aaa Inc', 'aaa', 'matched', 'ok', 3, NULL, NULL, ?), "
                     "('BBB', 'Bbb', 'bbb', 'no_hits', 'ok', 0, NULL, NULL, NULL)", [FETCHED])
    return tmp_path


def _pks(db: str = "raw") -> dict:
    with rawstore.connect(db=db) as c:
        return rawstore._live_primary_keys(c)


def test_the_first_v11_call_rekeys_both_tables_and_keeps_every_row(v10_store):
    assert _pks()["price_bars_daily"] == ["ticker", "date"]
    rawstore.ensure_all_tables()
    pks = _pks()
    assert "price_bars_daily" not in pks                                      # moved on to prices.duckdb
    assert _pks("prices")["price_bars_daily"] == ["ticker", "session_date"]
    assert pks["orphan_lookups"] == ["ticker", "session_date"]
    bars = rawstore.query("SELECT ticker, session_date::TEXT AS d, close, split_ratio, run_id, run_at "
                          "FROM price_bars_daily ORDER BY session_date", db="prices")
    assert [(b["d"], b["close"], b["split_ratio"]) for b in bars] == [("2026-05-26", 1.5, None), ("2026-05-27", 1.6, 2.0)]
    assert {b["run_id"] for b in bars} == {"schema-11"}
    assert bars[0]["run_at"] == FETCHED and bars[1]["run_at"] > FETCHED          # fetched_at, else the migration time
    look = rawstore.query("SELECT ticker, session_date::TEXT AS d, source, run_id, run_at FROM orphan_lookups "
                          "ORDER BY ticker")
    assert [(l["ticker"], l["source"], l["run_id"]) for l in look] == [("AAA", "fda_orphan", "schema-11"),
                                                                       ("BBB", "fda_orphan", "schema-11")]
    assert look[0]["d"] == "2026-05-28" and look[0]["run_at"] == FETCHED         # fetched_at is Thu 28 May 16:26 ET: that session
    assert look[1]["d"] is not None and look[1]["run_at"] > FETCHED               # no fetched_at: the migration session


def test_the_second_call_is_a_no_op_and_a_new_write_succeeds(v10_store):
    rawstore.ensure_all_tables()
    before = rawstore.query("SELECT * FROM price_bars_daily ORDER BY session_date", db="prices")
    assert len(before) == 2
    rawstore._TABLE_READY.clear()
    rawstore._ENSURED.clear()
    rawstore.ensure_all_tables()
    assert rawstore.query("SELECT * FROM price_bars_daily ORDER BY session_date", db="prices") == before
    rawstore.write_rows("price_bars_daily", [{"ticker": "AAA", "session_date": "2026-05-28", "close": 2.0}])
    assert rawstore.count("price_bars_daily") == 3
    marks = rawstore.query("SELECT dataset, source_count, verify FROM migration_manifest "
                           "WHERE dataset LIKE 'key_rebuild_v11:%' ORDER BY dataset")
    assert marks == [{"dataset": "key_rebuild_v11:orphan_lookups", "source_count": 2, "verify": "PASS"},
                     {"dataset": "key_rebuild_v11:price_bars_daily", "source_count": 2, "verify": "PASS"}]


def test_an_empty_table_is_rebuilt_too(tmp_path, monkeypatch):
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "raw.duckdb")
    rawstore._TABLE_READY.clear()
    rawstore._ENSURED.clear()
    with rawstore.connect() as conn:
        conn.execute(V10_DDL["price_bars_daily"])
    rawstore.ensure_all_tables()
    assert _pks("prices")["price_bars_daily"] == ["ticker", "session_date"] and rawstore.count("price_bars_daily") == 0


def test_a_live_key_that_is_neither_the_old_nor_the_new_one_stops_the_call(tmp_path, monkeypatch):
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "raw.duckdb")
    rawstore._TABLE_READY.clear()
    rawstore._ENSURED.clear()
    with rawstore.connect() as conn:
        conn.execute("CREATE TABLE price_bars_daily (ticker TEXT, date DATE, PRIMARY KEY (ticker))")
        conn.execute("INSERT INTO price_bars_daily VALUES ('AAA', '2026-05-26')")
    with pytest.raises(rawstore.RawStoreError, match="neither"):
        with rawstore.connect() as conn:
            rawstore._apply_key_rebuilds(conn)
    assert rawstore.query("SELECT ticker FROM price_bars_daily") == [{"ticker": "AAA"}]       # nothing was touched


def test_theme_runs_and_leaps_flow_prints_gain_their_columns(v10_store):
    rawstore.ensure_all_tables()
    cols = lambda t: {r["column_name"] for r in rawstore.query(      # noqa: E731
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [t])}
    assert {"session_date", "run_at"} <= cols("theme_runs")
    assert {"source", "fetched_at", "session_date", "run_at"} <= cols("leaps_flow_prints")


def test_the_mode_pins_of_06_section_4():
    m = rawstore.MANIFEST
    assert m["catalyst_events"]["history_mode"] == m["orphan_designations"]["history_mode"] == "change_history"
    assert m["orphan_lookups"]["history_mode"] == "event"


def test_a_row_written_between_the_rebuild_read_and_its_swap_is_never_dropped(tmp_path, monkeypatch):
    """The rebuild reads the old rows before its transaction; a row that lands in that gap must stop the swap (S2)."""
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "raw.duckdb")
    rawstore._TABLE_READY.clear()
    rawstore._ENSURED.clear()
    with rawstore.connect() as conn:
        conn.execute(V10_DDL["price_bars_daily"])
        conn.execute("INSERT INTO price_bars_daily (ticker, date, close) VALUES ('AAA', '2026-05-26', 1.0)")
    old_key, transform = rawstore._KEY_REBUILDS["price_bars_daily"]
    injected = []

    def racing(row, now):
        if not injected:
            with rawstore.connect() as other:                         # another writer lands in the gap
                other.execute("INSERT INTO price_bars_daily (ticker, date, close) VALUES ('BBB', '2026-05-26', 2.0)")
            injected.append(1)
        return transform(row, now)

    monkeypatch.setitem(rawstore._KEY_REBUILDS, "price_bars_daily", (old_key, racing))
    with rawstore.connect() as conn:
        try:
            rawstore._apply_key_rebuilds(conn)
        except Exception:  # noqa: BLE001 - refusing the swap is the safe outcome
            pass
    with rawstore.connect() as conn:
        tickers = sorted(r[0] for r in conn.execute("SELECT ticker FROM price_bars_daily").fetchall())
    assert tickers == ["AAA", "BBB"]                                  # no row lost, whichever way it ends
