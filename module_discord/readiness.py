"""Is the Discord export ready to run? The data behind the Settings "Discord export readiness" card.

Four checks, in the order the user fixes them: the DiscordChatExporter path, its version, the Discord token
and the server id. It reads the environment and the CLI file, and asks the exe for ``--version`` through
``exporter.cli_version`` (cached per exe file state, offline, started without keys or tokens in its
environment). It makes no network call, opens no store, reads the token only to see that it is set, and
never puts the token or the server id into its result: only whether each is set.
"""
from __future__ import annotations

import os

from RevenueChaser.core import credentials
from RevenueChaser.module_discord import exporter


def _dotted(version) -> str:
    return ".".join(str(n) for n in version)


def _check(key: str, label: str, state: str, detail: str) -> dict:
    return {"key": key, "label": label, "state": state, "detail": detail}


def _cli_check() -> dict:
    problem = credentials.dce_cli_problem()
    if problem is not None:
        return _check("cli", "DCE CLI path (RC_DCE_CLI)", "bad", problem)
    return _check("cli", "DCE CLI path (RC_DCE_CLI)", "ok", (os.getenv("RC_DCE_CLI") or "").strip())


def _version_check(cli_ok: bool, minimum: str) -> dict:
    label = "DCE version"
    if not cli_ok:
        return _check("version", label, "skipped", "Needs the CLI path above first.")
    found = exporter.cli_version()
    if found.version is None:
        return _check("version", label, "unknown", f"Could not read the version: {found.error}")
    shown = _dotted(found.version)
    if exporter.version_ok(found.version):
        return _check("version", label, "ok", f"{shown} (needs {minimum} or newer)")
    return _check("version", label, "bad",
                  f"{shown} is too old: needs {minimum} or newer. Older versions fail an Update when nothing new "
                  "was posted. Download a newer DiscordChatExporter and point RC_DCE_CLI at it.")


def _token_check() -> dict:
    label = f"Discord token ({credentials.get('discord').env_var})"
    if not credentials.key_for("discord"):
        return _check("token", label, "bad", credentials.missing_message("discord"))
    return _check("token", label, "ok",
                  f"{credentials.get('discord').env_var} is set. Its value is never shown or checked here: "
                  "only a Discover or Update, which needs the network, tells whether Discord accepts it.")


def _guild_check() -> dict:
    label = "Server id (RC_DISCORD_GUILD_ID)"
    problem = credentials.discord_guild_problem()
    if problem is not None:
        return _check("guild", label, "bad", problem)
    return _check("guild", label, "ok", "RC_DISCORD_GUILD_ID is set.")


def collect() -> dict:
    """``{"ready", "min_version", "checks": [{key, label, state, detail}]}``; ``state`` is ok, bad, unknown or skipped.

    ``ready`` is true only when all four checks are ``ok``.
    """
    minimum = _dotted(exporter.MIN_DCE_VERSION)
    cli = _cli_check()
    checks = [cli, _version_check(cli["state"] == "ok", minimum), _token_check(), _guild_check()]
    return {"ready": all(c["state"] == "ok" for c in checks), "min_version": minimum, "checks": checks}
