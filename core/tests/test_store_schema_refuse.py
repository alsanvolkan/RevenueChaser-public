"""PL7-b (#26): refuse data writes when a file's schema stamp is newer than this code, or missing.

Off unless ``RC_STORE_SCHEMA_REFUSE=1`` (``SCHEMA_REFUSE_WRITES`` is False). Readers warn, they do
not raise. ``RC_STORE_SCHEMA_OK=1`` lets a deliberate migration through and suppresses the warning.
An older stamp is not a refusal: the ensure path upgrades it (PL7-a). Temp stores only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from RevenueChaser.core import rawstore


def _set_stamp(db: str, version: str) -> None:
    with rawstore.connect(db=db) as conn:
        conn.execute(
            "INSERT INTO store_schema (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [version])


def _refuse():
    from RevenueChaser.core import store_schema_refuse
    return store_schema_refuse


class TestSwitch:
    def test_the_switch_is_named_off_by_default_and_documented(self, monkeypatch):
        monkeypatch.delenv("RC_STORE_SCHEMA_REFUSE", raising=False)
        monkeypatch.delenv("RC_STORE_SCHEMA_OK", raising=False)
        mod = _refuse()
        assert mod.SCHEMA_REFUSE_WRITES is False
        assert mod.schema_refuse_writes() is False
        text = (Path(__file__).resolve().parents[2] / "docs" / "data_architecture.md").read_text(encoding="utf-8")
        assert "RC_STORE_SCHEMA_REFUSE" in text
        assert "RC_STORE_SCHEMA_OK" in text
        assert "SCHEMA_REFUSE_WRITES" in text
        assert "store_schema_refuse.py" in text

    def test_the_env_turns_refusal_on_and_the_escape_turns_it_off(self, monkeypatch):
        mod = _refuse()
        monkeypatch.setenv("RC_STORE_SCHEMA_REFUSE", "1")
        monkeypatch.delenv("RC_STORE_SCHEMA_OK", raising=False)
        assert mod.schema_refuse_writes() is True
        monkeypatch.setenv("RC_STORE_SCHEMA_OK", "1")
        assert mod.schema_refuse_writes() is False


class TestRefuseWrites:
    def test_refusal_off_lets_a_newer_stamp_be_written(self, isolate_rawstore, monkeypatch):
        monkeypatch.delenv("RC_STORE_SCHEMA_REFUSE", raising=False)
        rawstore.ensure_all_tables()
        _set_stamp("raw", "13")
        rawstore.write_rows("watchlist", [{"ticker": "AAA"}])
        assert rawstore.count("watchlist") == 1

    def test_a_newer_stamp_refuses_the_write_and_names_the_restore(self, isolate_rawstore, monkeypatch):
        monkeypatch.setenv("RC_STORE_SCHEMA_REFUSE", "1")
        rawstore.ensure_all_tables()
        _set_stamp("raw", "13")
        with pytest.raises(rawstore.StoreSchemaMismatch) as ei:
            rawstore.write_rows("watchlist", [{"ticker": "AAA"}])
        msg = str(ei.value)
        assert "raw.duckdb" in msg and "13" in msg and "12" in msg
        assert "python -m RevenueChaser.scripts.restore_store --latest raw" in msg
        assert str(rawstore.DB_PATH) not in msg
        assert rawstore.count("watchlist") == 0

    def test_a_read_warns_and_does_not_raise_when_refusal_is_on(self, isolate_rawstore, monkeypatch, caplog):
        monkeypatch.setenv("RC_STORE_SCHEMA_REFUSE", "1")
        rawstore.ensure_all_tables()
        _set_stamp("raw", "13")
        with caplog.at_level("WARNING"):
            assert rawstore.get("watchlist", "ZZZ") is None
        assert any("schema stamp" in r.getMessage() and "13" in r.getMessage() for r in caplog.records)

    def test_the_escape_lets_a_write_and_a_read_through(self, isolate_rawstore, monkeypatch, caplog):
        monkeypatch.setenv("RC_STORE_SCHEMA_REFUSE", "1")
        monkeypatch.setenv("RC_STORE_SCHEMA_OK", "1")
        rawstore.ensure_all_tables()
        _set_stamp("raw", "13")
        rawstore.write_rows("watchlist", [{"ticker": "AAA"}])
        with caplog.at_level("WARNING"):
            assert rawstore.get("watchlist", "AAA")["ticker"] == "AAA"
        assert [r for r in caplog.records if "schema stamp" in r.getMessage()] == []

    def test_a_missing_stamp_on_an_existing_file_refuses_when_the_switch_is_on(self, isolate_rawstore, monkeypatch):
        with rawstore.connect() as conn:
            conn.execute("CREATE TABLE watchlist (ticker TEXT PRIMARY KEY)")
        monkeypatch.setenv("RC_STORE_SCHEMA_REFUSE", "1")
        with pytest.raises(rawstore.StoreSchemaMismatch, match="no schema stamp"):
            rawstore.write_rows("watchlist", [{"ticker": "AAA"}])
        assert rawstore.count("watchlist") == 0

    def test_a_fresh_store_stamps_and_writes_when_refusal_is_on(self, isolate_rawstore, monkeypatch):
        monkeypatch.setenv("RC_STORE_SCHEMA_REFUSE", "1")
        assert _refuse().schema_refuse_writes() is True
        rawstore.ensure_all_tables()
        rawstore.write_rows("watchlist", [{"ticker": "AAA"}])
        assert rawstore.count("watchlist") == 1
        assert rawstore.schema_stamp("raw") == "12"

    def test_an_older_stamp_is_not_a_refusal(self, isolate_rawstore, monkeypatch):
        """Supervisor decision: refuse newer or missing, not older. Ensure upgrades older."""
        monkeypatch.setenv("RC_STORE_SCHEMA_REFUSE", "1")
        rawstore.ensure_all_tables()
        _set_stamp("raw", "11")
        rawstore.write_rows("watchlist", [{"ticker": "AAA"}])
        assert rawstore.count("watchlist") == 1
        assert rawstore.schema_stamp("raw") == "11"
        rawstore._TABLE_READY.clear()
        rawstore.ensure_all_tables()
        assert rawstore.schema_stamp("raw") == "12"

    def test_a_newer_stamp_refuses_only_that_file(self, isolate_rawstore, monkeypatch):
        monkeypatch.setenv("RC_STORE_SCHEMA_REFUSE", "1")
        rawstore.ensure_all_tables()
        _set_stamp("edgar", "13")
        rawstore.write_rows("watchlist", [{"ticker": "AAA"}])
        with pytest.raises(rawstore.StoreSchemaMismatch) as ei:
            rawstore.execute("CREATE TABLE t_probe (id INTEGER)", db="edgar")
        msg = str(ei.value)
        assert "edgar.duckdb" in msg and "13" in msg
        assert "python -m RevenueChaser.scripts.restore_store --latest edgar" in msg
        assert str(rawstore.edgar_db_path()) not in msg
        with pytest.raises(rawstore.StoreSchemaMismatch):
            with rawstore.transaction(db="edgar"):
                pass
