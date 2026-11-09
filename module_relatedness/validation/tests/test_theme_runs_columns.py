"""The five nullable validation columns on ``theme_runs`` (Relatedness v2 Step 0, #341).

Tested on the per-test temporary store only (``rawstore_tmp_db``); never a live store.
"""
from __future__ import annotations

import duckdb
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness.validation import metrics

# theme_runs as it stood before this change (schema 11/12).
OLD_COLUMNS = ["id", "created_at", "k_fine", "k_medium", "k_coarse", "jaccard", "is_current",
               "session_date", "run_at"]
OLD_DDL = ("CREATE TABLE theme_runs (id TEXT PRIMARY KEY, created_at TEXT, k_fine INTEGER, "
           "k_medium INTEGER, k_coarse INTEGER, jaccard DOUBLE, is_current INTEGER, "
           "session_date DATE, run_at DOUBLE)")
OLD_ROW = ("run-1", "2026-10-30T01:00:00", 120, 60, 11, 0.42, 1, "2026-10-30", 1761786000.0)


def _columns(path):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return con.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'theme_runs' ORDER BY ordinal_position").fetchall()
    finally:
        con.close()


def _seed_old_table(path):
    con = duckdb.connect(str(path))
    try:
        con.execute("DROP TABLE IF EXISTS theme_runs")
        con.execute(OLD_DDL)
        con.execute("INSERT INTO theme_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", list(OLD_ROW))
    finally:
        con.close()


def _apply(monkeypatch):
    monkeypatch.setattr(rawstore, "_TABLE_READY", set())  # forget this process's memo, as a new process would
    rawstore.ensure_table("theme_runs")


def test_manifest_adds_exactly_five_columns_after_the_old_ones():
    cols = rawstore._columns_of(rawstore.MANIFEST["theme_runs"])
    assert cols[:len(OLD_COLUMNS)] == OLD_COLUMNS
    assert tuple(cols[len(OLD_COLUMNS):]) == metrics.THEME_RUNS_COLUMNS
    assert len(metrics.THEME_RUNS_COLUMNS) == 5


@pytest.mark.rawstore_schema_from_scratch
def test_migration_adds_five_nullable_columns_and_keeps_rows(rawstore_tmp_db, monkeypatch):
    path = rawstore.DB_PATH
    _seed_old_table(path)
    _apply(monkeypatch)
    cols = _columns(path)
    names = [c[0] for c in cols]
    assert names[:len(OLD_COLUMNS)] == OLD_COLUMNS
    new = cols[len(OLD_COLUMNS):]
    assert tuple(c[0] for c in new) == metrics.THEME_RUNS_COLUMNS
    assert all(c[1] == "DOUBLE" and c[2] == "YES" for c in new)
    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute(f"SELECT {', '.join(names)} FROM theme_runs").fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    old_part, new_part = rows[0][:len(OLD_COLUMNS)], rows[0][len(OLD_COLUMNS):]
    assert str(old_part[7]) == OLD_ROW[7]
    assert old_part[:7] + old_part[8:] == OLD_ROW[:7] + OLD_ROW[8:]
    assert new_part == (None,) * 5


@pytest.mark.rawstore_schema_from_scratch
def test_migration_applies_twice_without_error(rawstore_tmp_db, monkeypatch):
    path = rawstore.DB_PATH
    _seed_old_table(path)
    _apply(monkeypatch)
    first = _columns(path)
    _apply(monkeypatch)
    assert _columns(path) == first


def test_a_run_row_round_trips_the_validation_values(rawstore_tmp_db):
    row = {"id": "run-v", "created_at": "2026-11-01T00:00:00", "k_fine": 10, "k_medium": 5, "k_coarse": 2,
           "jaccard": 0.5, "is_current": 0}
    row.update({"val_peer_recall_25": 0.5, "val_hand_precision_10": 0.4, "val_gics_purity": 0.7,
                "val_etf_recall_medium": 0.6, "val_seed_ari": None})
    rawstore.write_rows("theme_runs", [row])
    got = rawstore.get("theme_runs", "run-v")
    assert got["val_peer_recall_25"] == 0.5 and got["val_seed_ari"] is None
