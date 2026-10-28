"""ops schema 2 and the two watch lists (PR 4): the v1 -> v2 upgrade in place, the toggle and its history, the reads.

The v1 file is written here from the v1 statements as PR 1 shipped them, not from ``ops._DDL``, so the test keeps
describing a real old file after ``_DDL`` moves on.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from RevenueChaser.rcdata import ops, read

V1_WATCH_DDL = [
    *(f"CREATE TABLE watchlist_{k} (ticker TEXT PRIMARY KEY, added_at REAL NOT NULL, added_by TEXT, note TEXT)"
      for k in ("option", "stock")),
    *(f"CREATE TABLE watchlist_{k}_changes (ticker TEXT NOT NULL, action TEXT NOT NULL "
      f"CHECK (action IN ('add','remove')), at REAL NOT NULL, by TEXT)" for k in ("option", "stock")),
]


def _v1_file(path):
    """A v1 ops file with two option-list changes and one stock ticker; ``at`` is out of order on purpose."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE ops_schema (version INTEGER NOT NULL, applied_at REAL NOT NULL)")
    con.execute("INSERT INTO ops_schema VALUES (1, 1.0)")
    for ddl in V1_WATCH_DDL:
        con.execute(ddl)
    con.execute("CREATE TABLE build_requests (id INTEGER PRIMARY KEY, dataset TEXT NOT NULL, requested_at REAL "
                "NOT NULL, requested_by TEXT NOT NULL, params TEXT, status TEXT NOT NULL, started_at REAL, "
                "ended_at REAL, note TEXT)")
    con.execute("INSERT INTO build_requests (dataset, requested_at, requested_by, status) "
                "VALUES ('market_series', 1.0, 'cli', 'done')")
    con.execute("INSERT INTO watchlist_stock VALUES ('MSFT', 5.0, 'leaps', NULL)")
    con.execute("INSERT INTO watchlist_option_changes VALUES ('NVDA', 'add', 9.0, 'leaps')")
    con.execute("INSERT INTO watchlist_option_changes VALUES ('NVDA', 'remove', 3.0, 'settings')")
    con.commit()
    con.close()


