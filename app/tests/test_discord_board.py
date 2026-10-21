"""#74 S2: dashboard wiring of the Discord tab and its routes. Fixtures only; no network, no DCE."""
from __future__ import annotations

import json
import subprocess

import pytest

from RevenueChaser.app import dashboard, jobs, runners
from RevenueChaser.core.tests.panel_client import panel_test_client
from RevenueChaser.core import config, rawstore
from RevenueChaser.module_discord import exporter, loader, store
from RevenueChaser.module_discord.tests.conftest import FIXTURE

CH = "900000000000000010"


@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return panel_test_client(dashboard.app)


@pytest.fixture
def seeded(rawstore_tmp_db):
    store.save_export(loader.load_export_file(FIXTURE))


def test_the_tab_is_registered_in_the_research_group():
    tabs = dashboard.TABS
    i_head = next(i for i, t in enumerate(tabs) if t["type"] == "header" and t["label"] == "Research")
    i_next = next(i for i, t in enumerate(tabs[i_head + 1:], i_head + 1) if t["type"] == "header")
    keys = [t["key"] for t in tabs[i_head + 1:i_next]]
    assert "module_discord" in keys
    tab = next(t for t in tabs if t.get("key") == "module_discord")
    assert tab["url"] == "/view/module_discord" and tab["label"] == "Discord"


def test_the_board_is_store_refreshable_and_renders_in_memory():
    """DF54: no cached discord_latest.html exists any more; the view route renders the board itself."""
    assert "module_discord" in dashboard._STORE_REFRESHABLE
    assert "module_discord" not in dashboard._MODEL_ONLY_RENDERERS
    assert dashboard._module_file("module_discord") is None
    assert not hasattr(runners, "DISCORD_LATEST") and not hasattr(runners, "run_discord_board")


def test_jobs_are_registered_store_only():
    for name in ("module_discord", "module_discord_discover"):
        assert name in jobs._RUNNERS, name
        assert jobs._REQUIRES_UW[name] is False, name
        assert jobs._LANE[name] == "long", name        # a channel export can take minutes


