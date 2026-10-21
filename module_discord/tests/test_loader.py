"""#72 S1: DCE JSON export -> typed rows -> discord.duckdb. Fixture only, no network."""
from __future__ import annotations

import copy

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_discord import loader, store

from .conftest import FIXTURE

CHANNEL = "900000000000000010"


def test_parse_channel_row(export_data):
    p = loader.parse_export(export_data)
    assert p.channel == {
        "channel_id": CHANNEL, "guild_id": "900000000000000001",
        "guild_name": "Example Trading Server", "name": "swing-setups",
        "category": "Ideas", "topic": "Chart ideas and trade logs",
        "kind": "GuildTextChat",
    }


def test_parse_message_rows_in_export_order(export_data):
    p = loader.parse_export(export_data)
    assert [m["message_id"] for m in p.messages] == [
        "900000000000001001", "900000000000001002", "900000000000001003",
        "900000000000001004", "900000000000001005", "900000000000001006"]
    m = p.messages[0]
    assert m["channel_id"] == CHANNEL
    assert m["author_id"] == "900000000000000101"
    assert m["author_name"] == "Alpha"          # nickname wins over the account name
    assert m["is_bot"] is False
    assert m["kind"] == "Default"
    assert m["content"] == "Watching $NVDA for a reclaim of 120. Stop below 114."
    assert m["is_pinned"] is False
    assert m["reply_to_id"] is None
    assert m["edited_ts"] is None


def test_timestamps_are_normalised_to_sortable_utc_text(export_data):
    p = loader.parse_export(export_data)
    assert p.messages[0]["ts"] == "2026-09-30T13:31:05.123000Z"
    assert p.messages[1]["ts"] == "2026-09-30T13:40:11.000000Z"      # no fraction in source
    assert p.messages[1]["edited_ts"] == "2026-09-30T13:41:00.500000Z"
    ts = [m["ts"] for m in p.messages]
    assert ts == sorted(ts)


def test_non_utc_offsets_are_converted(export_data):
    data = copy.deepcopy(export_data)
    data["messages"][0]["timestamp"] = "2026-09-30T15:31:05.123+02:00"
    assert loader.parse_export(data).messages[0]["ts"] == "2026-09-30T13:31:05.123000Z"


def test_reply_pin_reactions_attachments_mentions_embeds(export_data):
    by_id = {m["message_id"]: m for m in loader.parse_export(export_data).messages}
    reply = by_id["900000000000001002"]
    assert reply["kind"] == "Reply" and reply["is_pinned"] is True
    assert reply["reply_to_id"] == "900000000000001001"
    assert reply["reactions"] == [{"emoji": "\U0001f44d", "count": 3}]
    assert reply["attachments"] == [{
        "id": "900000000000002001", "file_name": "chart.png", "size_bytes": 20480,
        "path": "/data/discord_media/900000000000000010/chart-AB12.png"}]
    assert reply["mentions"] == [{"id": "900000000000000101", "name": "Alpha"}]
    bot = by_id["900000000000001004"]
    assert bot["is_bot"] is True
    assert bot["embeds"] == [{
        "title": "Daily recap", "url": "https://news.example.invalid/recap",
        "description": "SPX closed +0.4%", "color": "#3498DB",
        "author": "Recap Bot", "footer": "Data: example feed",
        "fields": [{"name": "SPX", "value": "+0.4%", "inline": True},
                   {"name": "VIX", "value": "-3.1%", "inline": True}],
        "images": ["https://cdn.example.invalid/chart.png"],
        "thumbnail": "https://cdn.example.invalid/t.png"}]


def test_only_real_replies_set_reply_to_id(export_data):
    by_id = {m["message_id"]: m for m in loader.parse_export(export_data).messages}
    reply, pin, fwd = by_id["900000000000001002"], by_id["900000000000001005"], by_id["900000000000001006"]
    assert (reply["reply_to_id"], reply["reference_id"], reply["reference_kind"]) == (
        "900000000000001001", "900000000000001001", "Default")
    assert pin["reply_to_id"] is None
    assert (pin["reference_id"], pin["reference_kind"]) == ("900000000000001002", "Default")
    assert fwd["reply_to_id"] is None
    assert (fwd["reference_id"], fwd["reference_kind"]) == ("900000000000000777", "Forward")
    assert by_id["900000000000001001"]["reference_id"] is None


