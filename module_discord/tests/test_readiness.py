"""#242: the Discord export readiness check. Environment and the CLI file only: no network, no token value."""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import credentials
from RevenueChaser.module_discord import exporter, readiness

TOKEN = "SENTINEL-TOKEN-4f9a1c"
GUILD = "SENTINEL-GUILD-773190"


@pytest.fixture
def ready_env(monkeypatch, tmp_path):
    """Everything configured, and ``exporter.cli_version`` answering 2.48.0 (the calls are recorded)."""
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.setenv("RC_DISCORD_GUILD_ID", GUILD)
    calls = []

    def fake_version():
        calls.append(1)
        return exporter.CliVersion((2, 48, 0), "v2.48.0", "")

    monkeypatch.setattr(exporter, "cli_version", fake_version)
    return exe, calls


def _by_key(result):
    return {c["key"]: c for c in result["checks"]}


def _states(result):
    return {c["key"]: c["state"] for c in result["checks"]}


def test_everything_configured_is_ready(ready_env):
    exe, _ = ready_env
    result = readiness.collect()
    assert result["ready"] is True
    assert [c["key"] for c in result["checks"]] == ["cli", "version", "token", "guild"]
    assert set(_states(result).values()) == {"ok"}
    assert result["min_version"] == "2.45"
    by = _by_key(result)
    assert str(exe) in by["cli"]["detail"]
    assert "2.48.0" in by["version"]["detail"] and "2.45" in by["version"]["detail"]
    assert "DISCORD_TOKEN" in by["token"]["detail"]
    assert all(c["label"] for c in result["checks"])


def test_the_cli_path_unset_fails_that_check_and_the_version_is_not_asked(ready_env, monkeypatch):
    _, calls = ready_env
    monkeypatch.delenv("RC_DCE_CLI")
    result = readiness.collect()
    by = _by_key(result)
    assert by["cli"]["state"] == "bad" and by["cli"]["detail"] == credentials.dce_cli_problem()
    assert by["version"]["state"] == "skipped"
    assert calls == [], "the version is not asked while the CLI check fails"
    assert result["ready"] is False


def test_the_cli_path_not_a_file_fails_that_check(ready_env, monkeypatch, tmp_path):
    _, calls = ready_env
    missing = tmp_path / "gone.exe"
    monkeypatch.setenv("RC_DCE_CLI", str(missing))
    by = _by_key(readiness.collect())
    assert by["cli"]["state"] == "bad" and str(missing) in by["cli"]["detail"]
    assert by["version"]["state"] == "skipped" and calls == []


@pytest.mark.parametrize("version, state", [((2, 44, 9), "bad"), ((2, 45, 0), "ok"), ((3, 0), "ok")])
def test_the_version_is_compared_with_the_minimum(ready_env, monkeypatch, version, state):
    monkeypatch.setattr(exporter, "cli_version", lambda: exporter.CliVersion(version, "", ""))
    result = readiness.collect()
    check = _by_key(result)["version"]
    assert check["state"] == state
    if state == "bad":
        shown = ".".join(str(n) for n in version)
        assert shown in check["detail"] and "2.45" in check["detail"] and "too old" in check["detail"]
        assert result["ready"] is False


def test_an_unreadable_version_is_unknown_never_ok(ready_env, monkeypatch):
    monkeypatch.setattr(exporter, "cli_version",
                        lambda: exporter.CliVersion(None, "", "DiscordChatExporter exited with code 3"))
    result = readiness.collect()
    check = _by_key(result)["version"]
    assert check["state"] == "unknown" and "exited with code 3" in check["detail"]
    assert result["ready"] is False


@pytest.mark.parametrize("value", [None, "", "   "])
def test_the_token_unset_fails_with_the_jobs_own_message(ready_env, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("DISCORD_TOKEN")
    else:
        monkeypatch.setenv("DISCORD_TOKEN", value)
    result = readiness.collect()
    check = _by_key(result)["token"]
    assert check["state"] == "bad" and check["detail"] == credentials.missing_message("discord")
    assert _states(result)["cli"] == "ok" and _states(result)["guild"] == "ok"
    assert result["ready"] is False


@pytest.mark.parametrize("value", [None, "", "   "])
def test_the_guild_unset_fails_with_the_jobs_own_message(ready_env, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("RC_DISCORD_GUILD_ID")
    else:
        monkeypatch.setenv("RC_DISCORD_GUILD_ID", value)
    result = readiness.collect()
    check = _by_key(result)["guild"]
    assert check["state"] == "bad" and check["detail"] == credentials.discord_guild_problem()
    assert _states(result)["token"] == "ok"
    assert result["ready"] is False


def test_ready_needs_all_four_checks(ready_env, monkeypatch):
    """Three of four ok is not ready, whichever one is the failing one."""
    base = {"cli": ("RC_DCE_CLI", None), "token": ("DISCORD_TOKEN", None), "guild": ("RC_DISCORD_GUILD_ID", None)}
    for key, (var, _) in base.items():
        with monkeypatch.context() as m:
            m.delenv(var)
            result = readiness.collect()
            assert result["ready"] is False, key
            assert [c for c in result["checks"] if c["state"] == "ok"], key
    with monkeypatch.context() as m:
        m.setattr(exporter, "cli_version", lambda: exporter.CliVersion((2, 1), "", ""))
        assert readiness.collect()["ready"] is False


@pytest.mark.parametrize("token_set", [True, False])
@pytest.mark.parametrize("guild_set", [True, False])
@pytest.mark.parametrize("version", [(2, 48, 0), (2, 1, 0), None])
def test_neither_the_token_nor_the_guild_value_is_ever_in_the_result(ready_env, monkeypatch, token_set, guild_set,
                                                                     version):
    if not token_set:
        monkeypatch.delenv("DISCORD_TOKEN")
    if not guild_set:
        monkeypatch.delenv("RC_DISCORD_GUILD_ID")
    monkeypatch.setattr(exporter, "cli_version",
                        lambda: exporter.CliVersion(version, "", "" if version else "exit code 1"))
    text = json.dumps(readiness.collect())
    assert TOKEN not in text and GUILD not in text


def test_the_real_version_probe_is_used_once_across_two_collects(monkeypatch, tmp_path):
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    calls = []

    def fake_run_cli(args, env=None, timeout=600, *, creationflags=0, scrub_secrets=False):
        calls.append(list(args))
        return exporter.CliResult(0, "v2.48.0\n", "")

    monkeypatch.setattr(exporter, "run_cli", fake_run_cli)
    first, second = readiness.collect(), readiness.collect()
    assert _states(first)["version"] == "ok" and first == second
    assert calls == [["--version"]]
