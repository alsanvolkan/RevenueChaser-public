"""``python -m RevenueChaser.rcdata backup`` — the market store's backup (D0, D3).

Into ``<RAW_STORE_BACKUP_DIR>/market/`` (an empty ``RAW_STORE_BACKUP_DIR`` is refused: log, exit 2, nothing written):

- ``ops_<UTC stamp>.sqlite``: SQLite's online backup API, so a copy taken while the dashboard or the builder writes
  is consistent; checked with ``PRAGMA integrity_check`` and the row count of every table against the source.
- ``<kind>_<UTC stamp>.duckdb`` for each serving file: copied from one open handle of the live file (a swap during
  the copy cannot mix two builds), written as ``.tmp``, verified — row count and ``sum(hash(row))`` of every table
  against the source (``scripts/backup_store.py``'s fingerprint) — and only then renamed. A swap between the copy
  and the check reads as a mismatch, and the copy is taken again (3 tries).
- ``.manifest.json``: the sha256, size and table fingerprints of every copy kept. The newest ``KEEP`` (5) per kind
  stay.

Then the D3 copy: the raw files of the last ``RAW_COPY_DAYS`` (35) days (by modification time) under
``<MARKET_RAW_DIR>/raw/`` go to ``RC_MARKET_RAW_COPY_DIR``, rsync-like (a file whose size and mtime match is not
copied again; nothing is deleted there), when their total is under ``RAW_COPY_LIMIT_BYTES`` (20 GB). Measured first
and logged; over the limit, or with no copy folder set, a warning and no copy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ..core import config
from . import ops, serving

logger = logging.getLogger(__name__)

KEEP = 5
ATTEMPTS = 3
MANIFEST_NAME = ".manifest.json"
SUBDIR = "market"
RAW_COPY_DAYS = 35
RAW_COPY_LIMIT_BYTES = 20 * 10**9


class BackupRefused(RuntimeError):
    """The backup cannot start (no backup folder configured)."""


class BackupFailed(RuntimeError):
    """A copy did not verify; nothing unverified was kept."""


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fingerprints(path: Path) -> dict[str, tuple[int, int]]:
    """``{table: (rows, sum of row hashes)}`` of a DuckDB file, read-only (the nightly backup's own check)."""
    from ..scripts import backup_store
    return backup_store._table_fingerprints(path)


def _sqlite_counts(path: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupFailed(f"{path.name}: integrity_check failed")
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                           "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        con.close()


class _Manifest:
    def __init__(self, out: Path):
        self.path = out / MANIFEST_NAME
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "files": {}}

    def add(self, path: Path, **extra) -> None:
        self.data["files"][path.name] = {"sha256": _sha256(path), "bytes": path.stat().st_size,
                                         "at": datetime.now(timezone.utc).isoformat(), **extra}

    def drop(self, name: str) -> None:
        self.data["files"].pop(name, None)

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


def _prune(out: Path, pattern: str, manifest: _Manifest) -> None:
    for old in sorted(out.glob(pattern))[:-KEEP]:
        manifest.drop(old.name)
        manifest.save()                      # listed only while it exists: drop first, then delete
        old.unlink(missing_ok=True)


def _online_backup(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    source.backup(target)


def _backup_ops(out: Path, manifest: _Manifest) -> Path | None:
    src = ops.ops_path()
    if not src.exists():
        return None
    dest = out / f"ops_{_stamp()}.sqlite"
    tmp = dest.with_name(dest.name + ".tmp")
    with ops.connect(src) as source:
        # One read transaction: the counts and the copy see the same snapshot, so a write that lands in between
        # (the dashboard enqueuing, the builder counting calls) is in neither and cannot fail the check.
        source.execute("BEGIN")
        try:
            want = {r[0]: source.execute(f'SELECT count(*) FROM "{r[0]}"').fetchone()[0]
                    for r in source.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                            "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()}
            target = sqlite3.connect(str(tmp))
            try:
                _online_backup(source, target)
                # A copy is one self-contained file: no -wal/-shm beside it, whatever the source's mode.
                target.execute("PRAGMA journal_mode = DELETE")
            finally:
                target.close()
        finally:
            source.execute("COMMIT")
    try:
        got = _sqlite_counts(tmp)
        if got != want:
            raise BackupFailed(f"{dest.name}: rows {got} != source {want}")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, dest)
    manifest.add(dest, tables=got)
    manifest.save()
    _prune(out, "ops_*.sqlite", manifest)
    return dest


def _backup_kind(kind: str, out: Path, manifest: _Manifest) -> Path | None:
    live = serving.live_path(kind)
    if not live.exists():
        return None
    last = ""
    for _ in range(ATTEMPTS):
        dest = out / f"{kind}_{_stamp()}.duckdb"
        tmp = dest.with_name(dest.name + ".tmp")
        with open(live, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        try:
            want, got = _fingerprints(live), _fingerprints(tmp)
        except Exception as e:  # noqa: BLE001 - an unreadable copy is a failed try, never a kept file
            want, got = None, None
            last = f"{type(e).__name__}: {e}"
        if want is not None and want == got:
            os.replace(tmp, dest)
            manifest.add(dest, source=str(live), tables={t: list(v) for t, v in got.items()})
            manifest.save()
            _prune(out, f"{kind}_*.duckdb", manifest)
            return dest
        tmp.unlink(missing_ok=True)
        last = last or f"{dest.name}: copy {got} != source {want}"
        time.sleep(0.2)
    raise BackupFailed(f"{kind}: no verified copy after {ATTEMPTS} tries ({last})")


def _size(path: Path) -> int:
    return path.stat().st_size


def _copy_recent_raw() -> dict | None:
    target = config.MARKET_RAW_COPY_DIR
    root = Path(config.MARKET_RAW_DIR) / "raw"
    if target is None:
        logger.warning("raw copy skipped: RC_MARKET_RAW_COPY_DIR is not set")
        return None
    cutoff = time.time() - RAW_COPY_DAYS * 86400
    files = [p for p in root.rglob("*") if p.is_file() and not p.name.endswith(".tmp")
             and p.stat().st_mtime >= cutoff] if root.exists() else []
    total = sum(_size(p) for p in files)
    logger.info("raw copy: %d files, %.2f GB in the last %d days", len(files), total / 1e9, RAW_COPY_DAYS)
    if total >= RAW_COPY_LIMIT_BYTES:
        logger.warning("raw copy skipped: %.2f GB is over the %.0f GB limit", total / 1e9, RAW_COPY_LIMIT_BYTES / 1e9)
        return {"files": len(files), "bytes": total, "copied": 0}
    copied = 0
    for p in files:
        dest = Path(target) / p.relative_to(root)
        st = p.stat()
        if dest.exists() and dest.stat().st_size == st.st_size and int(dest.stat().st_mtime) == int(st.st_mtime):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.copy2(p, tmp)
        os.replace(tmp, dest)
        copied += 1
    return {"files": len(files), "bytes": total, "copied": copied}


def run_backup() -> dict:
    """Back up ops.sqlite and every serving file, then the D3 raw copy; a summary of what was written."""
    folder = (config.RAW_STORE_BACKUP_DIR or "").strip()
    if not folder:
        raise BackupRefused("RAW_STORE_BACKUP_DIR is empty: set it to the backup folder (there is no default)")
    out = Path(folder) / SUBDIR
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.tmp"):                     # a run that was killed mid-copy
        stale.unlink(missing_ok=True)
    manifest = _Manifest(out)
    summary: dict = {}
    failed: list[str] = []
    # Each file on its own: a failed (or raced) ops copy must not cost the serving files theirs, and the other way.
    for name, step in [("ops", lambda: _backup_ops(out, manifest))] + [
            (kind, lambda kind=kind: _backup_kind(kind, out, manifest)) for kind in serving.KINDS]:
        try:
            summary[name] = step()
        except Exception as e:  # noqa: BLE001 - reported below, after every other file had its turn
            logger.error("backup of %s failed: %s", name, e)
            failed.append(f"{name}: {e}")
            summary[name] = None
    summary["raw_copy"] = _copy_recent_raw()
    if failed:
        raise BackupFailed("; ".join(failed))
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m RevenueChaser.rcdata")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backup", help="verified copies of ops.sqlite and the serving files, then the raw copy")
    p.parse_args(argv)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        summary = run_backup()
    except BackupRefused as e:
        logger.error("backup refused: %s", e)
        return 2
    except BackupFailed as e:
        logger.error("backup failed: %s", e)
        return 1
    for name, value in summary.items():
        print(f"{name}: {value if value is not None else 'nothing to back up'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