def test_the_template_knows_the_tab():
    html = (dashboard.Path(dashboard.__file__).parent.parent / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert "'module_discord'" in html.split("var STORE_BACKED")[1].split("];")[0]
    assert "module_discord: '/run/module_discord'" in html
    show_tab = html.split("function showTab")[1].split("checkDataAsOf(key)")[0]
    assert "module_discord" in show_tab                               # Fetch button list


def test_view_renders_the_archive(client, seeded):
    r = client.get("/view/module_discord")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "swing-setups" in body and "Watching $NVDA" in body


def test_a_view_renders_in_memory_and_writes_no_file(client, seeded, monkeypatch, tmp_path):
    monkeypatch.setattr(runners, "OUTPUT_DIR", tmp_path / "out")
    r = client.get(f"/view/module_discord?channel={CH}&q=reclaim")
    assert r.status_code == 200 and "Watching $NVDA" in r.get_data(as_text=True)
    assert list((tmp_path / "out").rglob("*")) == [], "no cache file for the Discord board any more (DF54)"


def test_view_on_an_empty_store_points_at_discover(client, rawstore_tmp_db):
    r = client.get("/view/module_discord")
    assert r.status_code == 200 and "Discover" in r.get_data(as_text=True)


def test_view_applies_filters_from_the_query(client, seeded):
    r = client.get(f"/view/module_discord?channel={CH}&q=reclaim&date_from=2026-09-30&date_to=2026-09-30")
    body = r.get_data(as_text=True)
    assert "Watching $NVDA" in body and "Buying 2 call spreads" not in body


@pytest.mark.parametrize("qs", ["date_from=nope", "date_to=2026-99-99"])
def test_view_rejects_a_bad_date_with_400(client, seeded, qs):
    assert client.get(f"/view/module_discord?{qs}").status_code == 400


def test_a_bad_page_is_clamped_not_an_error(client, seeded):
    assert client.get("/view/module_discord?page=abc").status_code == 200


def test_refresh_makes_no_provider_or_subprocess_call(client, seeded, monkeypatch):
    from RevenueChaser.module_discord import engine
    store.set_enabled([CH], True)       # a followed channel is what a wrong Refresh would export

    def boom(*a, **k):
        raise AssertionError("Refresh must not call out")
    monkeypatch.setattr(engine, "update", boom)
    monkeypatch.setattr(engine, "discover", boom)
    monkeypatch.setattr(exporter, "run_cli", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(runners, "run_discord", boom)
    monkeypatch.setattr(runners, "run_discord_discover", boom)
    monkeypatch.setattr(jobs, "submit", boom)
    r = client.get("/view/module_discord?refresh=1")
    assert r.status_code == 200 and "swing-setups" in r.get_data(as_text=True)


def test_refresh_rerenders_from_the_store_after_new_data(client, seeded):
    client.get("/view/module_discord")
    store.save_export(loader.load_export_file(FIXTURE))
    store.upsert_discovered([{"channel_id": "900000000000000042", "guild_id": "1", "name": "fresh", "category": "x"}])
    assert "fresh" in client.get("/view/module_discord?refresh=1").get_data(as_text=True)


# ── follow ──────────────────────────────────────────────────────────────────

def _post_follow(client, body):
    return client.post("/api/discord/follow", data=json.dumps(body), content_type="application/json")


def test_follow_toggle_persists(client, seeded):
    assert store.channels(enabled_only=True) == []
    r = _post_follow(client, {"channel_id": CH, "enabled": True})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "channel_id": CH, "enabled": True}
    assert [c["channel_id"] for c in store.channels(enabled_only=True)] == [CH]
    assert 'class="follow"' in client.get("/view/module_discord").get_data(as_text=True)
    _post_follow(client, {"channel_id": CH, "enabled": False})
    assert store.channels(enabled_only=True) == []


def test_follow_rejects_unknown_channel_and_bad_bodies(client, seeded):
    assert _post_follow(client, {"channel_id": "404", "enabled": True}).status_code == 404
    assert _post_follow(client, {"channel_id": CH}).status_code == 400
    assert _post_follow(client, {"channel_id": CH, "enabled": "yes"}).status_code == 400
    assert _post_follow(client, {"enabled": True}).status_code == 400
    assert client.post("/api/discord/follow", data="not json", content_type="application/json").status_code == 400
    assert client.post("/api/discord/follow", data="[]", content_type="application/json").status_code == 400
    assert store.channels(enabled_only=True) == []


def test_follow_is_post_only(client, seeded):
    assert client.get("/api/discord/follow").status_code == 405


# ── media ───────────────────────────────────────────────────────────────────

@pytest.fixture
def media(monkeypatch, tmp_path):
    root = tmp_path / "dm"
    (root / CH).mkdir(parents=True)
    (root / CH / "chart.png").write_bytes(b"\x89PNG-bytes")
    (tmp_path / "secret.txt").write_text("secret")
    monkeypatch.setattr(config, "DISCORD_MEDIA_DIR", root)
    return root


def test_media_is_served_from_inside_the_media_dir(client, media):
    r = client.get(f"/discord/media/{CH}/chart.png")
    assert r.status_code == 200 and r.data == b"\x89PNG-bytes"


@pytest.mark.parametrize("path", ["../secret.txt", f"{CH}/../../secret.txt", "%2e%2e/secret.txt",
                                  f"{CH}/missing.png", "..%2fsecret.txt"])
def test_media_outside_or_missing_is_404(client, media, path):
    assert client.get(f"/discord/media/{path}").status_code == 404


def test_media_refuses_a_symlink_that_escapes(client, media, tmp_path):
    try:
        (media / CH / "link.png").symlink_to(tmp_path / "secret.txt")
    except OSError:
        pytest.skip("no symlinks here")
    assert client.get(f"/discord/media/{CH}/link.png").status_code == 404


def test_media_is_served_as_an_attachment_not_as_html(client, media):
    (media / CH / "page.html").write_text("<script>alert(1)</script>")
    (media / CH / "vector.svg").write_text("<svg onload=alert(1)/>")
    for name in ("page.html", "vector.svg"):
        r = client.get(f"/discord/media/{CH}/{name}")
        assert r.status_code == 200, name
        assert "attachment" in r.headers.get("Content-Disposition", ""), name
        assert r.headers.get("X-Content-Type-Options") == "nosniff", name
    r2 = client.get(f"/discord/media/{CH}/chart.png")
    assert r2.status_code == 200 and "attachment" not in r2.headers.get("Content-Disposition", "")
    assert r2.headers.get("X-Content-Type-Options") == "nosniff"


def test_media_refuses_a_directory_and_the_root(client, media):
    assert client.get(f"/discord/media/{CH}").status_code == 404
    assert client.get(f"/discord/media/{CH}/").status_code == 404


# ── run routes ──────────────────────────────────────────────────────────────

def test_run_routes_refuse_without_a_token_and_submit_nothing(client, monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: submitted.append(a) or "j1")
    for route in ("/run/module_discord", "/run/module_discord_discover"):
        r = client.post(route)
        assert r.status_code == 400 and "DISCORD_TOKEN" in r.get_json()["error"], route
    assert submitted == []


def test_run_routes_submit_when_configured(client, seeded, monkeypatch, tmp_path):
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    monkeypatch.setenv("RC_DISCORD_GUILD_ID", "1")
    seen = []
    monkeypatch.setattr(jobs, "submit", lambda mod, params=None: seen.append((mod, params)) or "job1")
    r = client.post("/run/module_discord")
    assert r.status_code == 202 and r.get_json()["job_id"] == "job1"
    r = client.post("/run/module_discord_discover")
    assert r.status_code == 202
    r = client.post("/run/module_discord", data={"channel": CH})
    assert seen[0] == ("module_discord", {"media": True}) and seen[1] == ("module_discord_discover", {})
    assert seen[2] == ("module_discord", {"media": True, "channel_id": CH})


def test_run_rejects_an_unknown_channel(client, seeded, monkeypatch, tmp_path):
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: submitted.append(a) or "j")
    r = client.post("/run/module_discord", data={"channel": "123456789"})
    assert r.status_code == 404 and submitted == []


