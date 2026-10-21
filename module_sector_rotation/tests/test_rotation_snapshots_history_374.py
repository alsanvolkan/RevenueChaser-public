"""#374 (avo's card tap, 2026-10-10): the RRG points of a live Rotation Fetch are kept per session.

``rotation_snapshots`` gets a ``source`` column (``live`` from a Rotation Fetch, ``backfill`` from a range backfill),
and ``rotation_snapshots_history`` mirrors the rows a live Fetch saved, keyed ``(run_id, row_no)``. Only the live
Fetch records them; a range Fetch shows stored snapshots, so its run is partial and never the session's snapshot.
Offline; temp stores.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_sector_rotation import history as rot_history, store as rotation_store
from RevenueChaser.module_sector_rotation.tests.test_rotation_history_374 import HOLDINGS, OK_HEALTH, SNAPS, TREE
from RevenueChaser.module_sector_rotation.tests.d01_seed import ny, pin_clock, seed
from RevenueChaser.module_sector_rotation.tests.test_rotation_store import _FakeUW, _snap

COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()
TABLE = "rotation_snapshots_history"


def _rows(run_id):
    return rawstore.query(f"SELECT * FROM {TABLE} WHERE run_id = ? ORDER BY row_no", [run_id], db="history")


def test_a_live_save_stamps_source_live_and_returns_the_rows_it_wrote(rawstore_tmp_db):
    rows = rotation_store.save_snapshot([_snap("XLK"), _snap("XLF")], as_of=date(2026, 8, 14))
    stored = rawstore.query("SELECT * FROM rotation_snapshots ORDER BY ticker")
    assert {r["source"] for r in stored} == {"live"} and sorted(r["ticker"] for r in rows) == ["XLF", "XLK"]
    assert sorted(rows, key=lambda r: r["ticker"]) == stored


def test_a_backfill_stamps_source_backfill(rawstore_tmp_db, monkeypatch):
    import RevenueChaser.module_sector_rotation.store as rs
    pin_clock(monkeypatch, ny(2026, 8, 14, 18))
    seed("SPY", date(2026, 8, 14), closes=lambda i: 100.0 + i)
    seed("XLK", date(2026, 8, 14), closes=lambda i: 100.0 + i)
    assert rs.backfill_snapshots(_FakeUW(), days=2) == 2
    assert {r["source"] for r in rawstore.query("SELECT source FROM rotation_snapshots")} == {"backfill"}


def test_a_live_fetch_records_the_snapshot_rows_it_saved(rawstore_tmp_db):
    saved = rotation_store.save_snapshot([_snap("XLK"), _snap("XLF")], as_of=date(2026, 10, 6))
    counts = rot_history.record_rotation(SNAPS, HOLDINGS, TREE, run_id="r1", run_at=COMPLETE, uw_health=OK_HEALTH,
                                         snapshot_rows=saved)
    assert counts[TABLE] == 2
    got = [{k: v for k, v in r.items() if k not in ("run_id", "row_no")} for r in _rows("r1")]
    assert got == saved and [r["row_no"] for r in _rows("r1")] == [0, 1]


def test_a_range_fetch_is_partial_and_keeps_no_rows(rawstore_tmp_db):
    rot_history.record_rotation(SNAPS, HOLDINGS, TREE, run_id="r1", run_at=COMPLETE, uw_health=OK_HEALTH,
                                window=(date(2026, 8, 1), date(2026, 8, 31)))
    (head,) = rawstore.query("SELECT * FROM result_runs WHERE board = 'module_sector_rotation'", db="history")
    assert head["quality"] == "partial" and "a range Fetch shows stored snapshots" in head["quality_note"]
    assert _rows("r1") == [] and json.loads(head["context"])["window"] == ["2026-08-01", "2026-08-31"]


def test_the_mirror_has_the_latest_tables_columns_and_a_row_no_key():
    spec = rawstore.MANIFEST[TABLE]
    assert spec["key"] == ["run_id", "row_no"] and spec["mirror_of"] == "rotation_snapshots"
    assert spec["board"] == "module_sector_rotation" and spec["snapshot"] == "session"
    latest = {c for c in rawstore._columns_of(rawstore.MANIFEST["rotation_snapshots"])}
    assert latest | {"run_id", "row_no"} == set(rawstore._columns_of(spec)) and "source" in latest
