"""Change history captured by the store (schema 11, history redesign C).

A live table that declares ``rawstore.CHANGES`` keeps its key, its writers and its readers; the store appends a rule-A
row to ``<table>_changes`` in the SAME transaction as every write to it, and the first store call of a process seeds and
syncs. The invariant every test below leans on: after any write, the keys whose latest change row is 'present' are
exactly the keys of the live table. Temp stores only.
"""
from __future__ import annotations

import random

import pytest

from RevenueChaser.core import rawstore


def _latest(name: str) -> dict:
    """Each key's latest change status."""
    key = rawstore.MANIFEST[name]["key"]
    rows = rawstore.query(f"SELECT {', '.join(key)}, status FROM {name}_changes QUALIFY row_number() OVER "
                          f"(PARTITION BY {', '.join(key)} ORDER BY run_at DESC) = 1")
    return {tuple(r[k] for k in key): r["status"] for r in rows}


def _agrees(name: str) -> bool:
    key = rawstore.MANIFEST[name]["key"]
    live = {tuple(r[k] for k in key) for r in rawstore.query(
        f"SELECT DISTINCT {', '.join(key)} FROM {name} WHERE {' AND '.join(k + ' IS NOT NULL' for k in key)}")}
    return live == {k for k, s in _latest(name).items() if s == "present"}


def _n_changes(name: str) -> int:
    return rawstore.query(f"SELECT count(*) AS n FROM {name}_changes")[0]["n"]


def _new_process() -> None:
    """What a fresh process sees: nothing memoised, the first call applies the MANIFEST again."""
    rawstore._TABLE_READY.clear()
    rawstore._ENSURED.clear()
    rawstore._HISTORY_READY.clear()


@pytest.fixture
def store(rawstore_tmp_db):
    return rawstore_tmp_db


# ── the four write calls and the capture in their transaction ───────────────────────────────────────────────────
def test_every_tracked_table_has_a_change_table_in_the_same_file():
    assert set(rawstore.CHANGES) == {"equity_universe", "security_master", "identity_source_cache",
                                     "index_constituents", "watchlist", "board_settings", "catalyst_events",
                                     "orphan_designations", "etf_holdings"}
    for name in rawstore.CHANGES:
        assert rawstore._db_of(f"{name}_changes") == rawstore._db_of(name) == "raw"
        assert rawstore.MANIFEST[f"{name}_changes"]["history"] is True


def test_write_rows_replace_and_delete_are_recorded(store):
    rawstore.write_rows("watchlist", [{"ticker": "A"}, {"ticker": "B"}])
    assert _latest("watchlist") == {("A",): "present", ("B",): "present"}
    rawstore.replace_all("watchlist", [{"ticker": "B"}, {"ticker": "C"}])
    assert _latest("watchlist") == {("A",): "absent", ("B",): "present", ("C",): "present"}
    rawstore.delete("watchlist", "C")
    assert _latest("watchlist")[("C",)] == "absent"
    rawstore.clear("watchlist")
    assert set(_latest("watchlist").values()) == {"absent"}
    assert _agrees("watchlist")


def test_a_write_that_changes_nothing_appends_nothing(store):
    rawstore.write_rows("watchlist", [{"ticker": "A"}])
    before = _n_changes("watchlist")
    rawstore.write_rows("watchlist", [{"ticker": "A"}])
    rawstore.replace_all("watchlist", [{"ticker": "A"}])
    assert _n_changes("watchlist") == before


def test_update_columns_records_tracked_columns_only(store):
    rawstore.write_rows("equity_universe", [{"ticker": "AAA", "name": "Aaa", "sector": "Tech", "market_cap": 1.0}])
    n = _n_changes("equity_universe")
    rawstore.update_columns("equity_universe", "ticker", [{"ticker": "AAA", "market_cap": 2.0}], ["market_cap"])
    assert _n_changes("equity_universe") == n                       # market_cap is not tracked (moves every pull)
    rawstore.update_columns("equity_universe", "ticker", [{"ticker": "AAA", "sector": "Health"}], ["sector"])
    assert _n_changes("equity_universe") == n + 1
    assert rawstore.changes_as_of("equity_universe", 1e12) == [
        {"ticker": "AAA", "name": "Aaa", "sector": "Health", "industry": None, "industry_group": None,
         "sub_industry": None, "next_earnings_date": None}]


