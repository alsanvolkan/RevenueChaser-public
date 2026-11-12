"""ops schema 3 and the watch lists (PR 4, the named lists #774): the upgrades in place, the toggle and its
history, the reads.

The v1 and v2 files are written here from the statements as those versions shipped them, not from ``ops._DDL``, so
the tests keep describing a real old file after ``_DDL`` moves on.
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


V2_WATCH_DDL = [
    *(f"CREATE TABLE watchlist_{k} (ticker TEXT PRIMARY KEY, added_at REAL NOT NULL, added_by TEXT, note TEXT)"
      for k in ("option", "stock")),
    *(f"CREATE TABLE watchlist_{k}_changes (id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, action TEXT NOT NULL "
      f"CHECK (action IN ('add','remove')), at REAL NOT NULL, by TEXT, note TEXT)" for k in ("option", "stock")),
]


def _v2_file(path):
    """A v2 file (the shape PR 4 shipped): two names on the stock list, one history row each."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE ops_schema (version INTEGER NOT NULL, applied_at REAL NOT NULL)")
    con.execute("INSERT INTO ops_schema VALUES (2, 1.0)")
    for ddl in V2_WATCH_DDL:
        con.execute(ddl)
    con.execute("INSERT INTO watchlist_stock VALUES ('MSFT', 5.0, 'leaps', 'pinned')")
    con.execute("INSERT INTO watchlist_stock VALUES ('AAPL', 6.0, 'settings', NULL)")
    con.execute("INSERT INTO watchlist_option VALUES ('NVDA', 7.0, 'sec_13f', NULL)")
    con.execute("INSERT INTO watchlist_stock_changes VALUES (1, 'MSFT', 'add', 5.0, 'leaps', 'pinned')")
    con.execute("INSERT INTO watchlist_option_changes VALUES (1, 'NVDA', 'add', 7.0, 'sec_13f', NULL)")
    con.commit()
    con.close()


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


def test_a_new_file_is_version_3_with_the_list_table_and_list_id_everywhere(sqlite_ops):
    ops.ensure()
    with ops.connect() as con:
        assert [r[0] for r in con.execute("SELECT version FROM ops_schema")] == [3]
        assert _cols(con, "watch_list") == ["id", "kind", "name", "created_at", "created_by"]
        for k in ops.WATCH_KINDS:
            assert _cols(con, f"watchlist_{k}_changes") == ["id", "list_id", "ticker", "action", "at", "by", "note"]
            assert _cols(con, f"watchlist_{k}") == ["list_id", "ticker", "added_at", "added_by", "note"]


def test_a_v1_file_is_upgraded_in_place_and_keeps_every_row_in_written_order(sqlite_ops):
    path = ops.ops_path()
    _v1_file(path)
    ops.ensure(path)
    ops.ensure(path)                                    # a second ensure changes nothing
    with ops.connect(path) as con:
        assert [r[0] for r in con.execute("SELECT version FROM ops_schema ORDER BY version")] == [1, 3]
        hist = [tuple(r) for r in con.execute("SELECT id, ticker, action, at, by, note FROM "
                                               "watchlist_option_changes ORDER BY id")]
        assert hist == [(1, "NVDA", "add", 9.0, "leaps", None), (2, "NVDA", "remove", 3.0, "settings", None)]
        assert _cols(con, "watchlist_stock_changes")[:2] == ["id", "list_id"]
        stock = con.execute("SELECT id FROM watch_list WHERE kind = 'stock'").fetchone()[0]
        assert [tuple(r) for r in con.execute("SELECT * FROM watchlist_stock")] == [(stock, "MSFT", 5.0, "leaps",
                                                                                     None)]
        assert con.execute("SELECT count(*) FROM build_requests").fetchone()[0] == 1
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert not {n for n in names if n.endswith("_v1") or n.endswith("_v2")}
    assert "watchlist_option_changes_ticker" in names
    assert ops.watch_toggle("option", "NVDA", by="leaps") is True    # the upgraded file takes writes


