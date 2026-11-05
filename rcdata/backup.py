"""``python -m RevenueChaser.rcdata backup`` — the market store's backup (D0, D3).

Into ``<RAW_STORE_BACKUP_DIR>/market/`` (an empty ``RAW_STORE_BACKUP_DIR`` is refused: log, exit 2, nothing written):

- ``ops_<UTC stamp>.sqlite``: the ops state. On ``ops.sqlite``, SQLite's online backup API, so a copy taken while the
  dashboard or the builder writes is consistent; checked with ``PRAGMA integrity_check`` and the row count of every
  table against the source. On Redis (``RC_REDIS_URL``, master plan Order 3), a snapshot of every table read by one Lua
  script (one moment, whatever else writes), written into a new ops.sqlite and checked row for row
  against the snapshot. The same file format either way, so a Redis backup restores with
  ``python -m RevenueChaser.rcdata.ops_copy --sqlite <copy>`` (into an empty namespace) or serves the SQLite backend
  as it is. The Redis container's own AOF is its durability, not this backup.
- ``<kind>_<UTC stamp>.duckdb`` for each serving file: copied from one open handle of the live file (a swap during
  the copy cannot mix two builds), written as ``.tmp``, verified — row count and ``sum(hash(row))`` of every table
  against the source (``scripts/backup_store.py``'s fingerprint) — and only then renamed. A swap between the copy
  and the check reads as a mismatch, and the copy is taken again (3 tries).
- ``.manifest.json``: the sha256, size and table fingerprints of every copy kept. The newest ``KEEP`` (5) per kind
  stay.

``--only`` picks the steps (``ops``, a serving kind, ``raw``) and ``--subdir`` the folder; without either, every
step runs into ``market/``, as the command did before the light/heavy split of 5 Nov 2026. The nightly run takes the
small files, a weekly one takes ``prices`` and ``ownership`` into ``market-heavy/``: the 04:30 run of 5 Nov 2026 hit
its 30-minute limit, and those two are 3.5 GB of the ~3.6 GB it copies, so they are where the time most likely went
— an inference, not a measured step, because the run records no per-step timing. Each folder has its own manifest,
verification and retention count.

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
from pathlib import Path, PureWindowsPath

from ..core import config
from . import ops, serving

logger = logging.getLogger(__name__)

KEEP = 5
ATTEMPTS = 3
MANIFEST_NAME = ".manifest.json"
SUBDIR = "market"
# The heavy run's folder (avo, 5 Nov 2026). ``SUBDIRS`` is every folder a registry entry writes into, so the Data
# Health card knows where to look for each kind's newest copy; a test keeps the two in step.
HEAVY_SUBDIR = "market-heavy"
SUBDIRS = (SUBDIR, HEAVY_SUBDIR)
RAW_STEP = "raw"
RAW_COPY_DAYS = 35
RAW_COPY_LIMIT_BYTES = 20 * 10**9
# Every name ``--only`` takes: the ops state, each serving kind, and the D3 raw copy.
STEPS = ("ops", *serving.KINDS, RAW_STEP)


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
    if ops.backend() == "redis":
        return _backup_ops_redis(out, manifest)
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


def _backup_ops_redis(out: Path, manifest: _Manifest) -> Path | None:
    from . import ops_copy, ops_redis
    if ops.schema_version() is None:                    # nothing written to this namespace yet
        return None
    taken = ops_redis.snapshot()
    dest = out / f"ops_{_stamp()}.sqlite"
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        ops.ensure.__wrapped__(tmp)                     # the SQLite tables and stamp, whatever the backend
        with ops.connect(tmp) as con:
            for table, rows in taken.items():
                for row in rows:
                    con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                                tuple(row.values()))
            con.execute("PRAGMA journal_mode = DELETE")     # one self-contained file
        got = _sqlite_counts(tmp)
        con = ops_copy._open_read_only(tmp)
        try:
            differ = [t for t, rows in taken.items() if ops_copy._table_rows(con, t) != rows]
        finally:
            con.close()
        if differ:
            raise BackupFailed(f"{dest.name}: rows of {differ} differ from the Redis snapshot")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, dest)
    manifest.add(dest, tables=got, source="redis")
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


def _check_subdir(subdir: str) -> str:
    """One folder name under the backup root: never empty, never a path, never ``..``."""
    name = (subdir or "").strip()
    # ``PureWindowsPath`` on purpose, whatever this host is: it treats ``\\`` as a separator and ``C:`` as a drive,
    # so one check covers a separator, a drive-relative name and a reserved form that ``Path(folder) / name`` would
    # resolve outside the backup root on Windows (review N1). The runtime is Linux; this is belt and braces.
    if not name or name in (".", "..") or PureWindowsPath(name).name != name:
        raise BackupRefused(f"--subdir {subdir!r} is not one folder name under the backup folder")
    return name


def _check_only(only) -> tuple[str, ...]:
    """The steps to run, in the registry's order; ``None`` is every one of them (the old behaviour)."""
    known = STEPS
    if only is None:
        return known
    names = [n.strip() for n in only] if not isinstance(only, str) else [n.strip() for n in only.split(",")]
    names = [n for n in names if n]
    if not names:
        raise BackupRefused("--only was given no step name")
    unknown = [n for n in names if n not in known]
    if unknown:
        raise BackupRefused(f"--only: unknown step(s) {', '.join(unknown)}; known: {', '.join(known)}")
    return tuple(n for n in known if n in names)            # each step once, in a fixed order