def test_the_identity_payload_is_carried_not_tracked(store):
    row = {"source": "uw_sector", "ticker": "AAA", "status": "ok", "sector": "Tech", "payload": {"market_cap": 1}}
    rawstore.write_rows("identity_source_cache", [row])
    n = _n_changes("identity_source_cache")
    rawstore.write_rows("identity_source_cache", [{**row, "payload": {"market_cap": 2}}])
    assert _n_changes("identity_source_cache") == n                 # per-pull values never log a change
    rawstore.write_rows("identity_source_cache", [{**row, "sector": "Health"}])
    assert _n_changes("identity_source_cache") == n + 1
    got = rawstore.query("SELECT row_status, source FROM identity_source_cache_changes ORDER BY run_at DESC LIMIT 1")
    assert got == [{"row_status": "ok", "source": "uw_sector"}]     # live 'status' is renamed; the provider is the key


def test_a_capture_error_rolls_back_its_write(store, monkeypatch):
    rawstore.write_rows("watchlist", [{"ticker": "A"}])

    def boom(conn, name, touched=None):
        raise RuntimeError("capture failed")

    monkeypatch.setattr(rawstore, "_capture_changes", boom)
    with pytest.raises(RuntimeError, match="capture failed"):
        rawstore.replace_all("watchlist", [{"ticker": "Z"}])
    assert [r["ticker"] for r in rawstore.query("SELECT ticker FROM watchlist")] == ["A"]


def test_the_invariant_holds_over_random_write_sequences(store):
    rng = random.Random(7)
    pool = [f"T{i}" for i in range(8)]
    for _ in range(40):
        op = rng.choice(["write", "replace", "delete", "clear"])
        picks = rng.sample(pool, rng.randint(1, 4))
        if op == "write":
            rawstore.write_rows("watchlist", [{"ticker": t} for t in picks])
        elif op == "replace":
            rawstore.replace_all("watchlist", [{"ticker": t} for t in picks])
        elif op == "delete":
            rawstore.delete("watchlist", picks[0])
        else:
            rawstore.clear("watchlist")
        assert _agrees("watchlist")


def test_the_writer_is_stamped_on_the_change(store):
    rawstore.set_change_writer(lambda: ("job-7", "module_x"))
    try:
        rawstore.write_rows("equity_universe", [{"ticker": "AAA", "name": "Aaa"}])
        rawstore.write_rows("board_settings", [{"board": "b", "key": "k", "value": 1.0}])
    finally:
        rawstore.set_change_writer(None)
    assert rawstore.query("SELECT run_id, source FROM equity_universe_changes") == [{"run_id": "job-7", "source": "module_x"}]
    assert rawstore.query("SELECT run_id, source FROM board_settings_changes") == [{"run_id": "job-7", "source": "manual"}]


def test_changes_as_of_reads_the_rows_a_table_held_then(store):
    rawstore.write_rows("equity_universe", [{"ticker": "AAA", "name": "Aaa"}])
    mid = rawstore.query("SELECT max(run_at) AS t FROM equity_universe_changes")[0]["t"]
    rawstore.replace_all("equity_universe", [{"ticker": "BBB", "name": "Bbb"}])
    assert [r["ticker"] for r in rawstore.changes_as_of("equity_universe", mid)] == ["AAA"]
    assert [r["ticker"] for r in rawstore.changes_as_of("equity_universe", 1e12)] == ["BBB"]


# ── index_constituents has no PRIMARY KEY: today's repeats and NULLs stay accepted (report F10) ──────────────────
def test_index_constituents_keeps_accepting_repeats_and_null_keys(store):
    rows = [{"slug": "sp500", "ticker": "AAA"}, {"slug": "sp500", "ticker": "AAA"},
            {"slug": "sp500", "ticker": None}, {"slug": "sp500", "ticker": "BBB"}]
    rawstore.replace_all("index_constituents", rows)
    assert rawstore.count("index_constituents") == 4                # the live table is exactly as before
    assert _latest("index_constituents") == {("sp500", "AAA"): "present", ("sp500", "BBB"): "present"}
    rawstore.replace_all("index_constituents", [{"slug": "sp500", "ticker": "BBB"}, {"slug": "sp500", "ticker": "BBB"}])
    assert _latest("index_constituents") == {("sp500", "AAA"): "absent", ("sp500", "BBB"): "present"}