def _cols(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def test_a_new_file_is_version_2_with_the_history_id_and_note():
    ops.ensure()
    with ops.connect() as con:
        assert [r[0] for r in con.execute("SELECT version FROM ops_schema")] == [2]
        for k in ops.WATCH_KINDS:
            assert _cols(con, f"watchlist_{k}_changes") == ["id", "ticker", "action", "at", "by", "note"]
            assert _cols(con, f"watchlist_{k}") == ["ticker", "added_at", "added_by", "note"]


def test_a_v1_file_is_upgraded_in_place_and_keeps_every_row_in_written_order():
    path = ops.ops_path()
    _v1_file(path)
    ops.ensure(path)
    ops.ensure(path)                                    # a second ensure changes nothing
    with ops.connect(path) as con:
        assert [r[0] for r in con.execute("SELECT version FROM ops_schema ORDER BY version")] == [1, 2]
        hist = [tuple(r) for r in con.execute("SELECT id, ticker, action, at, by, note FROM "
                                               "watchlist_option_changes ORDER BY id")]
        assert hist == [(1, "NVDA", "add", 9.0, "leaps", None), (2, "NVDA", "remove", 3.0, "settings", None)]
        assert _cols(con, "watchlist_stock_changes")[0] == "id"
        assert [tuple(r) for r in con.execute("SELECT * FROM watchlist_stock")] == [("MSFT", 5.0, "leaps", None)]
        assert con.execute("SELECT count(*) FROM build_requests").fetchone()[0] == 1
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert not {n for n in names if n.endswith("_v1")}
    assert "watchlist_option_changes_ticker" in names
    assert ops.watch_toggle("option", "NVDA", by="leaps") is True    # the upgraded file takes writes


def test_a_failed_upgrade_leaves_the_v1_file_as_it_was(monkeypatch):
    path = ops.ops_path()
    _v1_file(path)

    def boom(con):
        con.execute("ALTER TABLE watchlist_option_changes RENAME TO watchlist_option_changes_v1")
        raise RuntimeError("disk gone")

    monkeypatch.setitem(ops._UPGRADES, 2, boom)
    with pytest.raises(RuntimeError):
        ops.ensure(path)
    with ops.connect(path) as con:
        assert [r[0] for r in con.execute("SELECT version FROM ops_schema")] == [1]
        assert _cols(con, "watchlist_option_changes") == ["ticker", "action", "at", "by"]


def test_a_file_stamped_newer_than_2_is_refused_and_not_touched():
    path = ops.ops_path()
    ops.ensure(path)
    with ops.connect(path) as con:
        con.execute("INSERT INTO ops_schema VALUES (3, 0)")
    with pytest.raises(ops.OpsSchemaTooNew, match="ops schema 3"):
        ops.ensure(path)


def test_toggle_flips_and_writes_one_history_row_per_flip_with_the_board():
    assert ops.watch_toggle("option", " nvda ", by="leaps") is True
    assert ops.watch_toggle("option", "NVDA", by="sec_13f") is False
    assert ops.watch_toggle("option", "NVDA", by="settings") is True
    assert ops.watch_list("option") == ["NVDA"] and ops.watch_list("stock") == []
    with ops.connect() as con:
        hist = [tuple(r) for r in con.execute("SELECT ticker, action, by FROM watchlist_option_changes ORDER BY id")]
        row = dict(con.execute("SELECT * FROM watchlist_option").fetchone())
    assert hist == [("NVDA", "add", "leaps"), ("NVDA", "remove", "sec_13f"), ("NVDA", "add", "settings")]
    assert row["added_by"] == "settings" and row["added_at"] <= time.time()
    with pytest.raises(ValueError):
        ops.watch_toggle("bond", "NVDA", by="x")


def test_add_keeps_the_note_on_the_row_and_its_history():
    ops.watch_add("stock", "aapl", by="settings", note="earnings in May")
    with ops.connect() as con:
        assert con.execute("SELECT note FROM watchlist_stock_changes").fetchone()[0] == "earnings in May"
    assert ops.watch_rows("stock")[0]["note"] == "earnings in May"


def test_the_reads_never_create_the_ops_file():
    assert read.watchlists() == {"option": [], "stock": []}
    assert read.watched("stock") == set()
    assert not ops.ops_path().exists()


def test_read_watchlists_returns_both_lists_by_ticker_and_counts_its_reads():
    ops.watch_add("stock", "MSFT", by="leaps")
    ops.watch_add("stock", "AAPL", by="settings")
    ops.watch_add("option", "NVDA", by="sec_13f")
    got = read.watchlists()
    assert [r["ticker"] for r in got["stock"]] == ["AAPL", "MSFT"]
    assert got["option"][0] == {"ticker": "NVDA", "added_at": got["option"][0]["added_at"],
                                "added_by": "sec_13f", "note": None}
    assert read.watched("option") == {"NVDA"}
    ops.read_log.flush()
    with ops.connect() as con:
        n = dict(con.execute("SELECT table_name, reads FROM read_log").fetchall())
    assert n == {"watchlist_option": 2, "watchlist_stock": 1}


# ── The lossless rollback (review should-fix 1): stamp the file down, the v1 code runs on it ──────────────────────
# v1's ensure(), statement for statement: CREATE TABLE IF NOT EXISTS of every table (the watch-list history in its
# v1 shape), its two indexes, then the stamp check. Written out here so the test keeps describing PR 1's code.
def _v1_ensure(path):
    v1_ddl = {**{k: v for k, v in ops._DDL.items() if not k.endswith("_changes")},
              **{f"watchlist_{k}_changes": f"""CREATE TABLE IF NOT EXISTS watchlist_{k}_changes (
        ticker TEXT NOT NULL, action TEXT NOT NULL CHECK (action IN ('add','remove')), at REAL NOT NULL, by TEXT)"""
                 for k in ("option", "stock")}}
    con = sqlite3.connect(str(path), isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        for ddl in v1_ddl.values():
            con.execute(ddl)
        con.execute("CREATE INDEX IF NOT EXISTS build_requests_status ON build_requests (status, id)")
        con.execute("CREATE INDEX IF NOT EXISTS dataset_check_dataset ON dataset_check (dataset, id)")
        found = con.execute("SELECT max(version) FROM ops_schema").fetchone()[0]
        if found is not None and found > 1:
            raise ops.OpsSchemaTooNew(f"ops schema {found}")
        if found is None:
            con.execute("INSERT INTO ops_schema (version, applied_at) VALUES (1, 0)")
        con.execute("COMMIT")
        return {k: con.execute(f"SELECT count(*) FROM watchlist_{k}").fetchone()[0] for k in ("option", "stock")}
    finally:
        con.close()


def _stamp_down(path):
    with ops.connect(path) as con:
        con.execute("DELETE FROM ops_schema WHERE version = 2")


def test_v1_code_refuses_the_v2_file_until_it_is_stamped_down_and_then_runs_on_it():
    path = ops.ops_path()
    ops.watch_add("stock", "MSFT", by="leaps", note="pinned")
    ops.count_calls("uw", 7)
    with pytest.raises(ops.OpsSchemaTooNew):
        _v1_ensure(path)                                  # revert alone is not enough
    _stamp_down(path)
    assert _v1_ensure(path) == {"option": 0, "stock": 1}  # v1's only watch-list read: count(*)
    assert ops.calls_today("uw") == 7                     # the day-cap counter survives the rollback


def test_redeploying_v2_after_a_stamp_down_keeps_the_history_its_ids_and_notes():
    path = ops.ops_path()
    ops.watch_add("stock", "MSFT", by="leaps", note="pinned")
    ops.watch_remove("stock", "MSFT", by="settings")
    _stamp_down(path)
    _v1_ensure(path)
    ops._ENSURED.discard(str(path))
    ops.ensure(path)                                      # the next deploy finds stamp 1 and upgrades again
    with ops.connect(path) as con:
        hist = [tuple(r) for r in con.execute("SELECT id, ticker, action, by, note FROM watchlist_stock_changes "
                                               "ORDER BY id")]
        assert [r[0] for r in con.execute("SELECT version FROM ops_schema ORDER BY version")] == [1, 2]
    assert hist == [(1, "MSFT", "add", "leaps", "pinned"), (2, "MSFT", "remove", "settings", None)]


def test_concurrent_whole_list_replaces_raise_nothing_and_the_history_replays_to_the_list():
    """``rawstore.save_watchlist`` (``watch_replace``) from several threads at once: each is one BEGIN IMMEDIATE."""
    import threading
    errors: list[Exception] = []

    def add(ticker):
        try:
            for _ in range(10):
                ops.watch_replace("stock", set(ops.watch_list("stock")) | {ticker}, by="t")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(t,)) for t in ("AAPL", "MSFT", "NVDA")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors[:2]
    with ops.connect() as con:
        state = {}
        for t, action in con.execute("SELECT ticker, action FROM watchlist_stock_changes ORDER BY id"):
            state[t] = action == "add"
    assert {t for t, on in state.items() if on} == set(ops.watch_list("stock"))
