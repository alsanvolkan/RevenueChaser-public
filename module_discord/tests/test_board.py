"""#74 S2: the board's data layer (feed paging, filters, replies, media paths). Fixtures only."""
from __future__ import annotations

import copy
import socket
import subprocess

import pytest

from RevenueChaser.core import config
from RevenueChaser.module_discord import board, exporter, loader, store

from .conftest import FIXTURE

CHANNEL = "900000000000000010"


@pytest.fixture
def seeded(rawstore_tmp_db):
    store.save_export(loader.load_export_file(FIXTURE))
    return CHANNEL


def _bulk(export_data, n, channel="900000000000000011", name="bulk"):
    data = copy.deepcopy(export_data)
    data["channel"].update(id=channel, name=name)
    base = data["messages"][0]
    data["messages"] = []
    for i in range(n):
        m = copy.deepcopy(base)
        m["id"] = str(900000000001000000 + i)
        m["timestamp"] = f"2026-09-{1 + i // 100:02d}T{(i % 100) // 5:02d}:{(i % 5) * 10:02d}:00.000+00:00"
        m["content"] = f"bulk message {i}"
        data["messages"].append(m)
    return loader.parse_export(data)


def test_payload_lists_channels_with_counts_and_follow_flag(seeded):
    store.set_enabled([CHANNEL], True)
    p = board.build_payload()
    [ch] = p["channels"]
    assert ch["channel_id"] == CHANNEL and ch["name"] == "swing-setups"
    assert ch["guild_name"] == "Example Trading Server" and ch["category"] == "Ideas"
    assert ch["enabled"] is True and ch["message_count"] == 6
    assert ch["last_ts"] == "2026-10-01T11:00:00.000000Z"


def test_feed_is_newest_first_with_a_total(seeded):
    p = board.build_payload()
    ids = [m["message_id"] for m in p["messages"]]
    assert ids == sorted(ids, reverse=True) and p["total"] == 6 and p["pages"] == 1
    assert p["page"] == 1 and p["page_size"] == 200


def test_pages_hold_200_messages(rawstore_tmp_db, export_data):
    store.save_export(_bulk(export_data, 450))
    p1 = board.build_payload()
    p2 = board.build_payload(page=2)
    p3 = board.build_payload(page=3)
    assert (p1["total"], p1["pages"]) == (450, 3)
    assert [len(p["messages"]) for p in (p1, p2, p3)] == [200, 200, 50]
    seen = {m["message_id"] for p in (p1, p2, p3) for m in p["messages"]}
    assert len(seen) == 450


def test_a_page_beyond_the_end_is_clamped(seeded):
    assert board.build_payload(page=99)["page"] == 1
    assert board.build_payload(page=0)["page"] == 1
    assert board.build_payload(page="x")["page"] == 1


def test_channel_filter(seeded, export_data):
    store.save_export(_bulk(export_data, 3))
    only = board.build_payload(channel="900000000000000011")
    assert only["total"] == 3 and {m["channel_id"] for m in only["messages"]} == {"900000000000000011"}
    assert {m["channel_name"] for m in only["messages"]} == {"bulk"}


def test_text_filter_is_case_insensitive_and_literal(seeded, export_data):
    data = copy.deepcopy(export_data)
    data["messages"][0]["content"] = "100% sure about NVDA_x"
    data["messages"][1]["content"] = "nothing here"
    store.save_export(loader.parse_export(data))
    assert board.build_payload(q="nvda")["total"] == 1
    assert board.build_payload(q="100%")["total"] == 1          # % is not a wildcard
    assert board.build_payload(q="NVDA_x")["total"] == 1
    assert board.build_payload(q="NVDAxx")["total"] == 0        # _ is not a wildcard either
    assert board.build_payload(q="%")["total"] == 1


def test_date_filter_is_inclusive_of_both_days(seeded):
    p = board.build_payload(date_from="2026-09-30", date_to="2026-09-30")
    assert {m["message_id"] for m in p["messages"]} == {"900000000000001001", "900000000000001002"}
    p = board.build_payload(date_from="2026-10-01")
    assert p["total"] == 4


