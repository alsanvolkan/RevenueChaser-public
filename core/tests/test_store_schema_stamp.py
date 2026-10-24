"""PL7-a (#26): each store file carries its own schema stamp. Nothing refuses.

The stamp is a ``store_schema`` row, not ``store_meta``'s ``schema_version`` (that key is the
ticker-store card version, ``"3"``). A first ensure writes the code version when the row is
absent or older. A newer stamp is left alone, logged, and listed by ``store_schema_mismatch()``.
An existing file with no stamp is stamped current — an already-wrong file would be marked right;
``schema_drift()`` is the backstop for a column or key the MANIFEST no longer matches.

Refusal of writes is PL7-b (``test_store_schema_refuse.py``) and is off unless its switch is on.
Temp stores only. No ``_SCHEMA_VERSION`` bump.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore


@pytest.fixture(autouse=True)
def _fresh_stamp_counters():
    rawstore._STAMP_WRITES.clear()
    rawstore._STAMP_WARNED.clear()
    yield
    rawstore._STAMP_WRITES.clear()
    rawstore._STAMP_WARNED.clear()


def _set_stamp(db: str, version: str) -> None:
    with rawstore.connect(db=db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS store_schema (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO store_schema (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [version])


def _canary(db: str) -> str | None:
    rows = rawstore.query(
        "SELECT value FROM store_schema WHERE key = 'canary'", db=db)
    return rows[0]["value"] if rows else None


class TestStampOnEnsure:
    def test_store_schema_is_a_manifest_table_and_the_version_is_not_bumped(self):
        spec = rawstore.MANIFEST["store_schema"]
        assert spec["key"] == ["key"]
        assert any(c.startswith("value ") for c in spec["columns"])
        assert rawstore.schema_version() == "12"

    def test_ensure_stamps_each_file_that_ensure_opens_and_does_not_create_history(self, isolate_rawstore):
        rawstore.ensure_all_tables()
        code = rawstore.schema_version()
        for db in ("raw", "edgar", "prices"):
            assert rawstore.schema_stamp(db) == code, db
        assert not rawstore.history_db_path().exists()
        assert rawstore.schema_stamp("history") is None

    def test_ensure_history_stamps_the_history_file(self, isolate_rawstore):
        rawstore.ensure_all_tables()
        rawstore.ensure_history()
        assert rawstore.history_db_path().is_file()
        assert rawstore.schema_stamp("history") == rawstore.schema_version()

    def test_an_existing_unstamped_file_is_stamped_with_the_current_version(self, isolate_rawstore):
        """The documented assumption: a file from before the stamp is marked current, not refused."""
        with rawstore.connect(db="edgar") as conn:
            conn.execute("CREATE TABLE edgar_documents (accession TEXT)")
        assert rawstore.schema_stamp("edgar") is None
        rawstore.ensure_all_tables()
        assert rawstore.schema_stamp("edgar") == "12"

    def test_an_older_stamp_is_upgraded_and_a_text_sorted_9_is_still_older(self, isolate_rawstore):
        rawstore.ensure_all_tables()
        _set_stamp("prices", "11")
        _set_stamp("edgar", "9")            # "9" > "12" as text; 9 < 12 as a version
        rawstore._TABLE_READY.clear()
        rawstore.ensure_all_tables()
        assert rawstore.schema_stamp("prices") == "12"
        assert rawstore.schema_stamp("edgar") == "12"

    def test_an_upgrade_keeps_the_old_value_and_warns(self, isolate_rawstore, caplog):
        """A restored older file leaves a trace after the ensure upgrades it (review SHOULD-3)."""
        rawstore.ensure_all_tables()
        _set_stamp("prices", "11")
        rawstore._TABLE_READY.clear()
        with caplog.at_level("WARNING"):
            rawstore.ensure_all_tables()
        rows = {r["key"]: r["value"] for r in rawstore.query("SELECT key, value FROM store_schema", db="prices")}
        assert rows["schema_version"] == "12" and rows["upgraded_from"] == "11" and rows["upgraded_at"]
        assert any("upgraded" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)

    def test_a_current_stamp_is_not_rewritten(self, isolate_rawstore):
        rawstore.ensure_all_tables()
        before = dict(rawstore._STAMP_WRITES)
        assert set(before) == {"raw", "edgar", "prices"}
        assert all(n == 1 for n in before.values())
        with rawstore.connect() as conn:
            conn.execute("INSERT INTO store_schema (key, value) VALUES ('canary', 'keep')")
        rawstore._TABLE_READY.clear()
        rawstore.ensure_all_tables()
        assert rawstore._STAMP_WRITES == before
        assert _canary("raw") == "keep"

    def test_the_file_stamp_is_not_the_ticker_store_schema_version(self, isolate_rawstore):
        from RevenueChaser.core import ticker_store
        rawstore.ensure_all_tables()
        ticker_store._set_meta("schema_version", ticker_store.SCHEMA_VERSION)
        assert rawstore.schema_stamp("raw") == "12"
        assert rawstore.get("store_meta", "schema_version")["value"] == ticker_store.SCHEMA_VERSION


class TestMismatchReport:
    def test_a_newer_stamp_is_reported_logged_and_not_overwritten(self, isolate_rawstore, caplog):
        rawstore.ensure_all_tables()
        _set_stamp("edgar", "13")
        _set_stamp("prices", "11")
        assert rawstore.store_schema_mismatch() == [
            {"db": "edgar", "file": "edgar.duckdb", "stored": "13", "code": "12", "kind": "newer"},
            {"db": "prices", "file": "prices.duckdb", "stored": "11", "code": "12", "kind": "older"},
        ]
        rawstore._TABLE_READY.clear()
        with caplog.at_level("WARNING"):
            rawstore.ensure_all_tables()
        assert rawstore.schema_stamp("edgar") == "13"
        assert rawstore.schema_stamp("prices") == "12"
        assert rawstore.store_schema_mismatch() == [
            {"db": "edgar", "file": "edgar.duckdb", "stored": "13", "code": "12", "kind": "newer"}]
        msgs = [r.getMessage() for r in caplog.records if "schema stamp mismatch" in r.getMessage()]
        assert len(msgs) == 1
        assert "edgar.duckdb" in msgs[0] and "13" in msgs[0] and "12" in msgs[0]
        assert str(rawstore.edgar_db_path()) not in msgs[0]

    def test_the_report_is_a_copy_and_a_second_ensure_does_not_log_again(self, isolate_rawstore, caplog):
        rawstore.ensure_all_tables()
        _set_stamp("edgar", "13")
        with caplog.at_level("WARNING"):
            rawstore._TABLE_READY.clear()
            rawstore.ensure_all_tables()
            got = rawstore.store_schema_mismatch()
            got[0]["stored"] = "tampered"
            got.clear()
            rawstore._TABLE_READY.clear()
            rawstore.ensure_all_tables()
        assert rawstore.store_schema_mismatch() == [
            {"db": "edgar", "file": "edgar.duckdb", "stored": "13", "code": "12", "kind": "newer"}]
        assert len([r for r in caplog.records if "schema stamp" in r.getMessage()]) == 1

    def test_a_read_of_a_mismatched_stamp_warns_and_does_not_raise(self, isolate_rawstore, caplog):
        rawstore.ensure_all_tables()
        _set_stamp("raw", "13")
        with caplog.at_level("WARNING"):
            assert rawstore.get("watchlist", "ZZZ") is None
        assert any("schema stamp" in r.getMessage() and "raw.duckdb" in r.getMessage()
                   and "13" in r.getMessage() for r in caplog.records)

    def test_a_missing_file_is_not_a_mismatch(self, isolate_rawstore):
        rawstore.ensure_all_tables()
        assert rawstore.store_schema_mismatch() == []