# ── the first call seeds and syncs; a failing table never holds the others back (F8, F10) ────────────────────────
def test_a_write_outside_the_api_is_recorded_by_the_next_first_call(store):
    rawstore.write_rows("watchlist", [{"ticker": "A"}])
    with rawstore.connect() as conn:
        conn.execute("INSERT INTO watchlist VALUES ('BYPASS')")
    assert ("BYPASS",) not in _latest("watchlist")
    _new_process()
    rawstore.ensure_all_tables()
    assert _latest("watchlist")[("BYPASS",)] == "present"
    got = rawstore.query("SELECT run_id, source FROM watchlist_changes WHERE ticker = 'BYPASS'")
    assert got == [{"run_id": "store-sync", "source": "manual"}]
    assert rawstore.sync_changes() == {t: 0 for t in rawstore.CHANGES}   # a second sync has nothing to add


def test_the_first_call_after_the_bump_seeds_every_change_table_from_its_live_table(store):
    with rawstore.connect() as conn:
        conn.execute("DROP TABLE watchlist_changes")
        conn.execute("DROP TABLE equity_universe_changes")
        conn.execute("INSERT INTO watchlist VALUES ('A'), ('B')")
        conn.execute("INSERT INTO equity_universe (ticker, name) VALUES ('AAA', 'Aaa')")
    _new_process()
    rawstore.ensure_all_tables()
    assert _latest("watchlist") == {("A",): "present", ("B",): "present"}
    assert _latest("equity_universe") == {("AAA",): "present"}
    assert {r["run_id"] for r in rawstore.query("SELECT run_id FROM watchlist_changes")} == {"store-sync"}


def test_one_table_that_cannot_sync_leaves_the_others_synced_and_the_call_raises(store, monkeypatch):
    with rawstore.connect() as conn:
        conn.execute("INSERT INTO watchlist VALUES ('BYPASS')")
    real = rawstore._capture_changes

    def broken(conn, name, touched=None):
        if name == "board_settings":
            raise RuntimeError("simulated")
        return real(conn, name, touched)

    monkeypatch.setattr(rawstore, "_capture_changes", broken)
    _new_process()
    edgar = rawstore.edgar_db_path()
    edgar.unlink(missing_ok=True)
    with pytest.raises(rawstore.RawStoreError, match="board_settings"):
        rawstore.ensure_all_tables()
    assert _latest("watchlist")[("BYPASS",)] == "present"            # synced, in its own transaction
    assert edgar.exists()                                            # the other files were still made


def test_a_store_holding_repeated_keys_still_syncs(store):
    with rawstore.connect() as conn:
        conn.execute("INSERT INTO index_constituents VALUES ('r1000','X'), ('r1000','X'), ('r1000', NULL)")
    _new_process()
    rawstore.ensure_all_tables()                                     # must not raise
    assert _latest("index_constituents") == {("r1000", "X"): "present"}


def test_a_legacy_table_missing_a_tracked_column_waits_for_the_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "legacy.duckdb")
    _new_process()
    rawstore.execute("CREATE TABLE equity_universe (ticker TEXT PRIMARY KEY, name TEXT)")
    rawstore.execute("INSERT INTO equity_universe VALUES ('OLD', 'Old Co')")        # the capture finds columns missing
    rawstore.ensure_all_tables()
    assert _latest("equity_universe") == {("OLD",): "present"}


def test_a_manifest_without_a_tracked_table_is_skipped(store, monkeypatch):
    spec = rawstore.MANIFEST.pop("catalyst_events")
    try:
        rawstore.sync_changes()                                      # must not raise
    finally:
        rawstore.MANIFEST["catalyst_events"] = spec


