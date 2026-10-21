"""Schema drift: differences between a live store and the MANIFEST that ``_ensure_table`` leaves alone
are recorded and logged, and a fresh store's keys are tested against the declared ones (#285, B10).

``_ensure_table`` adds a missing column. It does not retype a column or change a key, and until now it
skipped both without a word. History tables will live for years, so their keys will change: the first
sign must not be a failed write. The record is kept in memory (``schema_drift()``) and logged; a lone
``_ensure_table`` writes nothing to the store, ``ensure_all_tables`` keeps what its apply saw in ``store_meta``
(#312 item 1, ``TestDriftInStoreMeta``), and nothing migrates by itself.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore


@pytest.fixture(autouse=True)
def _no_drift_from_other_tests():
    rawstore._SCHEMA_DRIFT.clear()
    yield
    rawstore._SCHEMA_DRIFT.clear()


def _live(ddl: str, db: str = "raw") -> None:
    with rawstore.connect(db=db) as conn:
        conn.execute(ddl)


def _ensure(name: str) -> None:
    rawstore._TABLE_READY.clear()
    rawstore.ensure_table(name)


_BOARD_SETTINGS = "board TEXT, key TEXT, value {value}, updated_at TEXT, PRIMARY KEY ({pk})"


class TestRecordedDrift:
    def test_a_retyped_column_is_recorded_logged_and_not_migrated(self, isolate_rawstore, caplog):
        _live("CREATE TABLE board_settings (" + _BOARD_SETTINGS.format(value="TEXT", pk="board, key") + ")")
        with caplog.at_level("WARNING"):
            _ensure("board_settings")
        assert rawstore.schema_drift() == [{
            "db": "raw", "table": "board_settings", "kind": "type", "column": "value",
            "live": "VARCHAR", "manifest": "DOUBLE"}]
        assert any("board_settings" in r.getMessage() and "value" in r.getMessage()
                   and "VARCHAR" in r.getMessage() and "DOUBLE" in r.getMessage()
                   for r in caplog.records)
        types = {r["column_name"]: r["data_type"] for r in rawstore.query(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'board_settings'")}
        assert types["value"] == "VARCHAR"                       # nothing migrated by itself

    def test_a_key_change_is_reported_not_skipped_silently(self, isolate_rawstore, caplog):
        _live("CREATE TABLE board_settings (" + _BOARD_SETTINGS.format(value="DOUBLE", pk="board") + ")")
        with caplog.at_level("WARNING"):
            _ensure("board_settings")
        assert rawstore.schema_drift() == [{
            "db": "raw", "table": "board_settings", "kind": "key", "column": None,
            "live": ["board"], "manifest": ["board", "key"]}]
        assert any("board_settings" in r.getMessage() and "key" in r.getMessage()
                   for r in caplog.records)

    def test_a_dropped_primary_key_is_reported(self, isolate_rawstore):
        _live("CREATE TABLE board_settings (board TEXT, key TEXT, value DOUBLE, updated_at TEXT)")
        _ensure("board_settings")
        assert [(d["kind"], d["live"], d["manifest"]) for d in rawstore.schema_drift()] == [
            ("key", [], ["board", "key"])]

    def test_a_live_column_the_manifest_no_longer_has_is_recorded(self, isolate_rawstore):
        _live("CREATE TABLE board_settings (" + _BOARD_SETTINGS.format(value="DOUBLE", pk="board, key")
              + ", legacy TEXT)")
        _ensure("board_settings")
        assert rawstore.schema_drift() == [{
            "db": "raw", "table": "board_settings", "kind": "extra_column", "column": "legacy",
            "live": "VARCHAR", "manifest": None}]

    def test_drift_in_another_store_file_names_that_file(self, isolate_rawstore):
        _live("CREATE TABLE edgar_documents (accession TEXT PRIMARY KEY, cik BIGINT, tickers TEXT, form TEXT, "
              "filed TEXT, fiscal_year TEXT, doc_url TEXT, path TEXT, sha256 TEXT, bytes_raw BIGINT, "
              "bytes_gzip BIGINT, fetched_at DOUBLE)", db="edgar")
        _ensure("edgar_documents")
        assert [(d["db"], d["table"], d["column"], d["live"], d["manifest"])
                for d in rawstore.schema_drift()] == [("edgar", "edgar_documents", "cik", "BIGINT", "VARCHAR")]

    def test_each_difference_is_recorded_and_logged_once_and_the_list_is_a_copy(self, isolate_rawstore, caplog):
        _live("CREATE TABLE board_settings (" + _BOARD_SETTINGS.format(value="TEXT", pk="board, key") + ")")
        with caplog.at_level("WARNING"):
            _ensure("board_settings")
            _ensure("board_settings")
        got = rawstore.schema_drift()
        assert len(got) == 1
        assert len([r for r in caplog.records if "schema drift" in r.getMessage()]) == 1
        got[0]["live"] = "TAMPERED"
        got.clear()
        assert [d["live"] for d in rawstore.schema_drift()] == ["VARCHAR"]

    def test_the_bulk_reconcile_records_the_same_drift(self, isolate_rawstore):
        _live("CREATE TABLE board_settings (" + _BOARD_SETTINGS.format(value="TEXT", pk="board") + ")")
        rawstore._TABLE_READY.clear()
        rawstore.ensure_all_tables()
        got = {(d["table"], d["kind"]) for d in rawstore.schema_drift()}
        assert got == {("board_settings", "type"), ("board_settings", "key")}

    def test_recording_writes_nothing_to_the_store(self, rawstore_tmp_db, monkeypatch):
        rawstore.execute("DROP TABLE board_settings")
        _live("CREATE TABLE board_settings (" + _BOARD_SETTINGS.format(value="TEXT", pk="board") + ")")
        before = rawstore.query("SELECT count(*) AS n FROM store_meta")[0]["n"]
        _ensure("board_settings")
        assert rawstore.schema_drift()
        assert rawstore.query("SELECT count(*) AS n FROM store_meta")[0]["n"] == before


class TestRebuildMessage:
    """A table with stale ``compress`` columns is recreated from the MANIFEST right after the check, so
    the log must not claim the difference was skipped (review of 2026-10-04, finding 6)."""

    _HEAD = "CREATE TABLE edgar_financials (ticker TEXT{pk}, annual JSON, quarterly JSON, meta JSON, "
    _OLD = {
        "extra_column": _HEAD.format(pk=" PRIMARY KEY") + "fetched_at DOUBLE, legacy TEXT)",
        "type": _HEAD.format(pk=" PRIMARY KEY") + "fetched_at TEXT)",
        "key": _HEAD.format(pk="") + "fetched_at DOUBLE)",
    }

    @pytest.mark.parametrize("kind", ["extra_column", "type", "key"])
    def test_the_log_says_the_rebuild_replaces_the_difference(self, isolate_rawstore, caplog, kind):
        _live(self._OLD[kind], db="edgar")
        with caplog.at_level("WARNING"):
            _ensure("edgar_financials")
        msgs = [r.getMessage() for r in caplog.records if "schema drift" in r.getMessage()]
        assert len(msgs) == 1
        assert "rebuild" in msgs[0] and "skipped" not in msgs[0]
        assert rawstore.schema_drift() == []                      # replaced, so no longer a difference

    def test_a_rebuild_that_fails_leaves_the_difference_on_record(self, isolate_rawstore, monkeypatch):
        _live(self._OLD["extra_column"], db="edgar")

        def boom(*a, **k):
            raise RuntimeError("rebuild failed")
        monkeypatch.setattr(rawstore, "_rebuild_compressed", boom)
        with pytest.raises(RuntimeError):
            _ensure("edgar_financials")
        assert [(d["table"], d["kind"], d["column"]) for d in rawstore.schema_drift()] == [
            ("edgar_financials", "extra_column", "legacy")]

    def test_only_the_rebuilt_table_loses_its_record(self, isolate_rawstore):
        _live("CREATE TABLE board_settings (" + _BOARD_SETTINGS.format(value="TEXT", pk="board, key") + ")")
        _live(self._OLD["extra_column"], db="edgar")
        _ensure("board_settings")
        _ensure("edgar_financials")
        assert [(d["table"], d["kind"]) for d in rawstore.schema_drift()] == [("board_settings", "type")]

    def test_without_a_rebuild_the_log_still_says_skipped(self, isolate_rawstore, caplog):
        _live("CREATE TABLE board_settings (" + _BOARD_SETTINGS.format(value="TEXT", pk="board, key") + ")")
        with caplog.at_level("WARNING"):
            _ensure("board_settings")
        msgs = [r.getMessage() for r in caplog.records if "schema drift" in r.getMessage()]
        assert len(msgs) == 1 and "skipped" in msgs[0] and "rebuild" not in msgs[0]


class TestBestEffort:
    def test_a_failing_drift_check_never_fails_the_write(self, isolate_rawstore, monkeypatch, caplog):
        _live("CREATE TABLE board_settings (" + _BOARD_SETTINGS.format(value="TEXT", pk="board, key") + ")")

        def boom(*a, **k):
            raise RuntimeError("probe failed")
        monkeypatch.setattr(rawstore, "_canon_type", boom)
        with caplog.at_level("WARNING"):
            rawstore.write_rows("board_settings", [{"board": "b", "key": "k", "value": "1.5"}])
        assert rawstore.count("board_settings") == 1
        assert any("drift check" in r.getMessage() for r in caplog.records)


class TestNotDrift:
    def test_a_column_added_to_the_manifest_is_added_not_drift(self, isolate_rawstore):
        _live("CREATE TABLE board_settings (board TEXT, key TEXT, value DOUBLE, PRIMARY KEY (board, key))")
        _ensure("board_settings")
        assert rawstore.schema_drift() == []
        assert rawstore.query("SELECT updated_at FROM board_settings") == []

    def test_a_compressed_column_that_is_rebuilt_is_not_drift(self, isolate_rawstore):
        _live("CREATE TABLE edgar_financials (ticker TEXT PRIMARY KEY, annual JSON, quarterly JSON, "
              "meta JSON, fetched_at DOUBLE)", db="edgar")
        _ensure("edgar_financials")
        assert rawstore.schema_drift() == []

    def test_a_second_reconcile_of_a_fresh_store_finds_no_drift_in_any_table(self, isolate_rawstore):
        """Every MANIFEST type, STRUCT and array included, must compare equal to what DuckDB reports."""
        rawstore.ensure_all_tables()
        rawstore._TABLE_READY.clear()
        rawstore.ensure_all_tables()
        assert rawstore.schema_drift() == []

    def test_a_table_created_by_the_reconcile_is_not_compared_with_itself(self, isolate_rawstore):
        _ensure("board_settings")
        assert rawstore.schema_drift() == []


# ── a fresh store's keys match the keys the MANIFEST declares ────────────────────────────────
# Tables written in "replace" mode (rows grouped by ``key`` columns) or key-less, which carry no PRIMARY
# KEY constraint by design. A table not listed here must have one that equals its ``key``.
_NO_PRIMARY_KEY = {
    "comov_nodes", "comov_members", "comov_lag", "chain_mentions", "swing_ohlc", "rotation_snapshots",
    "index_constituents", "oipd_chain",
}


def _key_mismatches() -> list[str]:
    live: dict[str, list[str]] = {}
    rawstore.ensure_history()           # the history file exists only after the first recorded run
    for db in ("raw", "edgar", "discord", "history", "prices"):
        with rawstore.connect(read_only=True, db=db) as conn:
            for table, cols in conn.execute(
                    "SELECT table_name, constraint_column_names FROM duckdb_constraints() "
                    "WHERE schema_name = 'main' AND constraint_type = 'PRIMARY KEY'").fetchall():
                live[table] = list(cols)
    out = []
    for name, spec in rawstore.MANIFEST.items():
        declared = [k.strip('"') for k in spec.get("key") or []]
        if name in _NO_PRIMARY_KEY:
            if name in live:
                out.append(f"{name}: listed as having no PRIMARY KEY but the store has {live[name]}")
        elif live.get(name) != declared:
            out.append(f"{name}: PRIMARY KEY {live.get(name)} != declared key {declared}")
    return out


def test_a_fresh_store_has_the_keys_the_manifest_declares(rawstore_tmp_db):
    assert _key_mismatches() == []


def test_the_key_check_fails_when_a_declared_key_differs(rawstore_tmp_db, monkeypatch):
    spec = rawstore.MANIFEST["board_settings"]
    monkeypatch.setitem(rawstore.MANIFEST, "board_settings", {**spec, "key": ["board"]})
    assert _key_mismatches() == ["board_settings: PRIMARY KEY ['board', 'key'] != declared key ['board']"]


def test_a_table_without_a_primary_key_must_be_listed(rawstore_tmp_db, monkeypatch):
    spec = rawstore.MANIFEST["board_settings"]
    monkeypatch.setitem(rawstore.MANIFEST, "board_settings", {
        **spec, "columns": [c for c in spec["columns"] if not c.startswith("PRIMARY KEY")]})
    rawstore.execute("DROP TABLE board_settings")
    rawstore._TABLE_READY.clear()
    rawstore.ensure_table("board_settings")
    assert _key_mismatches() == ["board_settings: PRIMARY KEY None != declared key ['board', 'key']"]


# ── #312 item 1: the drift the schema apply saw is kept in store_meta ─────────────────────────────
# ``ensure_all_tables`` (the first store call of a process, read-write) upserts ``store_meta`` key
# ``schema_drift`` = {"recorded_at", "drift": [...]} when the list differs from the stored one: a store with
# drift gets the row, a fixed store gets an empty list (the record clears), an unchanged list writes nothing.


def _new_process() -> None:
    """What a new dashboard or job process starts with: no drift in memory, no table reconciled yet."""
    rawstore._SCHEMA_DRIFT.clear()
    rawstore._TABLE_READY.clear()


def _stored():
    row = rawstore.get("store_meta", "schema_drift")
    return None if row is None else row["value"]


_OLD_KEY = "CREATE TABLE board_settings (" + _BOARD_SETTINGS.format(value="DOUBLE", pk="board") + ")"


class TestDriftInStoreMeta:
    def test_a_store_with_drift_records_it_in_store_meta(self, isolate_rawstore):
        _live(_OLD_KEY)
        rawstore.ensure_all_tables()
        value = _stored()
        assert value["drift"] == [{"db": "raw", "table": "board_settings", "kind": "key", "column": None,
                                   "live": ["board"], "manifest": ["board", "key"]}]
        assert value["recorded_at"][:2] == "20" and value["recorded_at"].endswith("Z")

    def test_drift_in_another_file_is_recorded_with_its_db(self, isolate_rawstore):
        _live("CREATE TABLE edgar_documents (accession TEXT PRIMARY KEY, cik BIGINT, tickers TEXT, form TEXT, "
              "filed TEXT, fiscal_year TEXT, doc_url TEXT, path TEXT, sha256 TEXT, bytes_raw BIGINT, "
              "bytes_gzip BIGINT, fetched_at DOUBLE)", db="edgar")
        rawstore.ensure_all_tables()
        assert [(d["db"], d["table"], d["column"]) for d in _stored()["drift"]] == [
            ("edgar", "edgar_documents", "cik")]

    def test_a_fresh_store_without_drift_writes_no_row(self, isolate_rawstore):
        rawstore.ensure_all_tables()
        assert _stored() is None

    def test_a_fixed_store_clears_the_record(self, isolate_rawstore):
        _live(_OLD_KEY)
        rawstore.ensure_all_tables()
        assert _stored()["drift"]
        rawstore.execute("DROP TABLE board_settings")
        _new_process()
        rawstore.ensure_all_tables()
        assert _stored()["drift"] == []

    def test_an_unchanged_drift_list_is_not_written_again(self, isolate_rawstore, monkeypatch):
        _live(_OLD_KEY)
        rawstore.ensure_all_tables()
        first = _stored()
        writes = []
        real = rawstore.write_rows
        monkeypatch.setattr(rawstore, "write_rows", lambda name, rows: writes.append(name) or real(name, rows))
        _new_process()
        rawstore.ensure_all_tables()
        assert "store_meta" not in writes and _stored() == first

    def test_drift_another_store_file_showed_in_this_process_is_not_recorded_here(self, isolate_rawstore,
                                                                                    tmp_path, monkeypatch):
        """``schema_drift()`` is per process; the record is what THIS store's apply saw."""
        _live(_OLD_KEY)
        rawstore.ensure_all_tables()
        assert rawstore.schema_drift()
        monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "other" / "raw.duckdb")
        (tmp_path / "other").mkdir()
        rawstore._TABLE_READY.clear()
        rawstore.ensure_all_tables()
        assert _stored() is None

    def test_a_failed_record_write_never_fails_the_apply(self, isolate_rawstore, monkeypatch, caplog):
        _live(_OLD_KEY)
        real = rawstore.write_rows

        def refuse(name, rows):
            if name == "store_meta":
                raise rawstore.RawStoreError("store is locked")
            return real(name, rows)
        monkeypatch.setattr(rawstore, "write_rows", refuse)
        with caplog.at_level("WARNING"):
            rawstore.ensure_all_tables()
        assert [(d["table"], d["kind"]) for d in rawstore.schema_drift()] == [("board_settings", "key")]
        assert any("schema drift" in r.getMessage() and "store_meta" in r.getMessage() for r in caplog.records)
        monkeypatch.setattr(rawstore, "write_rows", real)
        assert _stored() is None

    def test_a_read_with_auto_ensure_off_writes_nothing(self, isolate_rawstore, monkeypatch):
        """The read-only protocol (``RC_STORE_AUTO_ENSURE=0`` + ``read_only=True``) never writes the record."""
        import duckdb
        _live(_OLD_KEY)
        rawstore.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
        _new_process()
        rawstore._ENSURED.clear()
        monkeypatch.setenv("RC_STORE_AUTO_ENSURE", "0")
        assert rawstore.query("SELECT count(*) AS n FROM board_settings")[0]["n"] == 0
        with duckdb.connect(str(rawstore.DB_PATH), read_only=True) as conn:
            assert conn.execute("SELECT count(*) FROM store_meta").fetchone()[0] == 0
        monkeypatch.setenv("RC_STORE_AUTO_ENSURE", "1")      # the same read with the apply on records it
        assert rawstore.query("SELECT count(*) AS n FROM board_settings")[0]["n"] == 0
        assert _stored()["drift"]