def test_run_rejects_a_non_numeric_channel(client, monkeypatch, tmp_path):
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: "j")
    assert client.post("/run/module_discord", data={"channel": "../x"}).status_code == 400


def test_the_runner_updates_one_channel_and_writes_no_html(rawstore_tmp_db, monkeypatch, tmp_path):
    from RevenueChaser.module_discord import engine
    seen = {}

    def fake_update(media=False, channel_ids=None):
        seen["channel_ids"] = channel_ids
        return {"channels": 1, "messages": 2, "new": 2, "failed": []}

    monkeypatch.setattr(engine, "update", fake_update)
    monkeypatch.setattr(runners, "OUTPUT_DIR", tmp_path / "out")
    with pytest.raises(jobs.JobMessage):
        runners.run_discord(channel_id=CH)
    assert seen["channel_ids"] == [CH]
    assert list((tmp_path / "out").rglob("*")) == []      # DF54: the job no longer writes discord_latest.html


def test_discover_writes_no_html(rawstore_tmp_db, monkeypatch, tmp_path):
    """DF54: the Discover job no longer renders discord_latest.html either."""
    from RevenueChaser.module_discord import engine
    monkeypatch.setattr(engine, "discover", lambda: {"channels": 1, "new": 1})
    monkeypatch.setattr(runners, "OUTPUT_DIR", tmp_path / "out")
    with pytest.raises(jobs.JobMessage):
        runners.run_discord_discover()
    assert list((tmp_path / "out").rglob("*")) == []


def test_engine_update_can_be_limited_to_given_channels(rawstore_tmp_db, monkeypatch, tmp_path):
    from RevenueChaser.module_discord import engine
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    store.upsert_discovered([{"channel_id": "1", "guild_id": "g", "name": "a", "category": ""},
                             {"channel_id": "2", "guild_id": "g", "name": "b", "category": ""}])
    store.set_enabled(["1"], True)              # channel 2 is not followed
    calls = []

    def fake_cli(args, env=None, timeout=600):
        calls.append(args[args.index("-c") + 1])
        out = args[args.index("-o") + 1]
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        data["channel"]["id"] = calls[-1]
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return exporter.CliResult(0, "", "")

    monkeypatch.setattr(exporter, "run_cli", fake_cli)
    engine.update(channel_ids=["2"])
    assert calls == ["2"]


