"""``scripts/migrate_board_store.py``: the copy of a board's tables from the old store file into its board file
(plan 2026-11-08 §2 step 5, §8): by key, twice is the same as once, counts per table, ``--dry-run``,
``--rename-legacy`` and ``--reverse``."""
from __future__ import annotations

import duckdb
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.scripts import migrate_board_store as mig

_TABLE = "migrate_probe"


@pytest.fixture
def old_rows(monkeypatch, isolate_rawstore):
    """Three rows in raw.duckdb, then MANIFEST moves the table to the news board (what a board PR does)."""
    spec = {"key": ["k"], "columns": ["k TEXT PRIMARY KEY", "v TEXT"], "kind": "tabular", "write": "upsert",
            "history_mode": "operational", "sources": []}
    con = duckdb.connect(str(isolate_rawstore))
    con.execute(f"CREATE TABLE {_TABLE} (k TEXT PRIMARY KEY, v TEXT)")
    con.execute(f"INSERT INTO {_TABLE} VALUES ('a', '1'), ('b', '2'), ('c', '3')")
    con.close()
    monkeypatch.setattr(rawstore, "BOARD_DBS", ("news",))
    monkeypatch.setattr(rawstore, "_SCHEMA_FILES", ("raw", "edgar", "history", "prices", "news"))
    monkeypatch.setitem(rawstore.MANIFEST, _TABLE, dict(spec, db="news"))
    return isolate_rawstore


def _rows(path, table):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return sorted(con.execute(f"SELECT k, v FROM {table}").fetchall())
    finally:
        con.close()


@pytest.mark.rawstore_schema_from_scratch
def test_copy_counts_and_twice_is_once(old_rows):
    # Only the probe: the news board's own tables are in MANIFEST too, and absent from this source.
    report = [r for r in mig.migrate("news", source="raw") if r["table"] == _TABLE]
    assert report == [{"table": _TABLE, "source": 3, "before": 0, "copied": 3, "after": 3, "skipped": 0,
                       "left_out": []}]
    assert _rows(rawstore.board_db_path("news"), _TABLE) == [("a", "1"), ("b", "2"), ("c", "3")]
    again = [r for r in mig.migrate("news", source="raw") if r["table"] == _TABLE]
    assert again[0]["copied"] == 0 and again[0]["after"] == 3 and again[0]["skipped"] == 3


@pytest.mark.rawstore_schema_from_scratch
def test_a_table_without_a_primary_key_copies_once_both_ways(old_rows, monkeypatch):
    con = duckdb.connect(str(old_rows))
    con.execute("CREATE TABLE migrate_nopk (k TEXT, v TEXT)")
    con.execute("INSERT INTO migrate_nopk VALUES ('a', '1'), ('a', '1'), ('b', '2')")
    con.close()
    monkeypatch.setitem(rawstore.MANIFEST, "migrate_nopk", {
        "key": ["k"], "columns": ["k TEXT", "v TEXT"], "kind": "tabular", "write": "replace",
        "history_mode": "operational", "sources": [], "db": "news"})
    mig.migrate("news", source="raw")
    again = {r["table"]: r for r in mig.migrate("news", source="raw")}
    assert again["migrate_nopk"]["after"] == 3 and again["migrate_nopk"]["copied"] == 0
    mig.rename_legacy("news", source="raw")
    mig.reverse("news", source="raw")
    assert len(_rows(old_rows, "legacy_migrate_nopk")) == 3


@pytest.mark.rawstore_schema_from_scratch
def test_dry_run_writes_nothing(old_rows):
    report = mig.migrate("news", source="raw", dry_run=True)
    assert report[0]["source"] == 3 and report[0]["copied"] == 0
    assert not rawstore.board_db_path("news").exists()


@pytest.mark.rawstore_schema_from_scratch
def test_rename_legacy_then_reverse(old_rows):
    mig.migrate("news", source="raw")
    mig.rename_legacy("news", source="raw")
    con = duckdb.connect(str(old_rows), read_only=True)
    names = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    con.close()
    assert f"legacy_{_TABLE}" in names and _TABLE not in names
    rawstore.write_rows(_TABLE, [{"k": "b", "v": "new"}, {"k": "d", "v": "4"}])   # after the deploy
    back = mig.reverse("news", source="raw")
    assert back == [{"table": _TABLE, "copied": 4}]
    assert _rows(old_rows, f"legacy_{_TABLE}") == [("a", "1"), ("b", "new"), ("c", "3"), ("d", "4")]


@pytest.mark.rawstore_schema_from_scratch
def test_a_table_missing_from_the_source_is_reported_not_raised(old_rows, monkeypatch):
    monkeypatch.setitem(rawstore.MANIFEST, "migrate_absent", dict(rawstore.MANIFEST[_TABLE]))
    report = {r["table"]: r for r in mig.migrate("news", source="raw")}
    assert report["migrate_absent"]["source"] is None and report["migrate_absent"]["copied"] == 0
