"""Data Health row "Your inputs": present watchlist and settings rows, and the last change (#306, D36)."""
from __future__ import annotations

from datetime import datetime, timezone

from RevenueChaser.core import board_settings as bs
from RevenueChaser.core import rawstore
from RevenueChaser.module_settings import inventory
from RevenueChaser.module_settings.tests.test_inventory import FIXED, _rows, _seed


def _day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def test_inputs_row_on_an_empty_store(rawstore_tmp_db):
    _seed()
    row = _rows(inventory.collect(now=FIXED))["inputs"]
    assert row["label"] == "Your inputs (watchlist, board settings)"
    assert row["tables"] == ["watchlist", "board_settings"]
    assert row["rows"] == 0 and row["last_date"] == "—" and row["fetch"] is None
    assert row["note"] == "no inputs yet (pin a ticker on LEAPS or save a board setting)"


def test_inputs_row_counts_present_rows_and_dates_the_last_change(rawstore_tmp_db):
    _seed()
    # The old ``watchlist`` table this row counts is no longer the toggle's (PR 4: the stock list in ops.sqlite);
    # it is written here as a store-table row with its change rows, which is all this row reads.
    rawstore.write_rows("watchlist", [{"ticker": "AAA"}, {"ticker": "BBB"}])
    with rawstore.connect() as conn:
        conn.execute("DELETE FROM watchlist WHERE ticker = 'BBB'")
    rawstore.sync_changes("test")              # BBB off again: an 'absent' row, not a present one
    bs.save("module_comovement", {"WINSOR_K": 6.5, "MIN_OBS": 100})
    bs.reset("module_comovement", ["MIN_OBS"])
    last = max(r["m"] for r in rawstore.query(
        "SELECT max(run_at) AS m FROM watchlist_changes UNION ALL SELECT max(run_at) FROM board_settings_changes"))
    row = _rows(inventory.collect(now=FIXED))["inputs"]
    assert row["rows"] == 2
    assert row["last_date"] == _day(last)
    assert row["note"] == f"watchlist 1 ticker, 1 setting override; last change {_day(last)}"


def test_the_last_change_counts_a_removal(rawstore_tmp_db):
    """A Reset that empties the board is still the newest change, though no present row is left."""
    _seed()
    bs.save("module_comovement", {"WINSOR_K": 6.5})
    bs.reset("module_comovement")
    last = rawstore.query("SELECT max(run_at) AS m FROM board_settings_changes")[0]["m"]
    row = _rows(inventory.collect(now=FIXED))["inputs"]
    assert row["rows"] == 0 and row["last_date"] == _day(last)
    assert row["note"] == f"watchlist 0 tickers, 0 setting overrides; last change {_day(last)}"


def test_inputs_queries_are_pinned():
    pairs = {("watchlist_changes", "run_at"), ("board_settings_changes", "run_at")}
    assert pairs <= set(inventory.QUERIED_COLUMNS)


def test_rows_without_a_change_row_still_show_their_count(rawstore_tmp_db):
    """A row written around the store API has no change row until the next sync: the note follows the count."""
    _seed()
    with rawstore.connect() as conn:
        conn.execute("INSERT INTO watchlist VALUES ('BYPASS')")
    row = _rows(inventory.collect(now=FIXED))["inputs"]
    assert row["rows"] == 1 and row["last_date"] == "—"
    assert row["note"] == "watchlist 1 ticker, 0 setting overrides; last change —"
