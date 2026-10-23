"""PL7-b (#26): refuse data writes when a file's schema stamp is ahead of this code (newer, or unreadable).

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

    def test_a_missing_stamp_is_stamped_by_the_ensure_not_refused(self, isolate_rawstore, monkeypatch):
        """Review MUST-2: with the real config (auto-ensure on) an unstamped file is stamped current, so the
        refusal covers a stamp AHEAD of the code only; the docs say so."""
        monkeypatch.setenv("RC_STORE_AUTO_ENSURE", "1")
        monkeypatch.setattr(rawstore, "_ENSURED", {})
        monkeypatch.setenv("RC_STORE_SCHEMA_REFUSE", "1")
        with rawstore.connect() as conn:
            conn.execute("CREATE TABLE watchlist (ticker TEXT PRIMARY KEY)")
        rawstore.write_rows("watchlist", [{"ticker": "AAA"}])
        assert rawstore.count("watchlist") == 1
        assert rawstore.schema_stamp("raw") == "12"

    def test_a_newer_file_is_not_reshaped_by_the_ensure_before_the_refusal(self, isolate_rawstore, monkeypatch):
        """Review MUST-1, real config: with auto-ensure ON the old checkout used to rename/merge/ALTER the newer
        file and only then refuse the rows. The stamp is checked before the first migration."""
        monkeypatch.setenv("RC_STORE_AUTO_ENSURE", "1")
        monkeypatch.setattr(rawstore, "_ENSURED", {})
        monkeypatch.setenv("RC_STORE_SCHEMA_REFUSE", "1")
        rawstore.ensure_all_tables()
        with rawstore.connect() as conn:
            conn.execute("CREATE TABLE etf_holdings_cache (ticker TEXT)")   # a legacy name _RENAMES would move
        _set_stamp("raw", "13")
        monkeypatch.setattr(rawstore, "_ENSURED", {})
        with pytest.raises(rawstore.StoreSchemaMismatch):
            rawstore.ensure_all_tables()
        with pytest.raises(rawstore.StoreSchemaMismatch):
            rawstore.write_rows("watchlist", [{"ticker": "AAA"}])
        with rawstore.connect() as conn:
            names = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        assert "etf_holdings_cache" in names           # the rename was not applied
        assert rawstore.schema_stamp("raw") == "13"

    def test_the_other_files_are_checked_too_and_an_unreadable_stamp_is_named(self, isolate_rawstore, monkeypatch):
        monkeypatch.setenv("RC_STORE_SCHEMA_REFUSE", "1")
        rawstore.ensure_all_tables()
        _set_stamp("edgar", "12-dev")
        with pytest.raises(rawstore.StoreSchemaMismatch, match="not a version number"):
            rawstore.ensure_all_tables()
        assert rawstore.store_schema_mismatch()[0]["kind"] == "unreadable"

    def test_the_stamp_is_not_re_read_for_a_steady_file(self, isolate_rawstore, monkeypatch):
        monkeypatch.setenv("RC_STORE_SCHEMA_REFUSE", "1")
        rawstore.ensure_all_tables()
        rawstore.write_rows("watchlist", [{"ticker": "AAA"}])
        calls = []
        real = rawstore.schema_stamp
        monkeypatch.setattr(rawstore, "schema_stamp", lambda db: calls.append(db) or real(db))
        rawstore._STAMP_SEEN.clear()
        for _ in range(5):
            rawstore._cached_stamp("raw")
        assert calls == ["raw"]

    def test_a_fresh_store_stamps_and_writes_when_refusal_is_on(self, isolate_rawstore, monkeypatch):
        monkeypatch.setenv("RC_STORE_SCHEMA_REFUSE", "1")
        assert _refuse().schema_refuse_writes() is True
        rawstore.ensure_all_tables()
        rawstore.write_rows("watchlist", [{"ticker": "AAA"}])
        assert rawstore.count("watchlist") == 1
        assert rawstore.schema_stamp("raw") == "12"

    def test_an_older_stamp_is_not_a_refusal(self, isolate_rawstore, monkeypatch):
        """Refuse ahead (newer or unreadable), not older or missing. Ensure upgrades older."""
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