def test_a_forwarded_message_keeps_its_text(export_data):
    """The forwarded text exists only in forwardedMessage.content; it must not be lost."""
    fwd = {m["message_id"]: m for m in loader.parse_export(export_data).messages}["900000000000001006"]
    assert fwd["content"] == "Original text that lives only here"
    assert fwd["raw"]["forwardedMessage"]["author"]["name"] == "user_delta"


def test_a_message_with_its_own_text_is_not_replaced_by_the_forwarded_text(export_data):
    data = copy.deepcopy(export_data)
    data["messages"][5]["content"] = "my comment"
    assert loader.parse_export(data).messages[5]["content"] == "my comment"


def test_raw_holds_the_full_dce_message_object(export_data):
    p = loader.parse_export(export_data)
    for parsed, original in zip(p.messages, export_data["messages"]):
        assert parsed["raw"] == original
    bot = p.messages[3]["raw"]
    assert bot["embeds"][0]["fields"][1] == {"name": "VIX", "value": "-3.1%", "isInline": True}


@pytest.mark.parametrize("field,value", [("id", "abc"), ("id", "12 34"), ("id", "")])
def test_message_ids_must_be_digits(export_data, field, value):
    data = copy.deepcopy(export_data)
    data["messages"][1][field] = value
    with pytest.raises(loader.ExportFormatError):
        loader.parse_export(data)


def test_channel_id_must_be_digits(export_data):
    data = copy.deepcopy(export_data)
    data["channel"]["id"] = "general"
    with pytest.raises(loader.ExportFormatError, match="channel"):
        loader.parse_export(data)


@pytest.mark.parametrize("bad", [None, "x", {"a": 1}])
def test_messages_must_be_a_list(export_data, bad):
    data = copy.deepcopy(export_data)
    data["messages"] = bad
    with pytest.raises(loader.ExportFormatError, match="messages"):
        loader.parse_export(data)


def test_a_missing_export_file_is_an_export_format_error(tmp_path):
    with pytest.raises(loader.ExportFormatError, match="cannot read"):
        loader.load_export_file(tmp_path / "nope.json")


def test_a_non_utf8_export_file_is_an_export_format_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(loader.ExportFormatError):
        loader.load_export_file(bad)


def test_message_text_is_kept_verbatim_not_escaped(export_data):
    """The store holds the third-party text as-is; escaping is the report's job (S2 / #144)."""
    by_id = {m["message_id"]: m for m in loader.parse_export(export_data).messages}
    assert by_id["900000000000001003"]["content"].startswith("<script>alert('x')</script> &amp;")


def test_a_message_without_an_id_is_rejected(export_data):
    data = copy.deepcopy(export_data)
    del data["messages"][2]["id"]
    with pytest.raises(loader.ExportFormatError, match="message 2"):
        loader.parse_export(data)


@pytest.mark.parametrize("missing", ["channel", "messages"])
def test_a_document_without_channel_or_messages_is_rejected(export_data, missing):
    data = copy.deepcopy(export_data)
    del data[missing]
    with pytest.raises(loader.ExportFormatError, match=missing):
        loader.parse_export(data)


def test_load_export_file_reads_the_fixture():
    p = loader.load_export_file(FIXTURE)
    assert p.channel["channel_id"] == CHANNEL and len(p.messages) == 6


def test_load_export_file_rejects_invalid_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(loader.ExportFormatError, match="not valid JSON"):
        loader.load_export_file(bad)


# ── store ────────────────────────────────────────────────────────────────────

def test_save_export_writes_channel_and_messages(rawstore_tmp_db):
    p = loader.load_export_file(FIXTURE)
    res = store.save_export(p)
    assert res == {"channel_id": CHANNEL, "messages": 6, "new": 6}
    assert rawstore.count("discord_messages") == 6
    assert rawstore.count("discord_channels") == 1


def test_save_export_is_idempotent_and_reports_only_new_rows(rawstore_tmp_db):
    p = loader.load_export_file(FIXTURE)
    store.save_export(p)
    again = store.save_export(p)
    assert again == {"channel_id": CHANNEL, "messages": 6, "new": 0}
    assert rawstore.count("discord_messages") == 6


def test_an_edited_message_overwrites_the_stored_row(rawstore_tmp_db, export_data):
    store.save_export(loader.parse_export(export_data))
    data = copy.deepcopy(export_data)
    data["messages"][0]["content"] = "edited text"
    data["messages"][0]["timestampEdited"] = "2026-10-02T07:00:00.000+00:00"
    store.save_export(loader.parse_export(data))
    got = store.messages(CHANNEL)[0]
    assert got["content"] == "edited text"
    assert got["edited_ts"] == "2026-10-02T07:00:00.000000Z"
    assert rawstore.count("discord_messages") == 6


