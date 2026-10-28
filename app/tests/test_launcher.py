"""launch.pyw / app/launcher.py and the PowerShell scripts: start the dashboard with no console window,
never fail silently, stop only our own server.

The launcher is tested through a fake ``run`` and ``alert``; the scripts are Windows-only, so their
tests read the text (the main session checks the real behaviour on Windows).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from RevenueChaser.app import launcher, logview
from RevenueChaser.core import config

REPO = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


class FakeRun:
    def __init__(self, code=0, exc=None):
        self.code, self.exc, self.calls = code, exc, []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.code)


class Alerts(list):
    def __call__(self, title, text):
        self.append((title, text))


@pytest.fixture()
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    return tmp_path


# ── app/launcher.py ───────────────────────────────────────────────────────────

def test_start_command_runs_start_ps1_without_a_profile():
    cmd = launcher.start_command(REPO)
    assert cmd[0].lower().startswith("powershell")
    assert "-NoProfile" in cmd and "-File" in cmd
    assert cmd[cmd.index("-ExecutionPolicy") + 1] == "Bypass"
    assert cmd[-1] == str(REPO / "start.ps1")


def test_success_shows_nothing(log_dir):
    run, alerts = FakeRun(0), Alerts()
    assert launcher.main(run=run, alert=alerts) == 0
    assert alerts == []
    assert len(run.calls) == 1


def test_no_pipe_and_a_hidden_window_on_windows(log_dir, monkeypatch):
    """A pipe would hang: the server start.ps1 starts inherits its write end and never closes it."""
    monkeypatch.setattr(launcher, "IS_WINDOWS", True)
    run = FakeRun(0)
    launcher.main(run=run, alert=Alerts())
    _, kwargs = run.calls[0]
    for stream in ("stdin", "stdout", "stderr"):
        assert kwargs[stream] is subprocess.DEVNULL, stream
    assert kwargs["creationflags"] == 0x08000000  # CREATE_NO_WINDOW
    assert Path(kwargs["cwd"]) == REPO
    assert kwargs["timeout"] == launcher.START_TIMEOUT


def test_no_windows_flag_off_windows(log_dir, monkeypatch):
    monkeypatch.setattr(launcher, "IS_WINDOWS", False)
    run = FakeRun(0)
    launcher.main(run=run, alert=Alerts())
    assert run.calls[0][1]["creationflags"] == 0


def test_failure_shows_the_last_launcher_lines(log_dir):
    (log_dir / "launcher.log").write_text(
        "09:00:00 Starting the Revenue Chaser dashboard\n"
        "09:00:40 The dashboard did not answer on http://127.0.0.1:8000/ within 120 s\n", encoding="utf-8")
    alerts = Alerts()
    assert launcher.main(run=FakeRun(1), alert=alerts) == 1
    (title, text), = alerts
    assert title == launcher.TITLE
    assert "did not answer" in text
    assert str(log_dir / "launcher.log") in text, "the box says where the full log is"
    assert "run_dashboard.bat" in text, "and how to see the same start in a window"


def test_only_the_last_lines_go_in_the_box(log_dir):
    (log_dir / "launcher.log").write_text("".join(f"line {i}\n" for i in range(40)), encoding="utf-8")
    alerts = Alerts()
    launcher.main(run=FakeRun(1), alert=alerts)
    text = alerts[0][1]
    assert f"line {39}" in text and f"line {40 - launcher.BOX_LINES}" in text
    assert f"line {39 - launcher.BOX_LINES}\n" not in text


def test_failure_without_a_log_still_says_something(log_dir):
    alerts = Alerts()
    assert launcher.main(run=FakeRun(3), alert=alerts) == 3
    assert "exit code 3" in alerts[0][1]


def test_powershell_missing_is_reported(log_dir):
    alerts = Alerts()
    assert launcher.main(run=FakeRun(exc=FileNotFoundError("powershell.exe")), alert=alerts) == 1
    assert "PowerShell" in alerts[0][1]


def test_timeout_is_reported(log_dir):
    alerts = Alerts()
    exc = subprocess.TimeoutExpired("powershell.exe", launcher.START_TIMEOUT)
    assert launcher.main(run=FakeRun(exc=exc), alert=alerts) == 1
    assert f"{launcher.START_TIMEOUT} s" in alerts[0][1]


def test_message_box_is_a_no_op_off_windows(monkeypatch):
    monkeypatch.setattr(launcher, "IS_WINDOWS", False)
    launcher.message_box("Revenue Chaser", "text")  # must not touch ctypes.windll


def test_the_launcher_reads_the_log_start_ps1_writes():
    assert logview.SOURCES["launcher"][1] == "launcher.log"
    assert logview.log_path("launcher") == config.LOG_DIR / "launcher.log"


# ── launch.pyw ────────────────────────────────────────────────────────────────

def test_launch_pyw_calls_the_launcher_from_the_package_parent():
    text = _read("launch.pyw")
    assert "launcher.main()" in text
    assert "from RevenueChaser.app import launcher" in text
    # pythonw puts the script's own folder on sys.path; the package is that folder, so its parent must be there.
    assert "sys.path.insert(0" in text and "dirname(os.path.dirname(" in text


# ── start.ps1 ─────────────────────────────────────────────────────────────────

def _ready_loop(text: str) -> str:
    return text.split("$ready = $false", 1)[1]


def test_start_ps1_logs_to_the_files_the_logs_tab_reads():
    text = _read("start.ps1")
    assert 'Join-Path $cacheDir "logs"' in text and 'Join-Path $repo ".cache"' in text
    for _label, name in logview.SOURCES.values():
        assert f'"{name}"' in text, name
    assert ".prev.log" in text and "Keep-PreviousRun" in text
    assert "Add-Content" in text and "-Encoding UTF8" in text


def test_start_ps1_starts_the_dashboard_hidden_and_unbuffered():
    text = _read("start.ps1")
    assert '$env:PYTHONUNBUFFERED = "1"' in text      # the Logs tab sees print() output live
    assert '$env:PYTHONIOENCODING = "utf-8"' in text  # and reads it as UTF-8
    assert '$env:PYTHONFAULTHANDLER = "1"' in text    # a fatal crash still leaves every thread's stack
    assert "$env:PYTHONPATH = $root" in text
    assert "-WindowStyle Hidden" in text
    assert "-RedirectStandardOutput" in text and "-RedirectStandardError" in text
    assert '"RevenueChaser.app.dashboard"' in text
    assert "-WorkingDirectory $root" in text


def test_start_ps1_picks_the_interpreter_like_the_old_bat():
    text = _read("start.ps1")
    repo_venv = text.index('Join-Path $repo ".venv\\Scripts\\python.exe"')
    parent_venv = text.index('Join-Path $root ".venv\\Scripts\\python.exe"')
    path_python = text.index("Get-Command python")
    assert repo_venv < parent_venv < path_python


def _block(text: str, head: str) -> str:
    """The body of the PowerShell block that starts with ``head`` (up to its first closing brace line)."""
    return text.split(head, 1)[1].split("\n}", 1)[0].split("\n    }", 1)[0]


def test_start_ps1_fails_when_the_dashboard_never_answers():
    loop = _ready_loop(_read("start.ps1"))
    assert "exit 1" in _block(loop, "if (-not $ready) {"), "a server that never answers must fail the launcher"
    assert "exit 1" in _block(loop, "if ($proc.HasExited) {"), "a server that exits early (port taken) fails at once"
    assert "/manifest.json" in loop


def test_start_ps1_stops_the_old_dashboard_before_it_renames_the_logs():
    """A running server holds its log files open; Windows refuses to rename them until it is gone."""
    text = _read("start.ps1")
    stop = text.index('"stop.ps1"')
    assert stop < text.index("Keep-PreviousRun $out") < text.index("Start-Process -FilePath $python")


def test_start_ps1_leaves_chrome_and_the_stale_server_check_to_the_dashboard():
    text = _read("start.ps1").lower()
    assert "chrome" not in text, "main() opens the app window"
    assert "stop-process" not in text and "get-process" not in text, "stopping goes through stop.ps1"


def test_launcher_timeout_is_well_past_the_start_scripts_waits():
    ready = int(re.search(r"\$readySeconds = (\d+)", _read("start.ps1")).group(1))
    stop = int(re.search(r"AddSeconds\((\d+)\)", _read("stop.ps1")).group(1))
    assert launcher.START_TIMEOUT >= 2 * (ready + stop + 5)


# ── stop.ps1 ──────────────────────────────────────────────────────────────────

def test_stop_ps1_stops_only_the_dashboard_listener():
    text = _read("stop.ps1")
    assert "param([switch]$Force)" in text
    assert "Get-NetTCPConnection -LocalPort $port -State Listen" in text
    assert "RevenueChaser\\.app\\.dashboard" in text, "it checks the listener's command line"
    assert "Stop-Process -Id $procId" in text
    assert "-Name" not in text and "Get-Process" not in text, "never by process name"


def test_stop_ps1_refuses_unless_idle_and_fails_closed():
    """It asks ``/api/idle`` (PR 2, F8 A), which also sees job locks and store markers, not ``/api/jobs`` alone."""
    text = _read("stop.ps1")
    assert "/api/idle" in text and "/api/jobs" not in text
    assert "$state.idle -ne $true" in text, "anything but an explicit idle refuses"
    check = text.split("if (-not $Force)", 1)[1]
    catch = check.split("catch", 1)[1].split("}", 1)[0]
    assert "exit 1" in catch, "no answer means it cannot tell, so it does not stop"
    assert "exit 2" in check, "a running or queued job refuses the stop"


# ── run_dashboard.bat and the shortcut ────────────────────────────────────────

def test_the_bat_is_the_visible_fallback_of_the_same_start():
    text = _read("run_dashboard.bat")
    assert 'start.ps1"' in text and "-ExecutionPolicy Bypass" in text and "-NoProfile" in text
    assert "if errorlevel 1 pause" in text
    assert "RevenueChaser.app.dashboard" not in text, "the server no longer runs in the bat's window"


def test_the_shortcut_points_at_launch_pyw_with_pythonw():
    text = _read("scripts/make_shortcut.ps1")
    assert '"Revenue Chaser.lnk"' in text
    assert "pythonw.exe" in text and "launch.pyw" in text
    assert "$lnk.WorkingDirectory = $repo" in text
    assert "app\\static\\icons\\icon.ico" in text
    assert (REPO / "app" / "static" / "icons" / "icon.ico").exists()


@pytest.mark.parametrize("name", ["start.ps1", "stop.ps1", "scripts/make_shortcut.ps1", "run_dashboard.bat",
                                  "launch.pyw"])
def test_scripts_are_ascii(name):
    """Windows PowerShell 5.1 reads a BOM-less script as ANSI, so a non-ASCII character breaks it."""
    data = (REPO / name).read_bytes()
    assert all(b < 128 for b in data), name
