"""Board result files (DECISIONS S9, S16): a board's tables live in ``<store>/boards/<board>.duckdb``.

The first, narrow step of the board-file plan (2026-11-08 migration plan): only boards born with their own file are
listed in ``rawstore.BOARD_DBS`` (News and Welcome). A MANIFEST table that declares ``"db": "<board>"`` is written to
and read from that file only; the apply creates the file with its schema stamp; the writer's presence marker lands
beside it (plan §9, R4), so the weekly backup's ``boards`` step sees a peer, not a foreign process.
"""
from __future__ import annotations

import duckdb
import pytest

from RevenueChaser.core import rawstore

_TABLE = "board_files_probe"


@pytest.fixture
def probe_table(monkeypatch):
    monkeypatch.setattr(rawstore, "BOARD_DBS", ("news",))
    monkeypatch.setitem(rawstore.MANIFEST, _TABLE, {
        "key": ["k"], "columns": ["k TEXT PRIMARY KEY", "v TEXT"],
        "kind": "tabular", "write": "upsert", "history_mode": "operational", "db": "news", "sources": [],
    })
    return _TABLE


def _tables(path):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    finally:
        con.close()


def test_board_path_is_under_boards(isolate_rawstore):
    assert rawstore.board_db_path("news") == isolate_rawstore.parent / "boards" / "news.duckdb"
    assert rawstore._db_path("news") == rawstore.board_db_path("news")


def test_news_and_welcome_are_board_files():
    assert set(rawstore.BOARD_DBS) == {"news", "welcome"}


@pytest.mark.rawstore_schema_from_scratch
def test_a_board_table_is_written_to_its_own_file_only(isolate_rawstore, probe_table):
    rawstore.write_rows(probe_table, [{"k": "a", "v": "1"}])
    board = isolate_rawstore.parent / "boards" / "news.duckdb"
    assert board.exists()
    assert probe_table in _tables(board)
    assert not isolate_rawstore.exists() or probe_table not in _tables(isolate_rawstore)
    assert rawstore.query(f"SELECT v FROM {probe_table}", db="news") == [{"v": "1"}]


@pytest.mark.rawstore_schema_from_scratch
def test_the_apply_makes_the_board_file_with_its_stamp(isolate_rawstore, probe_table):
    rawstore.ensure_all_tables()
    board = isolate_rawstore.parent / "boards" / "news.duckdb"
    assert probe_table in _tables(board)
    assert rawstore.schema_stamp("news") == rawstore._SCHEMA_VERSION


@pytest.mark.rawstore_schema_from_scratch
def test_the_writer_leaves_its_presence_marker_beside_the_board_file(isolate_rawstore, probe_table):
    rawstore.write_rows(probe_table, [{"k": "a", "v": "1"}])
    boards = isolate_rawstore.parent / "boards"
    assert any(p.name.startswith(rawstore._PRESENCE_PREFIX) for p in boards.iterdir())
