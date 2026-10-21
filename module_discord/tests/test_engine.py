"""#72 S1: discover + update orchestration. ``exporter.run_cli`` is monkeypatched, so
nothing touches the network, the DCE exe or a live store."""
from __future__ import annotations

import json
import socket

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_discord import engine, exporter, store

from .conftest import FIXTURE

CHANNEL = "900000000000000010"


@pytest.fixture(autouse=True)
def dce_cli(monkeypatch, tmp_path):
    exe = tmp_path / "dce-fake"
    exe.write_text("")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    return exe


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "test-token-not-real")
    monkeypatch.setenv("RC_DISCORD_GUILD_ID", "900000000000000001")


class FakeCli:
    """Stands in for ``exporter.run_cli``; records calls, writes the fixture on ``export``."""

    def __init__(self, channels_out="", export_json=None, returncode=0):
        self.calls = []
        self.channels_out = channels_out
        self.export_json = export_json
        self.returncode = returncode

    def __call__(self, args, env=None, timeout=600):
        self.calls.append((list(args), dict(env or {}), timeout))
        if args[0] == "channels":
            return exporter.CliResult(self.returncode, self.channels_out, "boom" if self.returncode else "")
        if args[0] == "export" and self.returncode == 0:
            out = args[args.index("-o") + 1]
            data = self.export_json or json.loads(FIXTURE.read_text(encoding="utf-8"))
            with open(out, "w", encoding="utf-8") as f:
                json.dump(data, f)
        return exporter.CliResult(self.returncode, "", "boom" if self.returncode else "")


def test_discover_stores_channels_disabled(rawstore_tmp_db, token, monkeypatch):
    cli = FakeCli(channels_out=f"{CHANNEL} | Ideas / swing-setups\n900000000000000011 | general\n")
    monkeypatch.setattr(exporter, "run_cli", cli)
    res = engine.discover()
    assert res == {"guild_id": "900000000000000001", "channels": 2, "new": 2}
    assert {c["channel_id"]: c["enabled"] for c in store.channels()} == {
        CHANNEL: False, "900000000000000011": False}
    args, env, _ = cli.calls[0]
    assert args[:3] == ["channels", "-g", "900000000000000001"]
    assert env == {"DISCORD_TOKEN": "test-token-not-real"}
    assert "test-token-not-real" not in " ".join(args)


