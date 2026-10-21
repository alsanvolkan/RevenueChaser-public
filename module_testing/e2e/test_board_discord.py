"""Discord board (#74 S2), end to end.

The board is store-only: it renders from ``.store/discord.duckdb`` on every load, and Refresh must
never reach DCE or Discord. A fake DiscordChatExporter (a script that records each call in a marker
file) proves both halves: Refresh leaves no marker, and the Update job calls it once for the one
followed channel and the board then shows the new messages.
"""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from RevenueChaser.module_discord.tests.conftest import FIXTURE

_APP = "module_discord"
CH = "900000000000000010"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake DCE is a shebang script")

_FAKE = """#!{python}
import json, sys
args = sys.argv[1:]
open({marker!r}, "a").write(" ".join(args) + "\\n")
if args[0] == "--version":
    print("v2.48.0")
if args[0] == "export":
    data = json.load(open({fixture!r}, encoding="utf-8"))
    data["channel"]["id"] = args[args.index("-c") + 1]
    extra = json.loads(json.dumps(data["messages"][0]))
    extra.update(id="900000000000009999", content="E2E-NEW-MSG", timestamp="2026-10-01T09:00:00.000+00:00")
    data["messages"].append(extra)
    json.dump(data, open(args[args.index("-o") + 1], "w", encoding="utf-8"))
"""


@pytest.fixture
def fake_dce(tmp_path):
    marker = tmp_path / "dce_calls.txt"
    exe = tmp_path / "dce"
    exe.write_text(_FAKE.format(python=sys.executable, marker=str(marker), fixture=str(FIXTURE)))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe, marker


@pytest.fixture
def seeded_discord(dashboard, monkeypatch, fake_dce):
    """Seed the dashboard's temp store (the discord file sits beside raw.duckdb) and point it at the fake DCE."""
    import RevenueChaser.core.rawstore as rawstore
    from RevenueChaser.module_discord import loader, store

    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
    store.save_export(loader.load_export_file(FIXTURE))
    exe, _ = fake_dce
    media = dashboard.store_dir.parent / "discord_media"
    (media / CH).mkdir(parents=True)
    (media / CH / "chart.png").write_bytes(b"\x89PNG-e2e")
    (dashboard.store_dir.parent / "secret.txt").write_text("secret")
    dashboard.env.update({"RC_DISCORD_MEDIA_DIR": str(media), "DISCORD_TOKEN": "e2e-token", "RC_DCE_CLI": str(exe), "RC_DISCORD_GUILD_ID": "1"})
    dashboard.restart()
    return dashboard


class TestView:
    def test_the_tab_is_in_the_shell(self, dashboard):
        r = dashboard.get("/")
        assert r.status_code == 200 and "Discord" in r.text

    def test_the_archive_renders_and_filters(self, seeded_discord):
        body = seeded_discord.view(_APP).text
        assert "swing-setups" in body and "Watching $NVDA" in body
        filtered = seeded_discord.view(_APP, channel=CH, q="reclaim").text
        assert "Watching $NVDA" in filtered and "Buying 2 call spreads" not in filtered

    def test_a_hostile_message_is_escaped(self, seeded_discord):
        r = seeded_discord.view(_APP)
        assert r.status_code == 200
        assert "&lt;script&gt;alert" in r.text and "<script>alert" not in r.text


class TestRefreshIsOffline:
    def test_refresh_never_calls_dce(self, seeded_discord, fake_dce, provider_routes):
        _, marker = fake_dce
        # A followed channel gives a Refresh that wrongly ran Update something to export.
        r = seeded_discord.client.post("/api/discord/follow", json={"channel_id": CH, "enabled": True})
        assert r.status_code == 200
        before = provider_routes.request_count
        r = seeded_discord.view(_APP, refresh=True)
        assert r.status_code == 200 and "swing-setups" in r.text
        assert not marker.exists(), f"Refresh started DCE: {marker.read_text()}"
        assert provider_routes.request_count == before
        assert seeded_discord.yf_calls() == []


class TestSettingsReadinessCard:
    """#242: Settings starts the DCE exe once with --version; the Discord board's Refresh still never does."""

    def test_the_card_shows_the_version_and_a_second_refresh_does_not_start_the_exe_again(
            self, dashboard, fake_dce, provider_routes):
        exe, marker = fake_dce
        dashboard.env.update({"RC_DCE_CLI": str(exe), "DISCORD_TOKEN": "SENTINEL-E2E-TOKEN",
                              "RC_DISCORD_GUILD_ID": "SENTINEL-E2E-GUILD"})
        dashboard.restart()
        before = provider_routes.request_count
        first = dashboard.view("module_settings", refresh=True)
        second = dashboard.view("module_settings", refresh=True)
        assert first.status_code == 200 and second.status_code == 200
        assert 'id="discord-readiness"' in first.text and "2.48.0" in first.text
        assert "Ready" in first.text[first.text.index('id="discord-summary"'):][:300]
        assert marker.read_text().splitlines() == ["--version"], "exactly one --version across two Refreshes"
        assert provider_routes.request_count == before
        assert dashboard.yf_calls() == []
        for page in (first.text, second.text):
            assert "SENTINEL-E2E-TOKEN" not in page and "SENTINEL-E2E-GUILD" not in page

    def test_an_unconfigured_dashboard_shows_not_ready_and_starts_nothing(self, dashboard, fake_dce):
        _, marker = fake_dce
        page = dashboard.view("module_settings", refresh=True).text
        summary = page[page.index('id="discord-summary"'):][:300]
        assert "Not ready" in summary and "4 checks need attention" in summary
        assert not marker.exists()


class TestUpdateJob:
    def test_following_then_updating_one_channel_calls_dce_once_and_shows_it(self, seeded_discord, fake_dce):
        _, marker = fake_dce
        r = seeded_discord.client.post("/api/discord/follow", json={"channel_id": CH, "enabled": True})
        assert r.status_code == 200 and r.json()["enabled"] is True
        assert "E2E-NEW-MSG" not in seeded_discord.view(_APP).text
        result = seeded_discord.run_job("/run/module_discord", channel=CH)
        assert result.ok, f"update failed: {result}\n{seeded_discord.logs()}"
        calls = marker.read_text().strip().splitlines()
        assert len(calls) == 1 and calls[0].startswith("export") and f"-c {CH}" in calls[0]
        assert "E2E-NEW-MSG" in seeded_discord.view(_APP).text

    def test_the_media_route_serves_a_real_file_and_refuses_traversal(self, seeded_discord, tmp_path):
        assert seeded_discord.get(f"/discord/media/{CH}/chart.png").status_code == 200
        for bad in ("%2e%2e%2f%2e%2e%2fetc%2fpasswd", f"{CH}/%2e%2e%2f%2e%2e%2fsecret.txt", "%5C%5Chost%5Cs%5Cx.png"):
            assert seeded_discord.get(f"/discord/media/{bad}").status_code == 404, bad