def test_a_failed_upgrade_leaves_the_v1_file_as_it_was(sqlite_ops, monkeypatch):
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


def test_a_file_stamped_newer_than_this_checkout_is_refused_and_not_touched(sqlite_ops):
    path = ops.ops_path()
    ops.ensure(path)
    with ops.connect(path) as con:
        con.execute("INSERT INTO ops_schema VALUES (4, 0)")
    with pytest.raises(ops.OpsSchemaTooNew, match="ops schema 4"):
        ops.ensure(path)


# ── v2 -> v3 (#774): the named lists ──────────────────────────────────────────────────────────────────────────────

def test_a_v2_file_is_upgraded_in_place_and_every_ticker_lands_on_its_groups_main_list(sqlite_ops):
    path = ops.ops_path()
    _v2_file(path)
    ops.ensure(path)
    ops.ensure(path)                                     # a second ensure changes nothing
    lists = {(r["kind"], r["name"]): r["id"] for r in ops.watch_lists()}
    assert set(lists) == {("option", ops.DEFAULT_WATCH_LIST), ("stock", ops.DEFAULT_WATCH_LIST)}
    with ops.connect(path) as con:
        assert [r[0] for r in con.execute("SELECT version FROM ops_schema ORDER BY version")] == [2, 3]
        assert [tuple(r) for r in con.execute("SELECT list_id, ticker, added_at, added_by, note "
                                              "FROM watchlist_stock ORDER BY ticker")] == [
            (lists[("stock", ops.DEFAULT_WATCH_LIST)], "AAPL", 6.0, "settings", None),
            (lists[("stock", ops.DEFAULT_WATCH_LIST)], "MSFT", 5.0, "leaps", "pinned")]
        assert [tuple(r) for r in con.execute("SELECT id, list_id, ticker, note FROM watchlist_stock_changes")] == [
            (1, lists[("stock", ops.DEFAULT_WATCH_LIST)], "MSFT", "pinned")]
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert not {n for n in names if n.endswith("_v2")}
    assert ops.watch_list("stock") == ["AAPL", "MSFT"] and ops.watch_list("option") == ["NVDA"]
    assert ops.watch_toggle("option", "NVDA", by="leaps") is False    # the upgraded file takes writes


def test_a_failed_v3_upgrade_leaves_the_v2_file_as_it_was(sqlite_ops, monkeypatch):
    path = ops.ops_path()
    _v2_file(path)

    def boom(con):
        con.execute(ops._DDL["watch_list"])
        con.execute("ALTER TABLE watchlist_stock RENAME TO watchlist_stock_v2")
        raise RuntimeError("disk gone")

    monkeypatch.setitem(ops._UPGRADES, 3, boom)
    with pytest.raises(RuntimeError):
        ops.ensure(path)
    with ops.connect(path) as con:
        assert [r[0] for r in con.execute("SELECT version FROM ops_schema")] == [2]
        assert _cols(con, "watchlist_stock") == ["ticker", "added_at", "added_by", "note"]
        assert not {r[0] for r in con.execute("SELECT name FROM sqlite_master") if r[0] == "watch_list"}


def test_the_v3_upgrade_is_not_redone_for_a_file_stamped_back_down(sqlite_ops):
    """The rollback stamp alone: the next deploy must not move the rows onto a second list (as v2 did for ``id``)."""
    path = ops.ops_path()
    _v2_file(path)
    ops.ensure(path)
    before = [dict(r) for r in ops.rows("watchlist_stock")]
    with ops.connect(path) as con:
        con.execute("DELETE FROM ops_schema WHERE version = 3")
    ops._ENSURED.discard(str(path))
    ops.ensure(path)
    assert [dict(r) for r in ops.rows("watchlist_stock")] == before
    assert len(ops.watch_lists("stock")) == 1


