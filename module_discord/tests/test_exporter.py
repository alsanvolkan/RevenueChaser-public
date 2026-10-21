"""#72 S1: the DiscordChatExporter CLI seam. Nothing here runs the exe or touches the network."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from RevenueChaser.module_discord import exporter


def test_cli_path_comes_from_the_env_at_call_time(monkeypatch, tmp_path):
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    assert exporter.cli_path() == exe
    monkeypatch.delenv("RC_DCE_CLI")
    with pytest.raises(exporter.ExporterError, match="RC_DCE_CLI"):
        exporter.cli_path()


def test_cli_path_must_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("RC_DCE_CLI", str(tmp_path / "missing.exe"))
    with pytest.raises(exporter.ExporterError, match="not a file"):
        exporter.cli_path()


def test_run_cli_runs_the_exe_with_the_args_and_env(monkeypatch, tmp_path):
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, kw=kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="out", stderr="err")

    monkeypatch.setattr(exporter.subprocess, "run", fake_run)
    res = exporter.run_cli(["channels", "-g", "1"], env={"DISCORD_TOKEN": "x"}, timeout=7)
    assert res == exporter.CliResult(0, "out", "err")
    assert seen["cmd"] == [str(exe), "channels", "-g", "1"]
    assert seen["kw"]["timeout"] == 7
    assert seen["kw"]["env"]["DISCORD_TOKEN"] == "x"
    assert "PATH" in seen["kw"]["env"] or sys.platform == "win32"     # inherits the process env
    assert seen["kw"].get("shell", False) is False


def test_run_cli_decodes_output_as_utf8_and_never_crashes_on_bad_bytes(monkeypatch, tmp_path):
    """DCE on a Turkish Windows box can emit cp857; text=True alone would decode as cp1254 and raise."""
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(exporter.subprocess, "run", fake_run)
    exporter.run_cli(["channels"])
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"
    assert not seen.get("text")


def test_run_cli_really_survives_non_utf8_bytes(monkeypatch, tmp_path):
    script = tmp_path / "fake_dce.py"
    # ASCII-only source (escapes are resolved by the child): write_text() encodes with the locale codec,
    # which is cp1252 on Windows and cannot hold the Turkish letters.
    script.write_text(r"import sys" "\n"
                      r"sys.stdout.buffer.write('\u00fc \u0131 \u015e'.encode('cp857')+b'\n')" "\n"
                      r"sys.stderr.buffer.write(b'\xff\xfe bad')" "\n", encoding="ascii")
    monkeypatch.setenv("RC_DCE_CLI", str(script))
    real_run = subprocess.run

    def run_with_python(cmd, **kw):
        return real_run([sys.executable, *cmd], **kw)

    monkeypatch.setattr(exporter.subprocess, "run", run_with_python)
    res = exporter.run_cli([])
    assert isinstance(res.stdout, str) and isinstance(res.stderr, str)


def test_run_cli_turns_a_timeout_into_an_exporter_error(monkeypatch, tmp_path):
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(exporter.subprocess, "run", boom)
    with pytest.raises(exporter.ExporterError, match="timed out"):
        exporter.run_cli(["export"], timeout=1)


def test_run_cli_error_text_never_contains_the_token(monkeypatch, tmp_path):
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))

    def boom(cmd, **kw):
        raise OSError("cannot start; env was " + repr(kw["env"].get("DISCORD_TOKEN")))

    monkeypatch.setattr(exporter.subprocess, "run", boom)
    with pytest.raises(exporter.ExporterError) as ei:
        exporter.run_cli(["export"], env={"DISCORD_TOKEN": "SECRET-TOKEN-VALUE"})
    assert "SECRET-TOKEN-VALUE" not in str(ei.value)


def test_export_args_use_json_and_an_after_id_and_never_the_token():
    args = exporter.export_args("42", "/tmp/out.json", after_id="99", media_dir="/m")
    assert args[0] == "export"
    assert args[args.index("-c") + 1] == "42"
    assert args[args.index("-f") + 1] == "Json"
    assert args[args.index("-o") + 1] == "/tmp/out.json"
    assert args[args.index("--after") + 1] == "99"
    assert "--media" in args and args[args.index("--media-dir") + 1] == "/m"
    assert not any(a in ("-t", "--token") for a in args)


def test_export_args_without_after_or_media():
    args = exporter.export_args("42", "/tmp/out.json")
    assert "--after" not in args and "--media" not in args and "--media-dir" not in args


def test_parse_channels_output_reads_id_category_and_name():
    text = ("900000000000000010 | Ideas / swing-setups\n"
            "900000000000000011 | general\n"
            "\n"
            "some warning line\n")
    assert exporter.parse_channels_output(text) == [
        {"channel_id": "900000000000000010", "category": "Ideas", "name": "swing-setups"},
        {"channel_id": "900000000000000011", "category": "", "name": "general"},
    ]


# -- run_cli creationflags (#242): only the version probe asks for a hidden console window ------------------

def _fake_exe(tmp_path, name="dce", content=""):
    exe = tmp_path / name
    exe.write_text(content)
    return exe


def test_run_cli_passes_no_creationflags_by_default(monkeypatch, tmp_path):
    """The export and discover jobs must behave exactly as before: creationflags stays 0."""
    monkeypatch.setenv("RC_DCE_CLI", str(_fake_exe(tmp_path)))
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(exporter.subprocess, "run", fake_run)
    exporter.run_cli(["channels"])
    assert seen["creationflags"] == 0


def test_run_cli_passes_creationflags_through_to_subprocess(monkeypatch, tmp_path):
    monkeypatch.setenv("RC_DCE_CLI", str(_fake_exe(tmp_path)))
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(exporter.subprocess, "run", fake_run)
    exporter.run_cli(["--version"], creationflags=0x08000000)
    assert seen["creationflags"] == 0x08000000


def test_run_cli_creationflags_is_keyword_only():
    import inspect
    param = inspect.signature(exporter.run_cli).parameters["creationflags"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY and param.default == 0


# -- version parsing and the minimum -------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("v2.48.0", (2, 48, 0)),
    ("2.45", (2, 45)),
    ("2.48.1+abc123", (2, 48, 1)),
    ("DiscordChatExporter.Cli v2.43.3", (2, 43, 3)),
    ("  v2.48  \n", (2, 48)),
    ("no version here", None),
    ("v2", None),
    ("", None),
    (None, None),
])
def test_parse_version(text, expected):
    assert exporter.parse_version(text) == expected


def test_the_minimum_is_2_45():
    assert exporter.MIN_DCE_VERSION == (2, 45)


@pytest.mark.parametrize("version, ok", [
    ((2, 44, 9), False),
    ((2, 44), False),
    ((2, 45), True),
    ((2, 45, 0), True),
    ((2, 48, 0), True),
    ((3, 0), True),
    ((1, 99, 9), False),
])
def test_version_ok_boundary(version, ok):
    assert exporter.version_ok(version) is ok


# -- cli_version: one spawn per exe file state ---------------------------------------------------------------

class _Probe:
    """A stand-in for ``exporter.run_cli`` that records each call."""

    def __init__(self, stdout="v2.48.0\n", stderr="", returncode=0, exc=None, delay=0.0):
        self.calls = []
        self.stdout, self.stderr, self.returncode, self.exc, self.delay = stdout, stderr, returncode, exc, delay

    def __call__(self, args, env=None, timeout=600, *, creationflags=0, scrub_secrets=False):
        self.calls.append({"args": list(args), "env": env, "timeout": timeout, "creationflags": creationflags,
                           "scrub_secrets": scrub_secrets})
        if self.delay:
            time.sleep(self.delay)
        if self.exc:
            raise self.exc
        return exporter.CliResult(self.returncode, self.stdout, self.stderr)


@pytest.fixture
def probe(monkeypatch, tmp_path):
    """RC_DCE_CLI points at a real file; ``exporter.run_cli`` is replaced by a recording probe."""
    exe = _fake_exe(tmp_path, content="exe-v1")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    p = _Probe()
    monkeypatch.setattr(exporter, "run_cli", p)
    p.exe = exe
    return p


def test_cli_version_asks_only_for_the_version_with_no_window_and_a_10_second_timeout(probe):
    got = exporter.cli_version()
    assert got.version == (2, 48, 0) and got.error == "" and got.text == "v2.48.0"
    assert len(probe.calls) == 1
    call = probe.calls[0]
    assert call["args"] == ["--version"]
    assert call["timeout"] == 10
    assert call["env"] is None, "the probe adds no env of its own"
    assert call["scrub_secrets"] is True, "the probe's inherited env must be stripped of keys and tokens"


def test_cli_version_asks_for_no_console_window_when_the_platform_has_the_flag(probe, monkeypatch):
    """Windows has ``subprocess.CREATE_NO_WINDOW``; Linux does not. Give the module the flag so the test
    does not depend on the platform, and expect exactly that value to reach ``run_cli``."""
    monkeypatch.setattr(exporter.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    exporter.cli_version()
    assert probe.calls[0]["creationflags"] == 0x08000000


def test_cli_version_passes_zero_when_the_platform_has_no_such_flag(probe, monkeypatch):
    monkeypatch.delattr(exporter.subprocess, "CREATE_NO_WINDOW", raising=False)
    exporter.cli_version()
    assert probe.calls[0]["creationflags"] == 0


def test_cli_version_is_cached_per_file_state(probe):
    first = exporter.cli_version()
    second = exporter.cli_version()
    assert first == second and len(probe.calls) == 1


def test_cli_version_runs_again_when_the_file_changes_size(probe):
    exporter.cli_version()
    st = probe.exe.stat()
    probe.exe.write_text("a-longer-replacement-exe")
    os.utime(probe.exe, ns=(st.st_atime_ns, st.st_mtime_ns))     # same mtime, new size
    exporter.cli_version()
    assert len(probe.calls) == 2


def test_cli_version_runs_again_when_the_file_changes_mtime(probe):
    exporter.cli_version()
    st = probe.exe.stat()
    os.utime(probe.exe, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))   # same size, new mtime
    exporter.cli_version()
    assert len(probe.calls) == 2


def test_cli_version_runs_again_for_another_path(probe, monkeypatch, tmp_path):
    exporter.cli_version()
    st = probe.exe.stat()
    other = _fake_exe(tmp_path, name="other", content="exe-v1")        # same size ...
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))               # ... same mtime, only the path differs
    monkeypatch.setenv("RC_DCE_CLI", str(other))
    exporter.cli_version()
    assert len(probe.calls) == 2


@pytest.mark.parametrize("probe_kw, reason", [
    ({"exc": exporter.ExporterError("DiscordChatExporter timed out after 10s")}, "timed out"),
    ({"exc": exporter.ExporterError("could not start DiscordChatExporter: [Errno 8] Exec format error")},
     "could not start"),
    ({"returncode": 3, "stdout": "v2.48.0\n"}, "exited with code 3"),
    ({"stdout": "usage: dce [command]", "returncode": 0}, "no version number"),
    ({"stdout": "", "stderr": "", "returncode": 0}, "no version number"),
])
def test_a_broken_exe_is_unknown_with_a_reason_and_the_failure_is_cached(probe, probe_kw, reason):
    for key, value in probe_kw.items():
        setattr(probe, key, value)
    first = exporter.cli_version()
    assert first.version is None and reason in first.error
    assert exporter.cli_version() == first
    assert len(probe.calls) == 1, "a failed probe must not be retried on every Refresh"


def test_the_version_is_read_from_stderr_when_stdout_is_empty(probe):
    probe.stdout, probe.stderr = "", "v2.46.1\n"
    assert exporter.cli_version().version == (2, 46, 1)


def test_an_unset_path_is_unknown_and_never_starts_the_exe(monkeypatch):
    monkeypatch.delenv("RC_DCE_CLI", raising=False)
    boom = _Probe(exc=AssertionError("must not start the exe"))
    monkeypatch.setattr(exporter, "run_cli", boom)
    got = exporter.cli_version()
    assert got.version is None and "RC_DCE_CLI" in got.error
    assert boom.calls == [] and exporter._VERSION_CACHE == {}


def test_a_stat_failure_is_unknown_and_not_cached(monkeypatch):
    class Vanishing:
        def stat(self):
            raise FileNotFoundError("the file vanished between the checks")

    p = _Probe()
    monkeypatch.setattr(exporter, "run_cli", p)
    monkeypatch.setattr(exporter, "cli_path", lambda: Vanishing())
    got = exporter.cli_version()
    assert got.version is None and "vanished" in got.error
    assert p.calls == [] and exporter._VERSION_CACHE == {}


def test_two_threads_start_the_exe_once(probe):
    probe.delay = 0.3
    results = []
    threads = [threading.Thread(target=lambda: results.append(exporter.cli_version())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(results) == 2 and results[0] == results[1]
    assert len(probe.calls) == 1


# -- the version probe starts without secrets in its environment (#242 review) ---------------------------------

TOKEN = "SENTINEL-TOKEN-5c2e"
KEY = "SENTINEL-KEY-91ab"


@pytest.fixture
def secret_env(monkeypatch, tmp_path):
    """A process env full of secrets, RC_DCE_CLI pointing at a real file, and ``subprocess.run`` recording the env."""
    exe = _fake_exe(tmp_path, content="exe-v1")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", KEY)
    monkeypatch.setenv("SOME_SERVICE_TOKEN", TOKEN)
    monkeypatch.setenv("lower_case_api_key", KEY)          # Windows env names are case-insensitive
    monkeypatch.setenv("RC_STORE_DIR", "keep-me")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="v2.48.0\n", stderr="")

    monkeypatch.setattr(exporter.subprocess, "run", fake_run)
    return seen


def _secret_names(env):
    return sorted(k for k in env if k.upper().endswith(("_KEY", "_TOKEN")))


def test_the_version_probe_does_not_hand_the_token_or_any_key_to_the_exe(secret_env):
    got = exporter.cli_version()
    assert got.version == (2, 48, 0)
    env = secret_env["env"]
    assert _secret_names(env) == [], "no *_KEY / *_TOKEN variable may reach the probe"
    assert "DISCORD_TOKEN" not in env
    assert TOKEN not in env.values() and KEY not in env.values()
    assert env["RC_STORE_DIR"] == "keep-me" and ("PATH" in env or sys.platform == "win32")


def test_run_cli_without_scrub_secrets_still_hands_the_whole_env_to_the_jobs(secret_env):
    """The export and discover jobs are unchanged: they inherit the process env, as before."""
    exporter.run_cli(["channels"])
    env = secret_env["env"]
    assert env["DISCORD_TOKEN"] == TOKEN and env["UNUSUAL_WHALES_API_KEY"] == KEY


def test_run_cli_scrub_secrets_drops_key_and_token_variables_in_any_case(secret_env):
    exporter.run_cli(["--version"], scrub_secrets=True)
    env = secret_env["env"]
    assert _secret_names(env) == []
    assert TOKEN not in env.values() and KEY not in env.values()


def test_run_cli_scrub_secrets_keeps_an_explicit_env_the_caller_asks_for(secret_env):
    exporter.run_cli(["export"], env={"DISCORD_TOKEN": "explicit"}, scrub_secrets=True)
    env = secret_env["env"]
    assert env["DISCORD_TOKEN"] == "explicit" and "UNUSUAL_WHALES_API_KEY" not in env


def test_run_cli_scrub_secrets_is_keyword_only():
    import inspect
    param = inspect.signature(exporter.run_cli).parameters["scrub_secrets"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY and param.default is False