def test_messages_accessor_returns_json_columns_as_objects(rawstore_tmp_db):
    store.save_export(loader.load_export_file(FIXTURE))
    by_id = {m["message_id"]: m for m in store.messages(CHANNEL)}
    assert by_id["900000000000001002"]["reactions"] == [{"emoji": "\U0001f44d", "count": 3}]
    assert by_id["900000000000001001"]["attachments"] == []


def test_messages_come_back_oldest_first(rawstore_tmp_db):
    store.save_export(loader.load_export_file(FIXTURE))
    ts = [m["ts"] for m in store.messages(CHANNEL)]
    assert ts == sorted(ts) and len(ts) == 6


@pytest.mark.parametrize("text", ['{"a": 1}', "[1, 2, 3]", "123", "null", "true"])
def test_content_that_looks_like_json_round_trips_as_text(rawstore_tmp_db, export_data, text):
    """rawstore.get_all would parse a TEXT cell holding a JSON object/array; the accessor must not."""
    data = copy.deepcopy(export_data)
    data["messages"][0]["content"] = text
    store.save_export(loader.parse_export(data))
    assert store.messages(CHANNEL)[0]["content"] == text


def test_last_message_id_is_the_newest_stored_message(rawstore_tmp_db):
    assert store.last_message_id(CHANNEL) is None
    store.save_export(loader.load_export_file(FIXTURE))
    assert store.last_message_id(CHANNEL) == "900000000000001006"


def test_last_message_id_orders_by_time_not_by_digit_string(rawstore_tmp_db, export_data):
    """Snowflakes grow in digits over the years; compare numerically, not as text."""
    data = copy.deepcopy(export_data)
    data["messages"][0]["id"] = "99999999999999999"          # 17 digits, older
    data["messages"][1]["id"] = "1000000000000000000"         # 19 digits, newer
    data["messages"] = data["messages"][:2]
    store.save_export(loader.parse_export(data))
    assert store.last_message_id(CHANNEL) == "1000000000000000000"


# ── channels ────────────────────────────────────────────────────────────────

def test_discovered_channels_start_disabled_and_keep_their_flag_on_rediscovery(rawstore_tmp_db):
    store.upsert_discovered([{"channel_id": "1", "guild_id": "g", "name": "general", "category": "Chat"},
                             {"channel_id": "2", "guild_id": "g", "name": "ideas", "category": "Ideas"}])
    assert [c["channel_id"] for c in store.channels(enabled_only=True)] == []
    store.set_enabled(["2"], True)
    store.upsert_discovered([{"channel_id": "2", "guild_id": "g", "name": "ideas-renamed", "category": "Ideas"},
                             {"channel_id": "3", "guild_id": "g", "name": "new", "category": "Chat"}])
    enabled = store.channels(enabled_only=True)
    assert [(c["channel_id"], c["name"]) for c in enabled] == [("2", "ideas-renamed")]
    assert {c["channel_id"] for c in store.channels()} == {"1", "2", "3"}


def test_save_export_keeps_the_enabled_flag(rawstore_tmp_db):
    store.upsert_discovered([{"channel_id": CHANNEL, "guild_id": "g", "name": "x", "category": "y"}])
    store.set_enabled([CHANNEL], True)
    store.save_export(loader.load_export_file(FIXTURE))
    ch = store.channels()[0]
    assert ch["enabled"] is True and ch["name"] == "swing-setups"


def test_set_enabled_rejects_an_unknown_channel(rawstore_tmp_db):
    with pytest.raises(store.DiscordStoreError, match="unknown channel"):
        store.set_enabled(["404"], True)


def test_raw_message_round_trips_through_the_store(rawstore_tmp_db, export_data):
    store.save_export(loader.parse_export(export_data))
    raw = store.raw_message("900000000000001004")
    assert raw == export_data["messages"][3]
    assert store.raw_message("404") is None


def test_the_listing_accessor_does_not_ship_the_raw_blob(rawstore_tmp_db):
    store.save_export(loader.load_export_file(FIXTURE))
    assert "raw" not in store.messages(CHANNEL)[0]


def test_reference_columns_are_stored(rawstore_tmp_db):
    store.save_export(loader.load_export_file(FIXTURE))
    by_id = {m["message_id"]: m for m in store.messages(CHANNEL)}
    assert by_id["900000000000001006"]["reference_kind"] == "Forward"
    assert by_id["900000000000001006"]["reply_to_id"] is None
    assert by_id["900000000000001002"]["reply_to_id"] == "900000000000001001"
