"""The DiscordChatExporter (DCE) CLI seam.

``run_cli`` is the ONLY place that starts the executable; tests monkeypatch it, so nothing
in the suite touches the network or the exe. The user token is handed to the child process
through its environment (``DISCORD_TOKEN``), never as a command-line argument, so it does
not show up in process listings, logs or error text. Output is decoded as UTF-8 with ``errors="replace"``:
on a Turkish Windows box DCE can write cp857, which must never crash the job.

Needs DiscordChatExporter >= ``MIN_DCE_VERSION``: older versions exit non-zero on an empty ``--after``
window (DCE commit 7add81a), which an incremental update hits whenever nothing new was posted.

``cli_version`` is the one other caller of ``run_cli`` outside the jobs: the Settings readiness card asks
the exe for ``--version`` (offline, no keys or tokens in its environment), at most once per exe file state,
never on every Refresh.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

# Pinned by docs/data_architecture.md (core/tests/test_doc_constants.py compares the two).
MIN_DCE_VERSION = (2, 45)


class ExporterError(RuntimeError):
    pass


@dataclass(frozen=True)
class CliResult:
    returncode: int
    stdout: str
    stderr: str


def cli_path() -> Path:
    raw = (os.getenv("RC_DCE_CLI") or "").strip()
    if not raw:
        raise ExporterError("RC_DCE_CLI is not set; point it at the DiscordChatExporter CLI in .env")
    p = Path(raw)
    if not p.is_file():
        raise ExporterError(f"RC_DCE_CLI is not a file: {p}")
    return p


_SECRET_SUFFIXES = ("_KEY", "_TOKEN")


def _without_secrets(environ) -> dict:
    """``environ`` minus every ``*_KEY`` / ``*_TOKEN`` variable (``DISCORD_TOKEN`` included), in any letter case."""
    return {k: v for k, v in environ.items() if not k.upper().endswith(_SECRET_SUFFIXES)}


def run_cli(args: list[str], env: dict | None = None, timeout: float = 600, *,
            creationflags: int = 0, scrub_secrets: bool = False) -> CliResult:
    """Run the DCE CLI with ``args``; ``env`` is merged over the process environment.

    ``creationflags`` goes to ``subprocess.run`` as is (Windows only; 0 elsewhere). ``scrub_secrets`` drops every
    ``*_KEY`` / ``*_TOKEN`` variable from the inherited environment (an ``env`` the caller passes is kept). Only the
    version probe sets either (no console window, no secrets); the export and discover jobs keep the defaults.
    """
    exe = cli_path()
    full_env = {**(_without_secrets(os.environ) if scrub_secrets else os.environ), **(env or {})}
    secrets = [v for k, v in (env or {}).items() if v and "TOKEN" in k.upper()]
    try:
        proc = subprocess.run([str(exe), *args], env=full_env, capture_output=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, shell=False, check=False, creationflags=creationflags)
    except subprocess.TimeoutExpired as exc:
        raise ExporterError(f"DiscordChatExporter timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        msg = str(exc)
        for s in secrets:
            msg = msg.replace(s, "***")
        raise ExporterError(f"could not start DiscordChatExporter: {msg}") from None
    return CliResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def export_args(channel_id: str, out_path: str, after_id: str | None = None,
                media_dir: str | None = None) -> list[str]:
    """CLI arguments for one channel export (JSON). The token is NOT an argument."""
    args = ["export", "-c", str(channel_id), "-f", "Json", "-o", str(out_path)]
    if after_id:
        args += ["--after", str(after_id)]
    if media_dir:
        args += ["--media", "--reuse-media", "--media-dir", str(media_dir)]
    return args


_CHANNEL_LINE = re.compile(r"^\s*(\d{5,})\s*\|\s*(.+?)\s*$")


def parse_channels_output(text: str) -> list[dict]:
    """Parse ``channels`` output lines: ``<id> | <Category> / <name>`` (category optional).

    Lines that do not start with an id and a pipe (warnings, blanks) are ignored.
    """
    out = []
    for line in text.splitlines():
        m = _CHANNEL_LINE.match(line)
        if not m:
            continue
        label = m.group(2)
        category, _, name = label.rpartition(" / ")
        out.append({"channel_id": m.group(1), "category": category.strip(), "name": name.strip()})
    return out


_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_VERSION_TIMEOUT = 10


def parse_version(text) -> tuple[int, ...] | None:
    """The first ``N.N`` or ``N.N.N`` in ``text`` (``v2.48.0``, ``2.48.1+abc``), else ``None``."""
    m = _VERSION_RE.search(text or "")
    return tuple(int(g) for g in m.groups() if g is not None) if m else None


def version_ok(version) -> bool:
    """Whether a parsed version is at least ``MIN_DCE_VERSION`` (major and minor decide)."""
    return tuple(version[:2]) >= MIN_DCE_VERSION


@dataclass(frozen=True)
class CliVersion:
    """What ``cli_version`` found: the parsed version, or why there is none."""
    version: tuple[int, ...] | None
    text: str       # the first line the CLI printed
    error: str      # why ``version`` is ``None``; empty otherwise


# One probe per exe file state, shared by every Settings Refresh of this process. A failed probe is cached
# too, so a hanging exe costs one timeout, not one per Refresh; a new file or a dashboard restart re-checks.
_VERSION_CACHE: dict[tuple[str, int, int], CliVersion] = {}
_VERSION_LOCK = threading.Lock()


def _probe_version() -> CliVersion:
    try:
        res = run_cli(["--version"], timeout=_VERSION_TIMEOUT, scrub_secrets=True,
                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except ExporterError as exc:
        return CliVersion(None, "", str(exc))
    lines = (res.stdout or res.stderr or "").strip().splitlines()
    text = lines[0].strip()[:80] if lines else ""
    if res.returncode != 0:
        return CliVersion(None, text, f"DiscordChatExporter exited with code {res.returncode}")
    version = parse_version(text)
    if version is None:
        return CliVersion(None, text, f"no version number in its output: {text!r}")
    return CliVersion(version, text, "")


def cli_version() -> CliVersion:
    """The DCE exe's version, from ``<exe> --version``, cached per file state.

    The exe is started offline, with every ``*_KEY`` / ``*_TOKEN`` variable (``DISCORD_TOKEN`` included) removed from
    its environment, and with no console window on Windows.

    Never raises. A path that is unset, or a file that vanishes before ``stat``, answers ``unknown`` and is not
    cached; every other failure is cached with the file state.
    """
    try:
        path = cli_path()
        st = path.stat()
    except (ExporterError, OSError) as exc:
        return CliVersion(None, "", str(exc))
    key = (str(path), st.st_size, st.st_mtime_ns)
    with _VERSION_LOCK:      # held across the probe: two concurrent Refreshes start the exe once
        found = _VERSION_CACHE.get(key)
        if found is None:
            found = _VERSION_CACHE[key] = _probe_version()
    return found
