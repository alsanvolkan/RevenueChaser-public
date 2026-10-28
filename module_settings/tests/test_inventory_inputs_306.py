"""Data Health row "Your inputs": the two watch lists and the board-settings rows, and the last change (#306, D36).

Since PR 4 the watch lists live in ops.sqlite (``rcdata.ops``): the row counts both lists and dates their newest
add or remove from their history, read without creating the file. The old raw ``watchlist`` table is unwritten and
no longer counted. Board settings are unchanged: ``board_settings`` and ``board_settings_changes`` in raw.duckdb.
"""
from __future__ import annotations

from datetime import datetime, timezone

from RevenueChaser.core import board_settings as bs
from RevenueChaser.core import rawstore
from RevenueChaser.module_settings import inventory
from RevenueChaser.module_settings.tests.test_inventory import FIXED, _rows, _seed
from RevenueChaser.rcdata import ops


def _day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def _last_watch_change() -> float:
    with ops.connect() as con:
        return max(con.execute(f"SELECT max(at) FROM watchlist_{k}_changes").fetchone()[0] or 0
                   for k in ops.WATCH_KINDS)


def test_inputs_row_on_an_empty_store_and_no_ops_file(rawstore_tmp_db):
    _seed()
    row = _rows(inventory.collect(now=FIXED))["inputs"]
    assert row["label"] == "Your inputs (watchlist, board settings)"
    assert row["tables"] == ["board_settings"]
    assert row["rows"] == 0 and row["last_date"] == "—" and row["fetch"] is None
    assert row["note"] == ("no inputs yet (press Opt or Stk next to a ticker on any board, or save a board "
                           "setting)")
    assert not ops.ops_path().exists()                  # the row reads ops.sqlite, it never creates it


def test_inputs_row_counts_both_watch_lists_and_dates_the_last_change(rawstore_tmp_db):
    _seed()
    ops.watch_add("stock", "AAA", by="leaps")
    ops.watch_add("option", "NVDA", by="sec_13f")
    ops.watch_add("stock", "BBB", by="leaps")
    ops.watch_remove("stock", "BBB", by="settings")    # off again: not counted, but the newest change
    bs.save("module_comovement", {"WINSOR_K": 6.5, "MIN_OBS": 100})
    bs.reset("module_comovement", ["MIN_OBS"])
    settings_last = rawstore.query("SELECT max(run_at) AS m FROM board_settings_changes")[0]["m"]
    last = max(settings_last, _last_watch_change())
    row = _rows(inventory.collect(now=FIXED))["inputs"]
    assert row["rows"] == 3
    assert row["last_date"] == _day(last)
    assert row["note"] == (f"watch lists: 1 option, 1 stock (ops.sqlite); 1 setting override; "
                           f"last change {_day(last)}")


def test_the_old_store_watchlist_table_is_not_counted(rawstore_tmp_db):
    """PR 4: the raw ``watchlist`` table is unwritten; a row left in it is not a watched name."""
    _seed()
    rawstore.write_rows("watchlist", [{"ticker": "OLD"}])
    row = _rows(inventory.collect(now=FIXED))["inputs"]
    assert row["rows"] == 0
    assert "OLD" not in row["note"]


def test_the_last_change_counts_a_removal(rawstore_tmp_db):
    """A Reset that empties the board is still the newest change, though no present row is left."""
    _seed()
    bs.save("module_comovement", {"WINSOR_K": 6.5})
    bs.reset("module_comovement")
    last = rawstore.query("SELECT max(run_at) AS m FROM board_settings_changes")[0]["m"]
    row = _rows(inventory.collect(now=FIXED))["inputs"]
    assert row["rows"] == 0 and row["last_date"] == _day(last)
    assert row["note"] == (f"watch lists: 0 option, 0 stock (ops.sqlite); 0 setting overrides; "
                           f"last change {_day(last)}")


def test_inputs_queries_are_pinned():
    assert ("board_settings_changes", "run_at") in set(inventory.QUERIED_COLUMNS)
    assert ("watchlist_changes", "run_at") not in set(inventory.QUERIED_COLUMNS)
