"""Durable JSON files for the SPX tree (history, Brier): atomic writes, no silent wipes.

The history and Brier files are not regenerable (UW serves recent days only), so
a corrupt or torn file must never read as "empty" on the way to a write — that
replaced the whole history with one record (S-1). Writers load with
``load_json`` (raises ``CorruptJsonError``), move the bad file aside with
``quarantine`` and refuse the write; readers that only display may degrade to a
default. Every write goes through a temp file in the same directory and
``os.replace``, so a crash mid-write leaves the previous file intact.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class CorruptJsonError(RuntimeError):
    """A durable JSON file exists but cannot be decoded (or read)."""

    def __init__(self, path: Path, detail: str):
        super().__init__(f"{path}: corrupt JSON ({detail})")
        self.path = path


def load_json(path: Path, default):
    """Decoded ``path``, ``default`` when the file is missing.

    Raises ``CorruptJsonError`` when the bytes do not decode; an ``OSError``
    (locked, permission) propagates unchanged — the file may be fine.
    """
    path = Path(path)
    if not path.exists():
        return default
    data = path.read_bytes()
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CorruptJsonError(path, str(exc)) from exc


def quarantine(path: Path) -> Path | None:
    """Move a corrupt file aside to ``<name>.corrupt-<ts>`` (kept for recovery)."""
    path = Path(path)
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%dT%H%M%S%f')}")
    os.replace(path, dest)
    logger.error("SPX: %s is corrupt; moved aside to %s and refused to overwrite it. "
                 "Restore it from that copy or a backup.", path, dest.name)
    return dest


def load_for_update(path: Path, default):
    """``load_json`` for a writer: on corruption, quarantine the file and re-raise."""
    try:
        return load_json(path, default)
    except CorruptJsonError:
        quarantine(path)
        raise


def write_json_atomic(path: Path, obj) -> None:
    """Serialise first, then temp file in the same directory + ``os.replace``."""
    path = Path(path)
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