# ── B1: no filesystem call before a pure-string check (Windows resolve() on a UNC path opens SMB) ──

@pytest.fixture
def fs_spy(monkeypatch):
    """Fail any Path.resolve / is_file whose path holds a backslash, a colon or the attacker's host."""
    from pathlib import Path as _P
    calls = []
    real_resolve, real_is_file = _P.resolve, _P.is_file

    def resolve(self, *a, **k):
        if "\\" in str(self) or "attacker" in str(self) or ":" in str(self)[2:]:
            calls.append(str(self))
        return real_resolve(self, *a, **k)

    def is_file(self, *a, **k):
        if "\\" in str(self) or "attacker" in str(self) or ":" in str(self)[2:]:
            calls.append(str(self))
        return real_is_file(self, *a, **k)

    monkeypatch.setattr(_P, "resolve", resolve)
    monkeypatch.setattr(_P, "is_file", is_file)
    return calls


@pytest.mark.parametrize("path", [
    "%5C%5Cattacker.example%5Cshare%5Cx.png", "%5C/attacker.example/share/x.png",
    "C:%5Cx.png", "a:b.png", "%5Cx.png", "ok/..%5C..%5Cx.png"])
def test_media_never_touches_the_filesystem_for_a_unc_or_drive_path(client, media, fs_spy, path):
    r = client.get(f"/discord/media/{path}")
    assert r.status_code == 404
    assert fs_spy == [], f"filesystem was touched with {fs_spy}"


def test_media_refuses_a_cross_site_get(client, media):
    r = client.get(f"/discord/media/{CH}/chart.png", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 404
    ok = client.get(f"/discord/media/{CH}/chart.png", headers={"Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200


def test_media_is_sandboxed_by_csp(client, media):
    r = client.get(f"/discord/media/{CH}/chart.png")
    assert r.headers.get("Content-Security-Policy") == "sandbox"


# ── N6: minimum interval between exports, and no duplicate queued runs ──────────

def _recent_run(channel_id, minutes_ago, status="ok"):
    import time
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes_ago * 60))
    store.record_run({"run_id": f"r{channel_id}{minutes_ago}", "channel_id": channel_id, "started_at": started,
                      "finished_at": started, "after_id": None, "message_count": 1, "status": status, "detail": ""})


@pytest.fixture
def counting_update(monkeypatch):
    from RevenueChaser.module_discord import engine
    calls = []

    def fake(media=False, channel_ids=None):
        calls.append(channel_ids)
        return {"channels": 1, "messages": 0, "new": 0, "failed": []}

    monkeypatch.setattr(engine, "update", fake)
    return calls


def test_update_refuses_inside_the_minimum_interval(seeded, counting_update):
    store.set_enabled([CH], True)
    _recent_run(CH, 5)
    with pytest.raises(jobs.JobMessage, match=r"15 minutes"):
        runners.run_discord()
    assert counting_update == []


def test_update_runs_after_the_minimum_interval(seeded, counting_update):
    store.set_enabled([CH], True)
    _recent_run(CH, 20)
    with pytest.raises(jobs.JobMessage, match="exported"):
        runners.run_discord()
    assert counting_update == [None]


def test_force_overrides_the_interval(seeded, counting_update):
    store.set_enabled([CH], True)
    _recent_run(CH, 1)
    with pytest.raises(jobs.JobMessage):
        runners.run_discord(force=True)
    assert counting_update == [None]


def test_the_interval_is_per_channel_for_a_single_channel_update(seeded, counting_update):
    store.upsert_discovered([{"channel_id": "900000000000000042", "guild_id": "1", "name": "other", "category": ""}])
    _recent_run(CH, 1)
    with pytest.raises(jobs.JobMessage):
        runners.run_discord(channel_id="900000000000000042")
    assert counting_update == [["900000000000000042"]]           # the other channel is old enough
    with pytest.raises(jobs.JobMessage, match="15 minutes"):
        runners.run_discord(channel_id=CH)
    assert counting_update == [["900000000000000042"]]


def test_update_all_with_some_recent_channels_skips_only_those(seeded, counting_update):
    store.upsert_discovered([{"channel_id": "900000000000000042", "guild_id": "1", "name": "other", "category": ""}])
    store.set_enabled([CH, "900000000000000042"], True)
    _recent_run(CH, 1)
    with pytest.raises(jobs.JobMessage):
        runners.run_discord()
    assert counting_update == [["900000000000000042"]]


def test_a_second_submit_while_one_is_queued_is_refused(client, seeded, monkeypatch, tmp_path):
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    monkeypatch.setenv("RC_DISCORD_GUILD_ID", "1")
    monkeypatch.setitem(jobs._JOBS, "q1", {"id": "q1", "module": "module_discord", "status": "queued", "params": {}})
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: submitted.append(a) or "j")
    for route in ("/run/module_discord", "/run/module_discord_discover"):
        r = client.post(route)
        assert r.status_code == 409 and "already" in r.get_json()["error"], route
    assert submitted == []


