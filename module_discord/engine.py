"""Discover and update: the two jobs behind the Discord archive.

Discover lists the guild's channels (new ones start disabled). Update exports every enabled
channel incrementally (``--after`` the last stored message id) and upserts the messages.
The only network path is the DCE CLI, reached through ``exporter.run_cli``.
"""
from __future__ import annotations

import os
import tempfile
import time
import uuid
from pathlib import Path

from RevenueChaser.core import config, credentials, job_control, job_lock
from RevenueChaser.module_discord import exporter, loader, store

_TOKEN_VAR = credentials.get("discord").env_var

# One Discord job at a time (#26): update and discover both write ``discord.duckdb`` and both drive the DCE CLI.
_LOCK = "discord"


class DiscordEngineError(RuntimeError):
    pass


def _holder_pid() -> int:
    """The PID holding the Discord lock: ``0`` when it is held but its owner cannot be read."""
    return dict(job_lock.holders()).get(_LOCK, 0)


def _require_token() -> dict:
    token = credentials.key_for("discord")
    if not token:
        raise DiscordEngineError(credentials.describe("discord"))
    problem = credentials.dce_cli_problem()
    if problem:
        raise DiscordEngineError(problem)
    return {_TOKEN_VAR: token}


def _guild_id() -> str:
    gid = (os.getenv("RC_DISCORD_GUILD_ID") or "").strip()
    if not gid:
        raise DiscordEngineError(
            "RC_DISCORD_GUILD_ID is not set. Put the server's id in .env (DiscordChatExporter's "
            "Token-and-IDs guide shows how to find it), then restart the dashboard.")
    return gid


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fail_text(res: exporter.CliResult, env: dict) -> str:
    text = (res.stderr or res.stdout or f"exit code {res.returncode}").strip()
    for secret in env.values():
        if secret:
            text = text.replace(secret, "***")
    return text[:500]


def discover() -> dict:
    """List the guild's channels and store the new ones (disabled).

    One Discord job at a time (#26): while another holds the ``"discord"`` lock this starts no subprocess, writes
    nothing, and returns ``{"busy": True, "holder_pid": pid, "guild_id": None, "channels": 0, "new": 0}``.
    """
    with job_lock.acquire(_LOCK, wait=0) as got:
        if not got:
            return {"busy": True, "holder_pid": _holder_pid(), "guild_id": None, "channels": 0, "new": 0}
        env = _require_token()
        guild = _guild_id()
        res = exporter.run_cli(["channels", "-g", guild], env=env, timeout=120)
        if res.returncode != 0:
            raise DiscordEngineError(f"channel discovery failed: {_fail_text(res, env)}")
        found = exporter.parse_channels_output(res.stdout)
        for c in found:
            c["guild_id"] = guild
        new = store.upsert_discovered(found)
        return {"guild_id": guild, "channels": len(found), "new": new}


def _export_channel(channel_id: str, env: dict, media: bool) -> dict:
    run = {"run_id": uuid.uuid4().hex, "channel_id": channel_id, "started_at": _now(),
           "finished_at": None, "after_id": store.last_message_id(channel_id),
           "message_count": 0, "status": "error", "detail": ""}
    try:
        with tempfile.TemporaryDirectory(prefix="rc-discord-") as tmp:
            out = Path(tmp) / f"{channel_id}.json"
            args = exporter.export_args(
                channel_id, str(out), after_id=run["after_id"],
                media_dir=str(config.DISCORD_MEDIA_DIR / channel_id) if media else None)
            res = exporter.run_cli(args, env=env, timeout=3600)
            if res.returncode != 0:
                raise DiscordEngineError(_fail_text(res, env))
            saved = store.save_export(loader.load_export_file(out))
        run.update(status="ok", message_count=saved["messages"], detail=f"{saved['new']} new")
        return {**saved, "ok": True}
    except (exporter.ExporterError, DiscordEngineError, loader.ExportFormatError) as exc:
        run["detail"] = str(exc)[:500]
        return {"ok": False, "messages": 0, "new": 0}
    finally:
        run["finished_at"] = _now()
        store.record_run(run)


def update(media: bool = False, channel_ids: list[str] | None = None) -> dict:
    """Export every enabled channel (or just ``channel_ids``); one failing channel does not stop the others.

    One Discord job at a time (#26): while another holds the ``"discord"`` lock this exports nothing and returns
    ``{"busy": True, "holder_pid": pid, "channels": 0, "messages": 0, "new": 0, "failed": []}``.
    """
    with job_lock.acquire(_LOCK, wait=0) as got:
        if not got:
            return {"busy": True, "holder_pid": _holder_pid(),
                    "channels": 0, "messages": 0, "new": 0, "failed": []}
        chans = store.channels(enabled_only=True)
        if channel_ids is not None:
            wanted = {str(c) for c in channel_ids}
            chans = [c for c in store.channels() if c["channel_id"] in wanted]
        if not chans:
            return {"channels": 0, "messages": 0, "new": 0, "failed": []}
        env = _require_token()
        total = {"channels": 0, "messages": 0, "new": 0, "failed": []}
        for c in chans:
            # Between channels. The per-channel CLI timeout (``exporter.run_cli``) stays; this deadline sits above it.
            job_control.checkpoint(f"Discord channel {c['channel_id']}")
            r = _export_channel(c["channel_id"], env, media)
            if r["ok"]:
                total["channels"] += 1
                total["messages"] += r["messages"]
                total["new"] += r["new"]
            else:
                total["failed"].append(c["channel_id"])
        return total