def test_a_second_list_holds_the_same_ticker_and_a_job_reads_the_union():
    """avo, 12 Nov 2026: a job reads every list of a group, so ``watch_list`` is the union, each ticker once."""
    core = ops.watch_list_new("stock", "Core", by="settings")
    bio = ops.watch_list_new("stock", " Biotech ", by="settings")
    assert core["created"] is True and bio["name"] == "Biotech"
    assert ops.watch_list_new("stock", "core", by="settings")["created"] is True       # names are case-sensitive
    assert ops.watch_list_new("stock", "Core", by="settings") == {**core, "created": False}
    assert ops.watch_add("stock", "MSFT", by="settings", list_id=core["id"]) is True
    assert ops.watch_add("stock", "MSFT", by="settings", list_id=bio["id"]) is True
    assert ops.watch_add("stock", "CRSP", by="settings", list_id=bio["id"]) is True
    assert ops.watch_tickers("stock", core["id"]) == ["MSFT"]
    assert ops.watch_tickers("stock", bio["id"]) == ["CRSP", "MSFT"]
    assert ops.watch_list("stock") == ["CRSP", "MSFT"]                                  # the union, once each
    assert ops.watch_count("stock") == 2
    # Taking it off one list leaves it on the other, so the union still holds it.
    assert ops.watch_toggle("stock", "MSFT", by="settings", list_id=bio["id"]) is False
    assert ops.watch_list("stock") == ["CRSP", "MSFT"]
    assert ops.watch_tickers("stock", bio["id"]) == ["CRSP"]


def test_a_list_of_the_other_group_or_no_list_at_all_is_refused_and_writes_nothing():
    opt = ops.watch_list_new("option", "Spreads", by="settings")
    with pytest.raises(ops.UnknownWatchList):
        ops.watch_add("stock", "MSFT", by="settings", list_id=opt["id"])
    with pytest.raises(ops.UnknownWatchList):
        ops.watch_toggle("stock", "MSFT", by="settings", list_id=9999)
    assert ops.watch_list("stock") == [] and ops.rows("watchlist_stock_changes") == []
    for bad in ("", "   ", "x" * (ops.WATCH_NAME_MAX + 1)):
        with pytest.raises(ValueError):
            ops.watch_list_new("stock", bad, by="settings")


def test_a_write_with_no_list_lands_on_the_groups_oldest_list_whatever_it_is_called():
    """``_default_list_id``: the oldest list, not one named ``Main``, so a renamed single list keeps the clicks."""
    first = ops.watch_list_new("stock", "Core", by="settings")
    ops.watch_list_new("stock", ops.DEFAULT_WATCH_LIST, by="settings")
    assert ops.watch_add("stock", "MSFT", by="leaps") is True
    assert ops.watch_tickers("stock", first["id"]) == ["MSFT"]


def test_watch_rows_names_every_list_a_ticker_is_on_and_keeps_one_row_per_ticker():
    core = ops.watch_list_new("stock", "Core", by="settings")
    bio = ops.watch_list_new("stock", "Biotech", by="settings")
    ops.watch_add("stock", "MSFT", by="settings", list_id=core["id"], note="first")
    ops.watch_add("stock", "MSFT", by="leaps", list_id=bio["id"])
    [row] = ops.watch_rows("stock")
    assert row["ticker"] == "MSFT" and row["lists"] == ["Biotech", "Core"]
    assert (row["added_by"], row["note"], row["list_id"]) == ("settings", "first", core["id"])   # the oldest add
    assert [r["lists"] for r in ops.watch_rows("stock", bio["id"])] == [["Biotech"]]