# ── raw SQL cannot slip past the capture (refutation pass, report F11) ───────────────────────────────────────────
def test_query_with_a_write_is_captured(store):
    rawstore.write_rows("watchlist", [{"ticker": "A"}, {"ticker": "B"}])
    got = rawstore.query("DELETE FROM watchlist WHERE ticker = 'A' RETURNING ticker")
    assert got == [{"ticker": "A"}] and _latest("watchlist")[("A",)] == "absent" and _agrees("watchlist")
    rawstore.query("INSERT INTO watchlist (ticker) VALUES ('Z') RETURNING ticker", read_only=False)
    assert _latest("watchlist")[("Z",)] == "present" and _agrees("watchlist")
    assert rawstore.sync_changes() == {t: 0 for t in rawstore.CHANGES}   # nothing left for the sync to heal


def test_execute_and_a_transaction_execute_are_captured(store):
    rawstore.write_rows("watchlist", [{"ticker": "A"}, {"ticker": "B"}, {"ticker": "C"}])
    rawstore.execute("DELETE FROM watchlist WHERE ticker = 'A'")
    with rawstore.transaction() as tx:
        tx.execute("DELETE FROM watchlist WHERE ticker = 'B'")
    assert _latest("watchlist")[("A",)] == _latest("watchlist")[("B",)] == "absent" and _agrees("watchlist")


@pytest.mark.parametrize("sql", [
    "PREPARE p AS DELETE FROM watchlist WHERE ticker = 'A'",
    "DELETE FROM watchlist WHERE ticker = 'A'; COMMIT",
])
def test_raw_sql_the_capture_cannot_follow_is_refused(store, sql):
    rawstore.write_rows("watchlist", [{"ticker": "A"}, {"ticker": "B"}])
    with pytest.raises(rawstore.RawStoreError, match="watchlist"):
        with rawstore.transaction() as tx:
            tx.execute(sql)
    assert rawstore.count("watchlist") == 2 and _agrees("watchlist")


def test_a_write_naming_a_tracked_table_on_another_file_is_refused(store):
    rawstore.write_rows("watchlist", [{"ticker": "A"}, {"ticker": "B"}])
    raw = str(rawstore.DB_PATH)
    with pytest.raises(rawstore.RawStoreError):
        rawstore.execute(f"ATTACH '{raw}' AS r2; DELETE FROM r2.watchlist WHERE ticker='A'", db="edgar")
    with pytest.raises(rawstore.RawStoreError):
        rawstore.execute("DELETE FROM watchlist WHERE ticker='A'", db="edgar")
    assert rawstore.count("watchlist") == 2


def test_a_temp_table_of_the_same_name_never_stands_in_for_the_live_one(store):
    rawstore.write_rows("watchlist", [{"ticker": "A"}, {"ticker": "B"}])
    with rawstore.transaction() as tx:
        tx.execute("CREATE TEMP TABLE watchlist AS SELECT * FROM main.watchlist")
        tx.execute("DELETE FROM raw.main.watchlist WHERE ticker = 'A'")      # the file's own table
    assert _latest("watchlist")[("A",)] == "absent" and _agrees("watchlist")


def test_a_change_table_created_in_work_that_rolls_back_does_not_break_later_writes(store):
    with rawstore.connect() as conn:
        conn.execute("DROP TABLE watchlist_changes")
    rawstore._TABLE_READY.clear()
    with pytest.raises(RuntimeError):
        with rawstore.transaction() as tx:
            tx.write_rows("watchlist", [{"ticker": "A"}])
            raise RuntimeError("caller fails after the write")
    rawstore.delete("watchlist", "A")
    rawstore.clear("watchlist")
    rawstore.execute("DELETE FROM watchlist")
    rawstore.write_rows("watchlist", [{"ticker": "Q"}])
    assert _agrees("watchlist")


# ── a transaction handle writes the file it was opened on, and no other (F6) ─────────────────────────────────────
def test_a_transaction_refuses_a_table_of_another_file(store):
    with pytest.raises(rawstore.RawStoreError, match="one transaction, one file"):
        with rawstore.transaction() as tx:                           # opened on raw
            tx.write_rows("edgar_financials", [{"ticker": "AAA"}])  # lives in edgar
    with pytest.raises(rawstore.RawStoreError, match="one transaction, one file"):
        rawstore.atomic(lambda tx: tx.write_rows("watchlist", [{"ticker": "A"}]), db="history")
