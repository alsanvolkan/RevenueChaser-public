"""DiscordChatExporter JSON -> typed rows (pure; no store, no network)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class ExportFormatError(ValueError):
    """The export is not the DCE JSON shape this loader understands."""


@dataclass(frozen=True)
class ParsedExport:
    channel: dict
    messages: list[dict]


def _ts(value) -> str | None:
    """Normalise an ISO timestamp to sortable UTC text: ``YYYY-MM-DDTHH:MM:SS.ffffffZ``."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExportFormatError(f"bad timestamp {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _author_name(author: dict) -> str | None:
    return author.get("nickname") or author.get("name")


def _snowflake(value, what: str) -> str:
    text = str(value) if value is not None else ""
    if not text.isascii() or not text.isdigit():
        raise ExportFormatError(f"{what} is not a numeric id: {text[:40]!r}")
    return text


def _embed(e: dict) -> dict:
    return {
        "title": e.get("title"), "url": e.get("url"), "description": e.get("description"),
        "color": e.get("color"),
        "author": (e.get("author") or {}).get("name"),
        "footer": (e.get("footer") or {}).get("text"),
        "fields": [{"name": f.get("name"), "value": f.get("value"), "inline": bool(f.get("isInline"))}
                   for f in e.get("fields") or []],
        "images": [i.get("url") for i in e.get("images") or [] if i.get("url")],
        "thumbnail": (e.get("thumbnail") or {}).get("url"),
    }


def _message_row(channel_id: str, raw: dict, idx: int) -> dict:
    if not raw.get("id"):
        raise ExportFormatError(f"message {idx} has no id")
    try:
        message_id = _snowflake(raw["id"], "message id")
    except ExportFormatError as exc:
        raise ExportFormatError(f"message {idx}: {exc}") from None
    author = raw.get("author") or {}
    ref = raw.get("reference") or {}
    kind = raw.get("type") or "Default"
    ref_id = str(ref["messageId"]) if ref.get("messageId") else None
    ref_kind = ref.get("type") if ref_id else None
    content = raw.get("content") or ""
    if not content:
        # A forward keeps its text only in forwardedMessage.content.
        content = (raw.get("forwardedMessage") or {}).get("content") or ""
    return {
        "message_id": message_id,
        "channel_id": channel_id,
        "author_id": str(author["id"]) if author.get("id") else None,
        "author_name": _author_name(author),
        "is_bot": bool(author.get("isBot")),
        "kind": kind,
        "ts": _ts(raw.get("timestamp")),
        "edited_ts": _ts(raw.get("timestampEdited")),
        "content": content,
        "is_pinned": bool(raw.get("isPinned")),
        # only a real reply: pin notices and forwards also carry a reference
        "reply_to_id": ref_id if kind == "Reply" and ref_kind != "Forward" else None,
        "reference_id": ref_id,
        "reference_kind": ref_kind,
        "attachments": [
            {"id": a.get("id"), "file_name": a.get("fileName"),
             "size_bytes": a.get("fileSizeBytes"), "path": a.get("url")}
            for a in raw.get("attachments") or []],
        "embeds": [_embed(e) for e in raw.get("embeds") or []],
        "reactions": [
            {"emoji": (r.get("emoji") or {}).get("name"), "count": r.get("count")}
            for r in raw.get("reactions") or []],
        "mentions": [
            {"id": m.get("id"), "name": _author_name(m)} for m in raw.get("mentions") or []],
        "raw": raw,
    }


def parse_export(data: dict) -> ParsedExport:
    """Parse one DCE JSON document (one channel) into a channel row and message rows."""
    if not isinstance(data, dict):
        raise ExportFormatError("export is not a JSON object")
    for key in ("channel", "messages"):
        if key not in data:
            raise ExportFormatError(f"export has no '{key}'")
    if not isinstance(data["messages"], list):
        raise ExportFormatError("export 'messages' is not a list")
    ch = data["channel"] if isinstance(data["channel"], dict) else {}
    if not ch.get("id"):
        raise ExportFormatError("export channel has no id")
    try:
        _snowflake(ch["id"], "channel id")
    except ExportFormatError as exc:
        raise ExportFormatError(f"export channel: {exc}") from None
    guild = data.get("guild") or {}
    channel_id = str(ch["id"])
    channel = {
        "channel_id": channel_id,
        "guild_id": str(guild["id"]) if guild.get("id") else None,
        "guild_name": guild.get("name"),
        "name": ch.get("name"),
        "category": ch.get("category"),
        "topic": ch.get("topic"),
        "kind": ch.get("type"),
    }
    messages = [_message_row(channel_id, m, i) for i, m in enumerate(data["messages"])]
    return ParsedExport(channel=channel, messages=messages)


def load_export_file(path: str | Path) -> ParsedExport:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ExportFormatError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ExportFormatError(f"{path} is not valid JSON: {exc}") from exc
    return parse_export(data)