def test_discover_without_a_token_names_the_variable_and_calls_nothing(rawstore_tmp_db, monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setenv("RC_DISCORD_GUILD_ID", "1")
    cli = FakeCli()
    monkeypatch.setattr(exporter, "run_cli", cli)
    with pytest.raises(engine.DiscordEngineError, match="DISCORD_TOKEN"):
        engine.discover()
    assert cli.calls == []


def test_discover_without_the_cli_path_calls_nothing(rawstore_tmp_db, token, monkeypatch):
    monkeypatch.delenv("RC_DCE_CLI")
    cli = FakeCli()
    monkeypatch.setattr(exporter, "run_cli", cli)
    with pytest.raises(engine.DiscordEngineError, match="RC_DCE_CLI"):
        engine.discover()
    assert cli.calls == []


def test_discover_without_a_guild_id_says_which_setting(rawstore_tmp_db, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.delenv("RC_DISCORD_GUILD_ID", raising=False)
    monkeypatch.setattr(exporter, "run_cli", FakeCli())
    with pytest.raises(engine.DiscordEngineError, match="RC_DISCORD_GUILD_ID"):
        engine.discover()


def test_discover_failure_is_reported_with_the_cli_stderr(rawstore_tmp_db, token, monkeypatch):
    monkeypatch.setattr(exporter, "run_cli", FakeCli(returncode=1))
    with pytest.raises(engine.DiscordEngineError, match="boom"):
        engine.discover()
    assert rawstore.count("discord_channels") == 0


def test_update_exports_only_enabled_channels_and_stores_messages(rawstore_tmp_db, token, monkeypatch):
    store.upsert_discovered([{"channel_id": CHANNEL, "guild_id": "g", "name": "x", "category": "y"},
                             {"channel_id": "900000000000000011", "guild_id": "g", "name": "off", "category": ""}])
    store.set_enabled([CHANNEL], True)
    cli = FakeCli()
    monkeypatch.setattr(exporter, "run_cli", cli)
    res = engine.update()
    assert res["channels"] == 1 and res["messages"] == 6 and res["new"] == 6 and res["failed"] == []
    exports = [c for c in cli.calls if c[0][0] == "export"]
    assert len(exports) == 1 and exports[0][0][exports[0][0].index("-c") + 1] == CHANNEL
    assert exports[0][1] == {"DISCORD_TOKEN": "test-token-not-real"}
    assert rawstore.count("discord_messages") == 6


def test_update_is_incremental_via_the_last_stored_message_id(rawstore_tmp_db, token, monkeypatch):
    store.upsert_discovered([{"channel_id": CHANNEL, "guild_id": "g", "name": "x", "category": "y"}])
    store.set_enabled([CHANNEL], True)
    cli = FakeCli()
    monkeypatch.setattr(exporter, "run_cli", cli)
    engine.update()
    first_args = cli.calls[0][0]
    assert "--after" not in first_args                       # first run: everything
    engine.update()
    second_args = [c for c in cli.calls if c[0][0] == "export"][1][0]
    assert second_args[second_args.index("--after") + 1] == "900000000000001006"
    assert rawstore.count("discord_messages") == 6            # re-ingest added no duplicates


def test_update_records_a_run_per_channel(rawstore_tmp_db, token, monkeypatch):
    store.upsert_discovered([{"channel_id": CHANNEL, "guild_id": "g", "name": "x", "category": "y"}])
    store.set_enabled([CHANNEL], True)
    monkeypatch.setattr(exporter, "run_cli", FakeCli())
    engine.update()
    runs = rawstore.get_all("discord_export_runs")
    assert len(runs) == 1
    assert runs[0]["channel_id"] == CHANNEL and runs[0]["status"] == "ok"
    assert runs[0]["message_count"] == 6 and runs[0]["finished_at"]


def test_a_failed_channel_is_recorded_and_does_not_stop_the_others(rawstore_tmp_db, token, monkeypatch):
    other = "900000000000000011"
    store.upsert_discovered([{"channel_id": other, "guild_id": "g", "name": "a", "category": ""},
                             {"channel_id": CHANNEL, "guild_id": "g", "name": "b", "category": ""}])
    store.set_enabled([other, CHANNEL], True)

    class Flaky(FakeCli):
        def __call__(self, args, env=None, timeout=600):
            if args[0] == "export" and args[args.index("-c") + 1] == other:
                self.calls.append((list(args), dict(env or {}), timeout))
                return exporter.CliResult(1, "", "channel gone")
            return super().__call__(args, env, timeout)

    monkeypatch.setattr(exporter, "run_cli", Flaky())
    res = engine.update()
    assert res["failed"] == [other] and res["messages"] == 6
    statuses = {r["channel_id"]: r["status"] for r in rawstore.get_all("discord_export_runs")}
    assert statuses == {other: "error", CHANNEL: "ok"}
    detail = [r for r in rawstore.get_all("discord_export_runs") if r["channel_id"] == other][0]["detail"]
    assert "channel gone" in detail


def test_update_without_enabled_channels_is_a_noop(rawstore_tmp_db, token, monkeypatch):
    cli = FakeCli()
    monkeypatch.setattr(exporter, "run_cli", cli)
    assert engine.update() == {"channels": 0, "messages": 0, "new": 0, "failed": []}
    assert cli.calls == []


def test_update_without_a_token_calls_nothing(rawstore_tmp_db, monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    store.upsert_discovered([{"channel_id": CHANNEL, "guild_id": "g", "name": "x", "category": "y"}])
    store.set_enabled([CHANNEL], True)
    cli = FakeCli()
    monkeypatch.setattr(exporter, "run_cli", cli)
    with pytest.raises(engine.DiscordEngineError, match="DISCORD_TOKEN"):
        engine.update()
    assert cli.calls == []


def test_media_flag_adds_the_media_dir_under_the_durable_tree(rawstore_tmp_db, token, monkeypatch):
    from RevenueChaser.core import config
    store.upsert_discovered([{"channel_id": CHANNEL, "guild_id": "g", "name": "x", "category": "y"}])
    store.set_enabled([CHANNEL], True)
    cli = FakeCli()
    monkeypatch.setattr(exporter, "run_cli", cli)
    engine.update(media=True)
    args = cli.calls[0][0]
    assert "--media" in args
    assert args[args.index("--media-dir") + 1] == str(config.DISCORD_MEDIA_DIR / CHANNEL)


def test_the_export_file_is_not_left_behind(rawstore_tmp_db, token, monkeypatch, tmp_path):
    store.upsert_discovered([{"channel_id": CHANNEL, "guild_id": "g", "name": "x", "category": "y"}])
    store.set_enabled([CHANNEL], True)
    cli = FakeCli()
    monkeypatch.setattr(exporter, "run_cli", cli)
    engine.update()
    from pathlib import Path
    out = Path(cli.calls[0][0][cli.calls[0][0].index("-o") + 1])
    assert not out.exists()


def test_update_opens_no_network_connection(rawstore_tmp_db, token, monkeypatch):
    """Belt and braces on top of conftest's socket guard: the engine itself never dials."""
    def no_dial(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(socket.socket, "connect", no_dial)
    store.upsert_discovered([{"channel_id": CHANNEL, "guild_id": "g", "name": "x", "category": "y"}])
    store.set_enabled([CHANNEL], True)
    monkeypatch.setattr(exporter, "run_cli", FakeCli())
    assert engine.update()["messages"] == 6


def _two_channels_one_bad(monkeypatch, bad_writer):
    other = "900000000000000011"
    store.upsert_discovered([{"channel_id": other, "guild_id": "g", "name": "a", "category": ""},
                             {"channel_id": CHANNEL, "guild_id": "g", "name": "b", "category": ""}])
    store.set_enabled([other, CHANNEL], True)

    class Cli(FakeCli):
        def __call__(self, args, env=None, timeout=600):
            if args[0] == "export" and args[args.index("-c") + 1] == other:
                self.calls.append((list(args), dict(env or {}), timeout))
                bad_writer(args[args.index("-o") + 1])
                return exporter.CliResult(0, "", "")
            return super().__call__(args, env, timeout)

    monkeypatch.setattr(exporter, "run_cli", Cli())
    return other


@pytest.mark.parametrize("name,writer", [
    ("invalid json", lambda out: open(out, "w").write("{not json")),
    ("no file written", lambda out: None),
    ("messages null", lambda out: open(out, "w").write(
        '{"channel": {"id": "900000000000000011"}, "messages": null}')),
    ("non utf8 bytes", lambda out: open(out, "wb").write(b"\xff\xfe\x00")),
])
def test_a_bad_export_is_recorded_with_a_reason_and_the_other_channels_still_run(
        rawstore_tmp_db, token, monkeypatch, name, writer):
    other = _two_channels_one_bad(monkeypatch, writer)
    res = engine.update()
    assert res["failed"] == [other] and res["messages"] == 6, name
    runs = {r["channel_id"]: r for r in rawstore.get_all("discord_export_runs")}
    assert runs[other]["status"] == "error" and runs[other]["detail"], name
    assert runs[CHANNEL]["status"] == "ok"


def test_the_token_is_redacted_from_the_recorded_failure_detail(rawstore_tmp_db, token, monkeypatch):
    store.upsert_discovered([{"channel_id": CHANNEL, "guild_id": "g", "name": "b", "category": ""}])
    store.set_enabled([CHANNEL], True)

    class Leaky(FakeCli):
        def __call__(self, args, env=None, timeout=600):
            return exporter.CliResult(1, "", "401 Unauthorized for test-token-not-real, retry")

    monkeypatch.setattr(exporter, "run_cli", Leaky())
    engine.update()
    detail = rawstore.get_all("discord_export_runs")[0]["detail"]
    assert "test-token-not-real" not in detail and "401 Unauthorized" in detail


def test_discover_failure_text_never_contains_the_token(rawstore_tmp_db, token, monkeypatch):
    class Leaky(FakeCli):
        def __call__(self, args, env=None, timeout=600):
            return exporter.CliResult(1, "", "bad token test-token-not-real")

    monkeypatch.setattr(exporter, "run_cli", Leaky())
    with pytest.raises(engine.DiscordEngineError) as ei:
        engine.discover()
    assert "test-token-not-real" not in str(ei.value)
