"""#72 S1: the Discord tables live in their own ``discord.duckdb``.

Routing, ensure step and file layout only; the loader, exporter and engine have their
own tests under ``module_discord/tests``.
"""
from __future__ import annotations

from pathlib import Path

from RevenueChaser.core import config, rawstore

DISCORD_TABLES = ("discord_channels", "discord_messages", "discord_export_runs")


def _tables(db: str) -> set[str]:
    with rawstore.connect(read_only=True, db=db) as conn:
        return {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'").fetchall()}


def test_discord_db_path_is_a_sibling_of_the_raw_store(isolate_rawstore):
    assert rawstore.discord_db_path() == Path(rawstore.DB_PATH).parent / "discord.duckdb"
    assert rawstore._db_path("discord") == rawstore.discord_db_path()


def test_discord_tables_declare_the_discord_file():
    for t in DISCORD_TABLES:
        assert rawstore.MANIFEST[t].get("db") == "discord", t
        assert rawstore._db_of(t) == "discord"


def test_ensure_all_tables_creates_the_discord_file_with_only_its_tables(rawstore_tmp_db):
    assert rawstore.discord_db_path().is_file()
    assert set(DISCORD_TABLES) <= _tables("discord")
    assert not (_tables("discord") & {"edgar_documents", "watchlist"})


def test_discord_tables_are_not_created_in_raw_or_edgar(rawstore_tmp_db):
    assert not (set(DISCORD_TABLES) & _tables("raw"))
    assert not (set(DISCORD_TABLES) & _tables("edgar"))


def test_rows_written_through_the_manifest_land_in_the_discord_file(rawstore_tmp_db):
    rawstore.write_rows("discord_channels", [{"channel_id": "1", "name": "general"}])
    assert rawstore.count("discord_channels") == 1
    rows = rawstore.query("SELECT channel_id FROM discord_channels", db="discord")
    assert [r["channel_id"] for r in rows] == ["1"]


def test_discord_tables_did_not_bump_the_schema_version_themselves():
    # The tables live in a new file and shipped without a bump (#72); the pin follows the current version (12, the 13F tables of #208).
    assert rawstore.schema_version() == "12"


def test_reset_db_also_resets_the_discord_file(rawstore_tmp_db):
    rawstore.write_rows("discord_channels", [{"channel_id": "1", "name": "general"}])
    rawstore.reset_db()
    assert rawstore.count("discord_channels") == 0


def test_reset_db_without_edgar_leaves_the_discord_file_alone(rawstore_tmp_db):
    """The migration's --reset passes include_edgar=False; it must not touch live sidecars."""
    rawstore.write_rows("discord_channels", [{"channel_id": "1", "name": "general"}])
    rawstore.reset_db(include_edgar=False)
    assert rawstore.count("discord_channels") == 1


def test_media_dir_is_a_separate_durable_tree():
    assert config.DISCORD_MEDIA_DIR.name == "discord_media"
    assert config.DATA_DIR not in config.DISCORD_MEDIA_DIR.parents
    assert config.OUTPUT_DIR not in config.DISCORD_MEDIA_DIR.parents


# ── text columns hold third-party text verbatim ──────────────────────────────

import pytest  # noqa: E402


@pytest.mark.parametrize("text", ['{"a": 1}', "[1, 2, 3]", "[]", "{}", "123", "null", "true", '"quoted"'])
def test_get_returns_json_looking_text_columns_as_text(rawstore_tmp_db, text):
    rawstore.write_rows("discord_messages", [
        {"message_id": "1", "channel_id": "9", "content": text, "author_name": text}])
    row = rawstore.get("discord_messages", "1")
    assert row["content"] == text and row["author_name"] == text
    assert rawstore.get_all("discord_messages")[0]["content"] == text


def test_channel_name_and_topic_that_look_like_json_stay_text(rawstore_tmp_db):
    rawstore.write_rows("discord_channels", [
        {"channel_id": "9", "name": "[]", "topic": '{"x": 1}'}])
    row = rawstore.get("discord_channels", "9")
    assert row["name"] == "[]" and row["topic"] == '{"x": 1}'


def test_json_columns_of_discord_tables_are_still_decoded(rawstore_tmp_db):
    rawstore.write_rows("discord_messages", [
        {"message_id": "1", "channel_id": "9", "reactions": [{"emoji": "x", "count": 1}]}])
    assert rawstore.get("discord_messages", "1")["reactions"] == [{"emoji": "x", "count": 1}]


def test_other_tables_keep_the_legacy_text_reparse(rawstore_tmp_db):
    """The verbatim flag is opt-in: a legacy TEXT blob in another table is still parsed."""
    assert not rawstore.MANIFEST["watchlist"].get("text_verbatim")
    assert all(rawstore.MANIFEST[t].get("text_verbatim") for t in DISCORD_TABLES)
    rawstore.write_rows("equity_universe", [{"ticker": "ZZZZ", "name": "[1, 2]"}])
    assert rawstore.get("equity_universe", "ZZZZ")["name"] == [1, 2]
