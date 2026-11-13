"""Data Health row "Your inputs": the board-settings rows and their last change (#306, D36).

The row counted the two job watch lists as well until avo, 13 Nov 2026 ("Watchlist panosuna bunları ekle ... ve
buradan yine de kaldır onları"): they are named lists on the Watch lists board, each named for what reads it, so
Settings keeps no second copy. Board settings are unchanged: ``board_settings`` and ``board_settings_changes`` in
raw.duckdb. The row still never opens ops.sqlite.
"""
from __future__ import annotations

from datetime import datetime, timezone

from RevenueChaser.core import board_settings as bs
from RevenueChaser.core import rawstore
from RevenueChaser.module_settings import inventory
from RevenueChaser.module_settings.tests.test_inventory import FIXED, _rows, _seed
from RevenueChaser.rcdata import ops

WATCH_WORDS = "the watch lists are on the Watch lists board"


def _day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def test_inputs_row_on_an_empty_store_and_no_ops_file(rawstore_tmp_db):
    _seed()
    row = _rows(inventory.collect(now=FIXED))["inputs"]
    assert row["label"] == "Your inputs (board settings)"
    assert row["tables"] == ["board_settings"]
    assert row["rows"] == 0 and row["last_date"] == "—" and row["fetch"] is None
    assert row["note"] == f"no inputs yet (save a board setting; {WATCH_WORDS})"
    assert not ops.ops_path().exists()                  # the row never creates ops.sqlite, and no longer reads it


def test_a_watched_name_is_not_counted_here_any_more(rawstore_tmp_db):
    """avo, 13 Nov 2026: the lists live on their own board; this row is the board settings and their date."""
    _seed()
    ops.watch_add("stock", "AAA", by="leaps")
    ops.watch_add("option", "NVDA", by="sec_13f")
    bs.save("module_comovement", {"WINSOR_K": 6.5, "MIN_OBS": 100})
    bs.reset("module_comovement", ["MIN_OBS"])
    settings_last = rawstore.query("SELECT max(run_at) AS m FROM board_settings_changes")[0]["m"]
    row = _rows(inventory.collect(now=FIXED))["inputs"]
    assert row["rows"] == 1                             # the one setting override, neither watched name
    assert row["last_date"] == _day(settings_last)      # a watch-list add is not this row's newest change
    assert row["note"] == f"1 setting override; last change {_day(settings_last)} · {WATCH_WORDS}"
    assert "AAA" not in row["note"] and "NVDA" not in row["note"]


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
    assert row["note"] == f"0 setting overrides; last change {_day(last)} · {WATCH_WORDS}"


def test_inputs_queries_are_pinned():
    assert ("board_settings_changes", "run_at") in set(inventory.QUERIED_COLUMNS)
    assert ("watchlist_changes", "run_at") not in set(inventory.QUERIED_COLUMNS)