def test_toggle_flips_and_writes_one_history_row_per_flip_with_the_board():
    assert ops.watch_toggle("option", " nvda ", by="leaps") is True
    assert ops.watch_toggle("option", "NVDA", by="sec_13f") is False
    assert ops.watch_toggle("option", "NVDA", by="settings") is True
    assert ops.watch_list("option") == ["NVDA"] and ops.watch_list("stock") == []
    hist = [(r["ticker"], r["action"], r["by"]) for r in ops.rows("watchlist_option_changes")]
    [row] = ops.rows("watchlist_option")
    assert hist == [("NVDA", "add", "leaps"), ("NVDA", "remove", "sec_13f"), ("NVDA", "add", "settings")]
    assert row["added_by"] == "settings" and row["added_at"] <= time.time()
    with pytest.raises(ValueError):
        ops.watch_toggle("bond", "NVDA", by="x")


def test_add_keeps_the_note_on_the_row_and_its_history():
    ops.watch_add("stock", "aapl", by="settings", note="earnings in May")
    assert [r["note"] for r in ops.rows("watchlist_stock_changes")] == ["earnings in May"]
    assert ops.watch_rows("stock")[0]["note"] == "earnings in May"


def test_the_reads_never_create_the_ops_file():
    assert read.watchlists() == {"option": [], "stock": []}
    assert read.watch_groups() == {"option": [], "stock": []}
    assert read.watched("stock") == set()
    assert ops.watch_lists() == [] and ops.watch_list("stock") == []
    assert not ops.ops_path().exists()


def test_read_watch_groups_is_each_groups_lists_with_their_tickers():
    """What the dropdown draws (#774): the named lists of each group, oldest first, each with its own tickers."""
    core = ops.watch_list_new("stock", "Core", by="settings")
    bio = ops.watch_list_new("stock", "Biotech", by="settings")
    ops.watch_add("stock", "MSFT", by="settings", list_id=core["id"])
    ops.watch_add("stock", "CRSP", by="leaps", list_id=bio["id"])
    ops.watch_add("option", "NVDA", by="sec_13f")
    got = read.watch_groups()
    assert [(l["name"], l["tickers"]) for l in got["stock"]] == [("Core", ["MSFT"]), ("Biotech", ["CRSP"])]
    assert [(l["name"], l["tickers"]) for l in got["option"]] == [(ops.DEFAULT_WATCH_LIST, ["NVDA"])]
    assert got["stock"][0]["id"] == core["id"] and got["stock"][1]["id"] == bio["id"]


def test_read_watchlists_returns_both_lists_by_ticker_and_counts_its_reads():
    ops.watch_add("stock", "MSFT", by="leaps")
    ops.watch_add("stock", "AAPL", by="settings")
    ops.watch_add("option", "NVDA", by="sec_13f")
    got = read.watchlists()
    assert [r["ticker"] for r in got["stock"]] == ["AAPL", "MSFT"]
    assert got["option"][0] == {"ticker": "NVDA", "added_at": got["option"][0]["added_at"],
                                "added_by": "sec_13f", "note": None, "list_id": got["option"][0]["list_id"],
                                "lists": [ops.DEFAULT_WATCH_LIST]}
    assert read.watched("option") == {"NVDA"}
    ops.read_log.flush()
    n = {r["table_name"]: r["reads"] for r in ops.rows("read_log")}
    assert n == {"watchlist_option": 2, "watchlist_stock": 1}


