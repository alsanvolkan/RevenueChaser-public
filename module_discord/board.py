"""The Discord board's data layer: filters, paging, reply previews and media paths.

Reads ``discord.duckdb`` only. No network, no subprocess: a Refresh of the board is
exactly ``build_payload`` plus ``report.render_discord_board``.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from RevenueChaser.core import config
from RevenueChaser.module_discord import store

PAGE_SIZE = 200
_PREVIEW_CHARS = 140
# SVG is excluded on purpose: it can carry script, so it is a download, never a thumbnail.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


class BoardInputError(ValueError):
    """A filter value the board refuses (the route answers 400)."""


def is_image(file_name) -> bool:
    return bool(file_name) and Path(str(file_name)).suffix.lower() in _IMAGE_EXTS


_WIN_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL",
                           *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))})


def _windows_unsafe_segment(part: str) -> bool:
    """Windows drops a trailing dot or space and treats ``CON``/``NUL``/``COM1``... (with any extension) as devices."""
    return part.endswith((".", " ")) or part.split(".", 1)[0].rstrip(" ").upper() in _WIN_RESERVED


def url_rel_is_safe(rel) -> bool:
    """Pure string checks on a ``/discord/media/<rel>`` URL path, run before any filesystem call.

    On Windows ``Path.resolve()`` / ``is_file()`` on a UNC path (``\\\\host\\share``) opens an SMB
    connection to that host, so a hostile page must never get a path with a drive, a backslash or
    a colon that far.
    """
    rel = str(rel or "")
    if not rel or "\\" in rel or ":" in rel or "\x00" in rel or rel.startswith("/"):
        return False
    return all(part not in ("", ".", "..") and not _windows_unsafe_segment(part) for part in rel.split("/"))


def media_rel(path) -> str | None:
    """``path`` relative to ``DISCORD_MEDIA_DIR`` (posix), or ``None``.

    Only a real file that resolves (symlinks followed) inside the media tree counts;
    DCE writes absolute local paths, which are never trusted as given.
    """
    if not path:
        return None
    try:
        root = Path(config.DISCORD_MEDIA_DIR).resolve()
        p = Path(str(path))
        if not p.is_absolute():
            p = root / p
        resolved = p.resolve()
        if resolved == root or not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        return resolved.relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def _day(value: str, what: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise BoardInputError(f"bad {what}: expected YYYY-MM-DD") from None


def _ts_bound(d: date) -> str:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _preview(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS - 1] + "…"


def _message(row: dict, previews: dict) -> dict:
    parent = previews.get(row.get("reply_to_id")) if row.get("reply_to_id") else None
    return {
        "message_id": row["message_id"], "channel_id": row["channel_id"],
        "channel_name": row.get("channel_name"), "author_name": row.get("author_name"),
        "is_bot": bool(row.get("is_bot")), "ts": row.get("ts"),
        "ts_label": (row.get("ts") or "")[:16].replace("T", " "),
        "content": row.get("content") or "", "kind": row.get("kind"),
        "is_pinned": bool(row.get("is_pinned")), "reply_to_id": row.get("reply_to_id"),
        "reference_kind": row.get("reference_kind"),
        "reply_preview": ({"author": parent.get("author_name"), "text": _preview(parent.get("content"))}
                          if parent else None),
        "attachments": [{"file_name": a.get("file_name"), "size_bytes": a.get("size_bytes"),
                         "media_rel": media_rel(a.get("path")), "is_image": is_image(a.get("file_name"))}
                        for a in row.get("attachments") or []],
        "embeds": row.get("embeds") or [], "reactions": row.get("reactions") or [],
    }


def build_payload(channel: str | None = None, q: str = "", date_from: str = "",
                  date_to: str = "", page=1) -> dict:
    channel = (channel or "").strip()
    if channel and not (channel.isascii() and channel.isdigit()):
        raise BoardInputError("bad channel")
    q = (q or "").strip()[:200]
    date_from, date_to = (date_from or "").strip(), (date_to or "").strip()
    ts_from = _ts_bound(_day(date_from, "date_from")) if date_from else None
    ts_to = _ts_bound(_day(date_to, "date_to") + timedelta(days=1)) if date_to else None
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    _, total = store.feed(channel or None, q, ts_from, ts_to, limit=1, offset=0)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(page, 1), pages)
    rows, _ = store.feed(channel or None, q, ts_from, ts_to, limit=PAGE_SIZE,
                         offset=(page - 1) * PAGE_SIZE)
    previews = store.message_previews([r["reply_to_id"] for r in rows if r.get("reply_to_id")])
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_sync": store.last_sync(),
        "channels": [{k: c.get(k) for k in ("channel_id", "guild_id", "guild_name", "name", "category",
                                            "enabled", "message_count", "last_ts")}
                     | {"enabled": bool(c.get("enabled")), "message_count": int(c["message_count"] or 0)}
                     for c in store.channel_stats()],
        "filters": {"channel": channel, "q": q, "date_from": date_from, "date_to": date_to},
        "messages": [_message(r, previews) for r in rows],
        "total": total, "page": page, "pages": pages, "page_size": PAGE_SIZE,
    }