def test_a_finished_job_does_not_block_the_next_submit(client, seeded, monkeypatch, tmp_path):
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    monkeypatch.setitem(jobs._JOBS, "d1", {"id": "d1", "module": "module_discord", "status": "done", "params": {}})
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: "j2")
    assert client.post("/run/module_discord").status_code == 202


# ── #233 N-A: the 15-minute edge, and a failed export counts as an export ───────

def test_update_is_skipped_at_14_minutes(seeded, counting_update):
    store.set_enabled([CH], True)
    _recent_run(CH, 14)
    with pytest.raises(jobs.JobMessage, match="15 minutes"):
        runners.run_discord()
    assert counting_update == []


def test_update_runs_at_16_minutes(seeded, counting_update):
    store.set_enabled([CH], True)
    _recent_run(CH, 16)
    with pytest.raises(jobs.JobMessage, match="exported"):
        runners.run_discord()
    assert counting_update == [None]


def test_a_recent_run_that_ended_in_error_still_blocks_the_update(seeded, counting_update):
    store.set_enabled([CH], True)
    _recent_run(CH, 3, status="error")
    with pytest.raises(jobs.JobMessage, match="15 minutes"):
        runners.run_discord()
    assert counting_update == []


# ── #233 N-C: the job messages say what was skipped, and for which scope ────────

def test_a_refused_single_channel_update_does_not_say_every_followed_channel(seeded, counting_update):
    store.set_enabled([CH], True)
    _recent_run(CH, 2)
    with pytest.raises(jobs.JobMessage) as e:
        runners.run_discord(channel_id=CH)
    assert "every followed channel" not in str(e.value) and "this channel" in str(e.value)


def test_update_all_names_the_recent_channels_it_skipped(seeded, counting_update):
    store.upsert_discovered([{"channel_id": "900000000000000042", "guild_id": "1", "name": "other", "category": ""}])
    store.set_enabled([CH, "900000000000000042"], True)
    _recent_run(CH, 1)
    name = next(c["name"] for c in store.channels() if c["channel_id"] == CH)
    with pytest.raises(jobs.JobMessage) as e:
        runners.run_discord()
    assert f"Skipped (exported less than 15 minutes ago): {name}" in str(e.value)
    assert "other" not in str(e.value).split("Skipped")[1]


def test_the_data_date_tooltip_does_not_call_a_store_board_live():
    """DF103: 'fetches live; it keeps no downloaded data' is wrong for a store board with no single date."""
    html = (dashboard.Path(dashboard.__file__).parent.parent / "templates" / "dashboard.html").read_text(encoding="utf-8")
    start = html.index("function checkDataAsOf")
    body = html[start:html.index("function checkSpxgammaFreshness")]
    assert "STORE_BACKED.indexOf(key)" in body
    assert "reads the store and has no single data date" in body