# ── Going back a version (review should-fix 1) ────────────────────────────────────────────────────────────────────
# v1's and v2's ensure(), statement for statement: CREATE TABLE IF NOT EXISTS of every table (the watch-list tables
# in that version's shape), its indexes, then the stamp check. Written out here so the tests keep describing the
# code those versions shipped. ``CREATE TABLE IF NOT EXISTS`` leaves a newer table alone, which is the point: the
# old code then reads the new tables, and a write of its own shape is what fails.
def _old_ensure(path, version: int, watch_ddl: list[str]):
    ddl = {**{k: v for k, v in ops._DDL.items() if not k.startswith("watch")},
           **{f"watch_{i}": stmt.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
              for i, stmt in enumerate(watch_ddl)}}
    con = sqlite3.connect(str(path), isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        for stmt in ddl.values():
            con.execute(stmt)
        con.execute("CREATE INDEX IF NOT EXISTS build_requests_status ON build_requests (status, id)")
        con.execute("CREATE INDEX IF NOT EXISTS dataset_check_dataset ON dataset_check (dataset, id)")
        found = con.execute("SELECT max(version) FROM ops_schema").fetchone()[0]
        if found is not None and found > version:
            raise ops.OpsSchemaTooNew(f"ops schema {found}")
        if found is None:
            con.execute("INSERT INTO ops_schema (version, applied_at) VALUES (?, 0)", (version,))
        con.execute("COMMIT")
        return {k: con.execute(f"SELECT count(*) FROM watchlist_{k}").fetchone()[0] for k in ("option", "stock")}
    finally:
        con.close()


def _v1_ensure(path):
    return _old_ensure(path, 1, V1_WATCH_DDL)


def _v2_ensure(path):
    return _old_ensure(path, 2, V2_WATCH_DDL)


def _stamp_down(path, version: int):
    with ops.connect(path) as con:
        con.execute("DELETE FROM ops_schema WHERE version >= ?", (version,))


def test_older_code_refuses_the_v3_file_until_it_is_stamped_down_and_then_reads_but_cannot_write(sqlite_ops):
    """Going back from 3 is not the stamp alone (``docs/data_architecture.md``): v1 and v2 wrote a membership row
    of four columns, so the way back is restoring the ops tables from the backup taken before the upgrade. The
    counters that do not change shape (the day's calls) survive either way."""
    path = ops.ops_path()
    ops.watch_add("stock", "MSFT", by="leaps", note="pinned")
    ops.count_calls("uw", 7)
    for older in (_v1_ensure, _v2_ensure):
        with pytest.raises(ops.OpsSchemaTooNew):
            older(path)                                   # revert alone is not enough
    _stamp_down(path, 3)
    assert _v2_ensure(path) == {"option": 0, "stock": 1}  # the old read still answers: count(*)
    with ops.connect(path) as con, pytest.raises(sqlite3.Error):
        con.execute("INSERT INTO watchlist_stock VALUES ('AAPL', 1.0, 'leaps', NULL)")   # v2's write, four columns
    assert ops.calls_today("uw") == 7                     # the day-cap counter survives the rollback


def test_redeploying_v3_after_a_stamp_down_keeps_the_lists_the_history_its_ids_and_notes(sqlite_ops):
    path = ops.ops_path()
    core = ops.watch_list_new("stock", "Core", by="settings")
    ops.watch_add("stock", "MSFT", by="leaps", note="pinned", list_id=core["id"])
    ops.watch_remove("stock", "MSFT", by="settings", list_id=core["id"])
    _stamp_down(path, 3)
    _v2_ensure(path)
    ops._ENSURED.discard(str(path))
    ops.ensure(path)                                      # the next deploy finds stamp 2 and upgrades again
    with ops.connect(path) as con:
        hist = [tuple(r) for r in con.execute("SELECT id, list_id, ticker, action, by, note FROM "
                                              "watchlist_stock_changes ORDER BY id")]
        assert [r[0] for r in con.execute("SELECT version FROM ops_schema ORDER BY version")] == [2, 3]
    assert hist == [(1, core["id"], "MSFT", "add", "leaps", "pinned"),
                    (2, core["id"], "MSFT", "remove", "settings", None)]
    assert [r["name"] for r in ops.watch_lists("stock")] == ["Core"]


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
    # #261: once seen failing here with the message cut off; #522 since queues a process's writers on one lock, so a
    # failure now names its type and text, and a writer still running at the deadline is its own failure.
    assert not [t for t in threads if t.is_alive()], "a writer was still running after 60 s"
    assert not errors, [f"{type(e).__name__}: {e}" for e in errors[:3]]
    state = {}
    for r in ops.rows("watchlist_stock_changes"):
        state[r["ticker"]] = r["action"] == "add"
    assert {t for t, on in state.items() if on} == set(ops.watch_list("stock"))