@pytest.mark.parametrize("kw", [{"date_from": "yesterday"}, {"date_to": "2026-13-45"}])
def test_a_bad_date_is_rejected(seeded, kw):
    with pytest.raises(board.BoardInputError):
        board.build_payload(**kw)


def test_replies_carry_a_preview_of_the_parent(seeded):
    by_id = {m["message_id"]: m for m in board.build_payload()["messages"]}
    reply = by_id["900000000000001002"]
    assert reply["reply_preview"] == {
        "author": "Alpha", "text": "Watching $NVDA for a reclaim of 120. Stop below 114."}
    assert by_id["900000000000001005"]["reply_preview"] is None     # pin notice
    assert by_id["900000000000001006"]["reply_preview"] is None     # forward
    assert by_id["900000000000001001"]["reply_preview"] is None


def test_a_reply_to_a_message_outside_the_archive_has_no_preview(rawstore_tmp_db, export_data):
    data = copy.deepcopy(export_data)
    data["messages"][1]["reference"]["messageId"] = "900000000000009999"
    store.save_export(loader.parse_export(data))
    by_id = {m["message_id"]: m for m in board.build_payload()["messages"]}
    assert by_id["900000000000001002"]["reply_preview"] is None


def test_long_parent_text_is_cut_for_the_preview(rawstore_tmp_db, export_data):
    data = copy.deepcopy(export_data)
    data["messages"][0]["content"] = "x" * 500
    store.save_export(loader.parse_export(data))
    by_id = {m["message_id"]: m for m in board.build_payload()["messages"]}
    assert len(by_id["900000000000001002"]["reply_preview"]["text"]) <= 140


def test_media_path_under_the_media_dir_becomes_a_relative_url_path(seeded, monkeypatch, tmp_path):
    media = tmp_path / "dm"
    (media / CHANNEL).mkdir(parents=True)
    (media / CHANNEL / "chart-AB12.png").write_bytes(b"png")
    monkeypatch.setattr(config, "DISCORD_MEDIA_DIR", media)
    by_id = {m["message_id"]: m for m in board.build_payload()["messages"]}
    [att] = by_id["900000000000001002"]["attachments"]
    # the fixture's absolute path is /data/discord_media/...; only paths really under the media dir resolve
    assert att["media_rel"] is None and att["is_image"] is True
    inside = media / CHANNEL / "chart-AB12.png"
    assert board.media_rel(str(inside)) == f"{CHANNEL}/chart-AB12.png"


@pytest.mark.parametrize("make", [
    lambda media, outside: str(outside),                                    # absolute, elsewhere
    lambda media, outside: str(media / ".." / "outside.txt"),               # traversal
    lambda media, outside: str(media),                                      # the root itself
    lambda media, outside: "",                                              # empty
    lambda media, outside: None,
])
def test_media_rel_refuses_anything_outside_the_media_tree(monkeypatch, tmp_path, make):
    media = tmp_path / "dm"
    media.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    monkeypatch.setattr(config, "DISCORD_MEDIA_DIR", media)
    assert board.media_rel(make(media, outside)) is None


def test_media_rel_refuses_a_symlink_that_escapes(monkeypatch, tmp_path):
    media = tmp_path / "dm"
    media.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    try:
        (media / "link.png").symlink_to(outside)
    except OSError:
        pytest.skip("no symlinks here")
    monkeypatch.setattr(config, "DISCORD_MEDIA_DIR", media)
    assert board.media_rel(str(media / "link.png")) is None


def test_relative_media_paths_are_taken_relative_to_the_media_dir(monkeypatch, tmp_path):
    media = tmp_path / "dm"
    (media / "9").mkdir(parents=True)
    (media / "9" / "a.png").write_bytes(b"x")
    monkeypatch.setattr(config, "DISCORD_MEDIA_DIR", media)
    assert board.media_rel("9/a.png") == "9/a.png"
    assert board.media_rel("../a.png") is None


