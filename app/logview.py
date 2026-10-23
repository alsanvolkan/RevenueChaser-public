"""Read-only tails of the dashboard's own log files, for the Logs tab (``/view/logs``, ``GET /api/logs``).

start.ps1 starts the dashboard hidden, with stdout in ``dashboard.log`` and stderr in ``dashboard.err.log``
(``logging`` and werkzeug's request lines go to stderr, and so do the stacks ``PYTHONFAULTHANDLER`` prints on a
fatal crash), and writes its own messages to ``launcher.log``. All three live in ``config.LOG_DIR``
(``.cache/logs/``), and each start keeps the previous run as ``*.prev.log``. Only these files can be read: the
browser names a source key, never a path.

A log line is untrusted text: any web page can make the browser request ``http://127.0.0.1:8000/<markup>``, and
werkzeug logs the path. The page puts lines in with ``textContent`` and its first payload through
``html_safe.script_json``.
"""
from __future__ import annotations

import codecs
import logging
import os
import re
from datetime import datetime

from markupsafe import Markup

from RevenueChaser.core import config, templating
from RevenueChaser.core.html_safe import script_json

# source key -> (label, file name)
SOURCES = {
    "server": ("Server", "dashboard.err.log"),
    "output": ("Output", "dashboard.log"),
    "launcher": ("Launcher", "launcher.log"),
}
HINTS = {
    "server": "Requests, warnings and errors (the dashboard's stderr).",
    "output": "What the dashboard printed (stdout).",
    "launcher": "What start.ps1 said while starting the dashboard.",
}
RUNS = ("current", "prev")
DEFAULT_TAIL = 400
MAX_TAIL = 2000
READ_BYTES = 256 * 1024
POLL_PATH = "/api/logs"
# Request lines this tab must not show. Its own poll, plus any other tab that polls and would otherwise fill
# the log it shows — the Runs tab registers itself with ``quiet_path`` (#26 PL5-b).
POLL_PATHS = {POLL_PATH}
# werkzeug colours its request lines (304, 404, 5xx) even when stderr is a file; the tab shows plain text.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def prev_name(name: str) -> str:
    """``dashboard.err.log`` -> ``dashboard.err.prev.log`` (what start.ps1's Keep-PreviousRun renames it to)."""
    return name[:-len(".log")] + ".prev.log"


def log_path(src: str, run: str = "current"):
    """The file for a source key and run, or None when either is unknown."""
    if src not in SOURCES or run not in RUNS:
        return None
    name = SOURCES[src][1]
    if run == "prev":
        name = prev_name(name)
    return config.LOG_DIR / name


def read_tail(path, lines: int, nbytes: int | None = None) -> list[str]:
    """The last ``lines`` lines of a file, reading at most ``nbytes`` from its end. [] when it cannot be read."""
    nbytes = READ_BYTES if nbytes is None else nbytes
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            start = max(0, fh.tell() - nbytes)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    if start == 0 and data.startswith(codecs.BOM_UTF8):
        data = data[len(codecs.BOM_UTF8):]  # PowerShell 5.1's Add-Content -Encoding UTF8 writes one
    out = _ANSI.sub("", data.decode("utf-8", errors="replace")).splitlines()
    if start > 0 and out:
        out = out[1:]  # the first line was cut by the seek
    return out[-lines:] if lines > 0 else []


def clamp_tail(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TAIL
    return max(1, min(n, MAX_TAIL))


def read(src: str, run: str = "current", tail=DEFAULT_TAIL) -> dict:
    """What /api/logs returns for one source. Raises ValueError for an unknown source or run."""
    path = log_path(src, run)
    if path is None:
        raise ValueError(f"unknown log source {src!r} / run {run!r}")
    out = {"src": src, "run": run, "file": path.name, "exists": False, "size": 0, "modified": None, "lines": []}
    try:
        st = path.stat()
    except OSError:
        return out
    out.update(exists=True, size=st.st_size,
               modified=datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
               lines=read_tail(path, clamp_tail(tail)))
    return out


def page_payload() -> dict:
    """What the Logs page is rendered from: the folder it names and the Server tail it shows first."""
    return {"log_dir": str(config.LOG_DIR), "initial": read("server", tail=1000)}


def render_page(payload: dict) -> str:
    sources = [{"key": key, "label": label} for key, (label, _name) in SOURCES.items()]
    # script_json output is the one trusted fragment: Markup keeps autoescape from turning its quotes into
    # entities, which an inline <script> would not decode.
    return templating.render("logs.html.j2", log_dir=payload.get("log_dir") or "", sources=sources,
                             initial_json=Markup(script_json(payload.get("initial"))),
                             hints_json=Markup(script_json(HINTS)))


class SkipLogPolling(logging.Filter):
    """Drop werkzeug's request lines for the polling paths: a tab polls every few seconds and would
    otherwise fill the log this one shows."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed record is not ours to judge
            return True
        return not any(path in message for path in POLL_PATHS)


def quiet_polling(logger_name: str = "werkzeug") -> None:
    logger = logging.getLogger(logger_name)
    if not any(isinstance(f, SkipLogPolling) for f in logger.filters):
        logger.addFilter(SkipLogPolling())


def quiet_path(path: str) -> None:
    """Keep another polling tab's own requests out of the log as well (one filter serves them all)."""
    POLL_PATHS.add(path)
