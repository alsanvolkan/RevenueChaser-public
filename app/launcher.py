"""Start the dashboard with no console window (launch.pyw, which the desktop shortcut runs).

launch.pyw runs under the venv's pythonw.exe, which has no console. This runs start.ps1 hidden, so start.ps1
keeps the whole start logic and run_dashboard.bat still runs it in a visible window. start.ps1 writes its
messages to launcher.log (the Logs tab shows them); when it fails, this shows the last of them in a message box,
so a failed start is never silent.

PowerShell's output is not piped back: the dashboard start.ps1 starts would inherit the pipe's write end and
keep it open for its whole life, so waiting for the end of the output would wait for the dashboard to stop.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import logview

REPO = Path(__file__).resolve().parent.parent
TITLE = "Revenue Chaser"
START_TIMEOUT = 300   # well past start.ps1's own waits (stop.ps1 on an old dashboard, the new one answering)
BOX_LINES = 12
CREATE_NO_WINDOW = 0x08000000
IS_WINDOWS = os.name == "nt"


def start_command(repo: Path = REPO) -> list[str]:
    return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(Path(repo) / "start.ps1")]


def message_box(title: str, text: str) -> None:
    if not IS_WINDOWS:
        return
    import ctypes
    # MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST: a hidden launcher has no window to come to the front with
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 | 0x10000 | 0x40000)


def main(run=subprocess.run, alert=message_box) -> int:
    detail = ""
    try:
        code = run(start_command(REPO), cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=START_TIMEOUT,
                   creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0).returncode
    except subprocess.TimeoutExpired:
        code, detail = 1, f"start.ps1 did not finish within {START_TIMEOUT} s."
    except OSError as exc:
        code, detail = 1, f"PowerShell could not be started: {exc}"
    if code != 0:
        log = logview.log_path("launcher")
        lines = logview.read_tail(log, BOX_LINES)
        body = "\n".join(lines) if lines else f"start.ps1 stopped with exit code {code}."
        if detail:
            body = f"{body}\n\n{detail}" if lines else detail
        alert(TITLE, f"The dashboard did not start.\n\n{body}\n\nFull log: {log}\n"
                     "run_dashboard.bat runs the same start in a visible window.")
    return code
