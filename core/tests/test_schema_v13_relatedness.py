"""Schema 12 -> 13 (#401, Relatedness v2 Step 1): two nullable columns on ``relatedness_profiles`` and the
``relatedness_embeddings`` table. Additive: nothing already stored is rewritten.

The v12 store is built from a MANIFEST without the v13 columns and table and filled through ``write_rows``; then
the real MANIFEST is restored and ``ensure_all_tables`` runs again, the way the dashboard's first store call does
after the merge.
"""
from __future__ import annotations

import duckdb
import pytest

from RevenueChaser.core import rawstore

V13_COLUMNS = {"relatedness_profiles": ("profile_json", "label_source")}
V13_TABLE = "relatedness_embeddings"
_PROFILE = {"ticker": "AAPL", "cik": "320193", "model": "m", "prompt_version": "v2", "fetched_at": 1.0,
            "cache_key": "k", "status": "ok", "attempts": 0, "provider": "evren"}


def _forget_reconciliation():
    rawstore._TABLE_READY.clear()
    rawstore._ENSURED.clear()


def _snapshot(path):
    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position").fetchall()
    finally:
        con.close()
    out: dict[str, list] = {}
    for t, c, ty in rows:
        out.setdefault(t, []).append((c, ty))
    return out


def _columns(path, table):
    return {c for c, _ in _snapshot(path).get(table, [])}


@pytest.fixture
def v12_store(isolate_rawstore):
    """A v12 store with one profile row; yields that row as stored."""
    mp = pytest.MonkeyPatch()
    try:
        mp.delitem(rawstore.MANIFEST, V13_TABLE)
        for t, cols in V13_COLUMNS.items():
            spec = rawstore.MANIFEST[t]
            mp.setitem(rawstore.MANIFEST, t, {
                **spec, "columns": [c for c in spec["columns"] if c.split()[0] not in cols]})
        _forget_reconciliation()
        rawstore.ensure_all_tables()
        rawstore.write_rows("relatedness_profiles", [_PROFILE])
    finally:
        mp.undo()
    assert V13_TABLE not in _snapshot(rawstore.board_db_path("relatedness"))
    assert not set(V13_COLUMNS["relatedness_profiles"]) & _columns(rawstore.board_db_path("relatedness"), "relatedness_profiles")
    yield rawstore.get("relatedness_profiles", "AAPL")


def _migrate():
    _forget_reconciliation()
    rawstore.ensure_all_tables()


def test_the_label_is_13():
    assert rawstore.schema_version() == "13"


def test_a_v12_store_gains_the_columns_and_the_table_and_keeps_its_row(v12_store):
    _migrate()
    assert {"profile_json", "label_source"} <= _columns(rawstore.board_db_path("relatedness"), "relatedness_profiles")
    assert {"ticker", "model", "dim", "vector"} <= _columns(rawstore.board_db_path("relatedness"), V13_TABLE)
    assert V13_TABLE not in _snapshot(rawstore.edgar_db_path())
    row = rawstore.get("relatedness_profiles", "AAPL")
    assert row["profile_json"] is None and row["label_source"] is None
    for name, was in v12_store.items():
        assert row[name] == was
    assert rawstore.count("relatedness_profiles") == 1


def test_the_new_columns_hold_json_and_the_vector_holds_bytes(rawstore_tmp_db):
    profile = {"summary": "Makes phones.", "segments": [{"name": "iPhone", "share": 0.52, "source": "table"}]}
    rawstore.write_rows("relatedness_profiles", [{**_PROFILE, "profile_json": profile,
                                                  "label_source": {"segments": "table"}}])
    row = rawstore.get("relatedness_profiles", "AAPL")
    assert row["profile_json"] == profile and row["label_source"] == {"segments": "table"}
    vec = bytes(range(16))
    rawstore.write_rows(V13_TABLE, [{"ticker": "AAPL", "model": "bge-small-en-v1.5", "dim": 4, "vector": vec}])
    rawstore.write_rows(V13_TABLE, [{"ticker": "AAPL", "model": "bge-small-en-v1.5", "dim": 4, "vector": vec[::-1]}])
    assert rawstore.count(V13_TABLE) == 1                      # keyed on (ticker, model): an upsert
    assert bytes(rawstore.get(V13_TABLE, ("AAPL", "bge-small-en-v1.5"))["vector"]) == vec[::-1]


def test_a_second_ensure_changes_nothing(v12_store):
    _migrate()
    before = _snapshot(rawstore.board_db_path("relatedness"))
    _migrate()
    assert _snapshot(rawstore.board_db_path("relatedness")) == before
    assert rawstore.count("relatedness_profiles") == 1
