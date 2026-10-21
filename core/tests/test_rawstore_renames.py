"""Idempotent table renames (``rawstore._RENAMES``) — the Phase 1 migration hook."""
from __future__ import annotations

from RevenueChaser.core import rawstore


def test_apply_renames_is_idempotent_and_preserves_data(rawstore_tmp_db, monkeypatch):
    rawstore.execute("CREATE TABLE old_x (a INTEGER)")
    rawstore.execute("INSERT INTO old_x VALUES (1)")
    monkeypatch.setattr(rawstore, "_RENAMES", {"old_x": "new_x"})

    rawstore.ensure_all_tables()
    kinds = {r["table_name"]: r["table_type"] for r in rawstore.query(
        "SELECT table_name, table_type FROM information_schema.tables "
        "WHERE table_schema='main'")}
    assert kinds.get("new_x") == "BASE TABLE"
    # compat VIEWs are retired (Phase 1 P2): the old name is gone, not aliased
    assert "old_x" not in kinds
    assert rawstore.query("SELECT count(*) n FROM new_x")[0]["n"] == 1

    # second run: old name is gone, nothing to do, no error
    rawstore.ensure_all_tables()
    assert rawstore.query("SELECT count(*) n FROM new_x")[0]["n"] == 1


def test_rename_is_skipped_when_new_already_exists(rawstore_tmp_db, monkeypatch):
    rawstore.execute("CREATE TABLE old_y (a INTEGER)")
    rawstore.execute("CREATE TABLE new_y (a INTEGER)")
    rawstore.execute("INSERT INTO new_y VALUES (7)")
    monkeypatch.setattr(rawstore, "_RENAMES", {"old_y": "new_y"})

    rawstore.ensure_all_tables()  # must not raise / must not clobber new_y
    assert rawstore.query("SELECT count(*) n FROM new_y")[0]["n"] == 1