def test_only_images_are_flagged_as_thumbnails():
    assert board.is_image("a.PNG") and board.is_image("b.jpeg") and board.is_image("c.webp")
    assert not board.is_image("d.pdf") and not board.is_image("e.svg") and not board.is_image(None)


def test_last_sync_is_the_newest_successful_export(seeded):
    assert board.build_payload()["last_sync"] == store.channels()[0]["last_export_at"]
    store.record_run({"run_id": "r1", "channel_id": CHANNEL, "started_at": "2026-10-02T08:00:00Z",
                      "finished_at": "2026-10-02T08:05:00Z", "status": "ok", "message_count": 6})
    store.record_run({"run_id": "r2", "channel_id": CHANNEL, "started_at": "2026-10-02T09:00:00Z",
                      "finished_at": "2026-10-02T09:01:00Z", "status": "error", "message_count": 0})
    assert board.build_payload()["last_sync"] == "2026-10-02T08:05:00Z"


def test_an_empty_store_gives_an_empty_payload(rawstore_tmp_db):
    p = board.build_payload()
    assert p["channels"] == [] and p["messages"] == [] and p["total"] == 0
    assert p["last_sync"] is None and p["pages"] == 1


def test_building_the_payload_makes_no_network_or_subprocess_call(seeded, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the board must not call out")
    monkeypatch.setattr(exporter, "run_cli", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    assert board.build_payload()["total"] == 6


def test_filters_are_echoed_for_the_form(seeded):
    p = board.build_payload(channel=CHANNEL, q="nvda", date_from="2026-09-30", date_to="2026-10-01")
    assert p["filters"] == {"channel": CHANNEL, "q": "nvda",
                            "date_from": "2026-09-30", "date_to": "2026-10-01"}


def test_a_store_with_no_discord_file_yet_gives_an_empty_board(isolate_rawstore, monkeypatch):
    """Read-only first look (auto-ensure off, no discord.duckdb): the board is empty, not a crash."""
    monkeypatch.setenv("RC_STORE_AUTO_ENSURE", "0")
    p = board.build_payload()
    assert p["channels"] == [] and p["messages"] == [] and p["total"] == 0 and p["last_sync"] is None


@pytest.mark.parametrize("rel,ok", [
    ("9/a.png", True), ("9/sub dir/b-1.png", True),
    ("", False), ("\\\\host\\share\\x.png", False), ("\\x.png", False), ("C:\\x.png", False), ("a:b.png", False),
    ("/abs/x.png", False), ("../x.png", False), ("9/../x.png", False), ("9//x.png", False), ("9/./x.png", False),
    ("9/a\x00.png", False),
    # N-B (#233): Windows drops a trailing dot/space and reserves device names
    ("9/a.", False), ("9/a ", False), ("9/dir./b.png", False), ("9/.hidden.png", True), ("9/ok.name.png", True),
    ("9/CON", False), ("9/nul.png", False), ("9/Com1.txt", False), ("9/LPT9", False), ("9/con.tar.gz", False),
    ("9/COM0.png", True), ("9/console.png", True), ("9/communication.png", True)])
def test_url_rel_is_safe_is_a_pure_string_check(rel, ok):
    assert board.url_rel_is_safe(rel) is ok


@pytest.mark.parametrize("stored,query", [("Hava çok ılık bugün", "ILIK"), ("Hava çok ILIK bugün", "ılık"),
                                          ("İSTANBUL açılış", "istanbul"), ("Plain ascii ILIK", "ilik")])
def test_text_search_folds_the_turkish_dotted_and_dotless_i(rawstore_tmp_db, stored, query):
    import json
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["messages"] = raw["messages"][:1]
    raw["messages"][0]["content"] = stored
    store.save_export(loader.parse_export(raw))
    p = board.build_payload(q=query)
    assert [m["content"] for m in p["messages"]] == [stored]