def run_backup(only=None, subdir: str = SUBDIR) -> dict:
    """Back up the named steps into ``<RAW_STORE_BACKUP_DIR>/<subdir>/``; a summary of what was written.

    ``only`` is a list (or a comma-separated string) of ``ops``, a serving kind, or ``raw`` for the D3 raw copy;
    ``None`` runs every one of them, which is what the command did before the light/heavy split. Each folder has its
    own manifest, its own verification and its own retention count, because ``KEEP`` prunes inside ``out`` alone.
    """
    steps = _check_only(only)
    subdir = _check_subdir(subdir)
    folder = (config.RAW_STORE_BACKUP_DIR or "").strip()
    if not folder:
        raise BackupRefused("RAW_STORE_BACKUP_DIR is empty: set it to the backup folder (there is no default)")
    summary: dict = {}
    failed: list[str] = []
    files = [s for s in steps if s != RAW_STEP]
    if files:
        out = Path(folder) / subdir
        out.mkdir(parents=True, exist_ok=True)
        for stale in out.glob("*.tmp"):                     # a run that was killed mid-copy
            stale.unlink(missing_ok=True)
        manifest = _Manifest(out)
        # Each file on its own: a failed (or raced) ops copy must not cost the serving files theirs, and the other way.
        for name in files:
            step = ((lambda: _backup_ops(out, manifest)) if name == "ops"
                    else (lambda name=name: _backup_kind(name, out, manifest)))
            try:
                summary[name] = step()
            except Exception as e:  # noqa: BLE001 - reported below, after every other file had its turn
                logger.error("backup of %s failed: %s", name, e)
                failed.append(f"{name}: {e}")
                summary[name] = None
    if RAW_STEP in steps:
        summary["raw_copy"] = _copy_recent_raw()
    if failed:
        raise BackupFailed("; ".join(failed))
    return summary


def _queue_is_idle() -> tuple[bool, str]:
    """``(idle, what is running)``: the builder is the serving files' writer, so nothing of its may be in flight."""
    from . import ops
    try:
        active = ops.queue_view(finished=0).get("active") or []
    except Exception as exc:                                  # noqa: BLE001 - no queue file yet is idle
        return True, f"the queue could not be read ({type(exc).__name__}), taken as idle"
    if not active:
        return True, "nothing queued or running"
    names = ", ".join(sorted({str(r.get("dataset")) for r in active}))
    return False, f"{len(active)} request(s) queued or running: {names}"


def _create_empty_stores(*, apply: bool) -> int:
    """``create-empty-stores``: a write to the live store, so it carries the three protections CLAUDE.md asks for.

    The backup is the same verified one ``backup`` runs, the queue must be idle (the builder owns these files), and
    every created file is printed with the command that undoes it. Without ``--apply`` nothing is written and the
    missing kinds are only listed - the review's I3, which found the comment promising all three above a call that
    did none of them.
    """
    from . import scaffold, serving
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    missing = [k for k in sorted(serving.KINDS) if not serving.live_path(k).exists()]
    for kind in sorted(serving.KINDS):
        print(f"{kind}: {'missing' if kind in missing else 'already built, left alone'}", flush=True)
    if not missing:
        print("nothing to create: every kind already has a serving file", flush=True)
        return 0
    if not apply:
        print(f"would create {len(missing)} file(s): {', '.join(missing)}", flush=True)
        print("nothing was written. Run it again with --apply to create them.", flush=True)
        return 0
    idle, what = _queue_is_idle()
    if not idle:
        print(f"refused: the build queue is not idle ({what}). The builder writes these files; "
              f"wait for it to finish, or pause the datasets, then run this again.", flush=True)
        return 2
    try:
        summary = run_backup()
    except (BackupRefused, BackupFailed) as exc:
        print(f"refused: the verified backup did not complete ({exc}). Nothing was created.", flush=True)
        return 2
    print(f"backup taken: {summary}", flush=True)
    made = scaffold.create_missing()
    for kind in sorted(made):
        print(f"{kind}: created {made[kind]}", flush=True)
    paths = " ".join(f'"{made[k]}"' for k in sorted(made))
    print(f"rollback: these files hold no rows, so removing them undoes this step: rm {paths}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m RevenueChaser.rcdata")
    sub = p.add_subparsers(dest="cmd", required=True)
    bk = sub.add_parser("backup", help="verified copies of the ops state (ops.sqlite or Redis) and the serving files, "
                                       "then the raw copy")
    bk.add_argument("--only", default=None,
                    help="comma-separated steps to run, out of " + ", ".join(STEPS) +
                         " (the default runs every one of them)")
    bk.add_argument("--subdir", default=SUBDIR,
                    help=f"the folder under the backup folder to write into (default {SUBDIR}); each folder keeps "
                         f"its own manifest and its own {KEEP} newest copies per kind")
    empty = sub.add_parser("create-empty-stores",
                           help="create a serving file with its tables and no rows for every kind that has none; "
                                "an existing file is never touched. Says what it would do unless --apply is given")
    empty.add_argument("--apply", action="store_true",
                       help="actually create the files (the default only reports what is missing)")
    args = p.parse_args(argv)
    if args.cmd == "create-empty-stores":
        return _create_empty_stores(apply=args.apply)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        summary = run_backup(only=args.only, subdir=args.subdir)
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
