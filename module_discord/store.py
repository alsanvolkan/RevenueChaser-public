"""Reads and writes of ``discord.duckdb`` (tables: ``discord_channels``, ``discord_messages``,
``discord_export_runs``). All writes go through ``rawstore`` (atomic, retried on lock)."""
from __future__ import annotations

import json
import time

from RevenueChaser.core import rawstore
from RevenueChaser.module_discord.loader import ParsedExport

_JSON_COLUMNS = ("attachments", "embeds", "reactions", "mentions")


class DiscordStoreError(RuntimeError):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _stored_ids(table: str, key: str, ids: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        rows = rawstore.query(f"SELECT {key} FROM {table} WHERE {key} IN ({marks})",
                              chunk, db="discord")
        found.update(r[key] for r in rows)
    return found


def save_export(parsed: ParsedExport) -> dict:
    """Upsert one channel export; returns ``{channel_id, messages, new}``."""
    ch = parsed.channel
    existing = rawstore.query("SELECT enabled, discovered_at FROM discord_channels "
                              "WHERE channel_id = ?", [ch["channel_id"]], db="discord")
    prev = existing[0] if existing else {}
    rawstore.write_rows("discord_channels", [{
        **ch,
        "enabled": prev.get("enabled", False) if prev.get("enabled") is not None else False,
        "discovered_at": prev.get("discovered_at") or _now(),
        "last_export_at": _now(),
    }])
    ids = [m["message_id"] for m in parsed.messages]
    already = _stored_ids("discord_messages", "message_id", ids)
    if parsed.messages:
        rawstore.write_rows("discord_messages", parsed.messages)
    return {"channel_id": ch["channel_id"], "messages": len(parsed.messages),
            "new": len(set(ids) - already)}


def messages(channel_id: str) -> list[dict]:
    """A channel's messages, oldest first, JSON columns as objects, without the ``raw`` blob.

    The full DCE object of one message is ``raw_message``. (``rawstore.get_all`` would
    also work now: the tables are ``text_verbatim``.)
    """
    rows = rawstore.query(
        "SELECT * EXCLUDE (raw) FROM discord_messages WHERE channel_id = ? ORDER BY ts, message_id",
        [channel_id], db="discord")
    for r in rows:
        for col in _JSON_COLUMNS:
            if isinstance(r.get(col), str):
                r[col] = json.loads(r[col])
    return rows


def raw_message(message_id: str) -> dict | None:
    """The full DiscordChatExporter object of one message, or ``None``."""
    row = rawstore.get("discord_messages", str(message_id))
    return row["raw"] if row else None


def last_message_id(channel_id: str) -> str | None:
    """The newest stored message id of a channel (numeric snowflake order), or ``None``."""
    rows = rawstore.query(
        "SELECT message_id FROM discord_messages WHERE channel_id = ? "
        "ORDER BY CAST(message_id AS HUGEINT) DESC LIMIT 1", [channel_id], db="discord")
    return rows[0]["message_id"] if rows else None


def channels(enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM discord_channels" + (" WHERE enabled" if enabled_only else "")
    return rawstore.query(sql + " ORDER BY channel_id", db="discord")


def upsert_discovered(found: list[dict]) -> int:
    """Store channels from a Discover run; new ones start disabled, old flags are kept.

    Returns the number of channels that were not stored before.
    """
    ids = [c["channel_id"] for c in found]
    prev = {r["channel_id"]: r for r in channels()}
    rows = []
    for c in found:
        old = prev.get(c["channel_id"], {})
        rows.append({
            "channel_id": c["channel_id"],
            "guild_id": c.get("guild_id") or old.get("guild_id"),
            "guild_name": c.get("guild_name") or old.get("guild_name"),
            "name": c.get("name"),
            "category": c.get("category"),
            "topic": old.get("topic"),
            "kind": old.get("kind"),
            "enabled": bool(old.get("enabled")),
            "discovered_at": old.get("discovered_at") or _now(),
            "last_export_at": old.get("last_export_at"),
        })
    if rows:
        rawstore.write_rows("discord_channels", rows)
    return len(set(ids) - set(prev))


def set_enabled(channel_ids: list[str], enabled: bool) -> None:
    known = {c["channel_id"] for c in channels()}
    unknown = [c for c in channel_ids if c not in known]
    if unknown:
        raise DiscordStoreError(f"unknown channel(s): {', '.join(unknown)}")
    rows = [{**c, "enabled": enabled} for c in channels() if c["channel_id"] in set(channel_ids)]
    rawstore.write_rows("discord_channels", rows)


def record_run(run: dict) -> None:
    rawstore.write_rows("discord_export_runs", [run])


# ── board reads (S2) ─────────────────────────────────────────────────────────

def channel_stats() -> list[dict]:
    """Every channel with its stored message count and newest message time."""
    return rawstore.query(
        "SELECT c.*, coalesce(m.n, 0) AS message_count, m.last_ts AS last_ts "
        "FROM discord_channels c LEFT JOIN ("
        "  SELECT channel_id, count(*) AS n, max(ts) AS last_ts FROM discord_messages GROUP BY channel_id"
        ") m ON m.channel_id = c.channel_id "
        "ORDER BY coalesce(c.guild_name, ''), coalesce(c.category, ''), coalesce(c.name, ''), c.channel_id",
        db="discord")


def _fold(text: str) -> str:
    """Case-fold for search, treating the Turkish dotted and dotless i as plain i."""
    return text.lower().replace("i\u0307", "i").replace("\u0131", "i")


def last_export_started(channel_ids: list[str]) -> dict[str, str]:
    """``{channel_id: newest export-run start}`` (any status) for the given channels."""
    if not channel_ids:
        return {}
    marks = ",".join("?" * len(channel_ids))
    rows = rawstore.query(
        f"SELECT channel_id, max(started_at) AS t FROM discord_export_runs WHERE channel_id IN ({marks}) "
        "GROUP BY channel_id", list(channel_ids), db="discord")
    return {r["channel_id"]: r["t"] for r in rows if r.get("t")}


def _like_literal(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def feed(channel_id: str | None = None, q: str = "", ts_from: str | None = None,
         ts_to: str | None = None, limit: int = 200, offset: int = 0) -> tuple[list[dict], int]:
    """Newest-first page of messages plus the total that match. ``ts_to`` is exclusive."""
    where, params = [], []
    if channel_id:
        where.append("m.channel_id = ?")
        params.append(channel_id)
    if q:
        where.append("replace(lower(m.content), 'ı', 'i') LIKE ? ESCAPE '\\'")
        params.append("%" + _like_literal(_fold(q)) + "%")
    if ts_from:
        where.append("m.ts >= ?")
        params.append(ts_from)
    if ts_to:
        where.append("m.ts < ?")
        params.append(ts_to)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    counted = rawstore.query(f"SELECT count(*) AS n FROM discord_messages m{clause}", params, db="discord")
    total = counted[0]["n"] if counted else 0          # no discord.duckdb yet (auto-ensure off)
    rows = rawstore.query(
        "SELECT m.* EXCLUDE (raw), c.name AS channel_name FROM discord_messages m "
        f"LEFT JOIN discord_channels c ON c.channel_id = m.channel_id{clause} "
        "ORDER BY m.ts DESC, CAST(m.message_id AS HUGEINT) DESC LIMIT ? OFFSET ?",
        params + [int(limit), int(offset)], db="discord")
    for r in rows:
        for col in _JSON_COLUMNS:
            if isinstance(r.get(col), str):
                r[col] = json.loads(r[col])
    return rows, int(total)


def message_previews(message_ids: list[str]) -> dict[str, dict]:
    """``{message_id: {author_name, content}}`` for the ids that are stored."""
    out: dict[str, dict] = {}
    ids = list(dict.fromkeys(message_ids))
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for r in rawstore.query(
                f"SELECT message_id, author_name, content FROM discord_messages "
                f"WHERE message_id IN ({marks})", chunk, db="discord"):
            out[r["message_id"]] = r
    return out


def last_sync() -> str | None:
    """Finish time of the newest successful export run, else the newest channel export."""
    rows = rawstore.query("SELECT max(finished_at) AS t FROM discord_export_runs WHERE status = 'ok'",
                          db="discord")
    if rows and rows[0]["t"]:
        return rows[0]["t"]
    rows = rawstore.query("SELECT max(last_export_at) AS t FROM discord_channels", db="discord")
    return rows[0]["t"] if rows and rows[0]["t"] else None
