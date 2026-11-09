"""``python -m RevenueChaser.rcdata backup`` — the weekly backup (avo's decisions of 8 Nov 2026).
``rcdata restore`` and ``rcdata restore-check`` are in ``rcdata/restore.py``.

One job a week (Sunday 03:30, registry row ``RC-Backup-Weekly``), everything onto the HDD under
``<RAW_STORE_BACKUP_DIR>/weekly-v2/<step>/`` (an empty ``RAW_STORE_BACKUP_DIR`` is refused: log, exit 2, nothing
written), the newest ``KEEP`` (4) copies of every file kept, the oldest deleted only after the new copy verified.
Raw downloads and Parquet get no backup (they are fetched again). Nothing leaves the machine.

The steps, in the order they run (``--only`` picks some of them, or one DuckDB kind for a copy by hand):

- ``duckdb``: the DuckDB files the builder writes from Parquet (``serving.KINDS``: market, spine, ownership, prices,
  fundamentals, options), each ``<kind>_<UTC stamp>.duckdb``: copied from one open handle of the live file (a swap
  during the copy cannot mix two builds), written as ``.tmp``, verified (row count and ``sum(hash(row))`` of every
  table against the source) and only then renamed; a swap between copy and check reads as a mismatch and the copy is
  taken again (3 tries). A copy taken with ``--only <kind>`` (before a deploy) is tagged ``source: manual`` and
  pruned on its own count (``KEEP_MANUAL``), so it never costs a weekly copy.
- ``boards``: the board-result DuckDBs, which the dashboard writes live: the old store's ``raw``, ``edgar``,
  ``history`` and ``prices`` files while they exist, and every ``<RC_STORE_DIR>/boards/<board>.duckdb`` (DECISIONS
  S6). Each is first opened through ``rawstore`` (the peer-lock budget: a dashboard holding the file is waited out,
  outer ``ATTEMPTS`` tries a second apart) for a ``CHECKPOINT``, so the ``.wal`` is in the main file, then copied and
  verified like a ``duckdb`` copy. The manifest carries the file's ``store_schema`` version. A board writer must open
  its file through ``rawstore`` too: the ``.rc-proc-<pid>`` marker beside the file is how its lock reads as a peer's.
- ``ops``: the operational state, only the tables worth keeping (``OPS_BACKUP_TABLES``: the watch lists and their
  changes, the dataset schedule, the switches, the automation rows, the run records); the queue, the read log and
  the counters are left empty in the copy. On ``ops.sqlite`` SQLite's online backup API, built on local storage
  (5 Nov 2026: 8m44s page by page on the HDD) and checked table by table; on Redis a snapshot of every table read by
  one Lua script, written into a new ``ops.sqlite`` and checked row for row. The copy restores with
  ``python -m RevenueChaser.rcdata.ops_copy --sqlite <copy>`` into an EMPTY namespace only.
- ``uw-raw``: the raw files no provider serves again: ``<MARKET_RAW_DIR>/raw/uw/`` (Unusual Whales gives no
  history; the subscription ends 6 Dec 2026) and the SPX Gamma tree (``module_spx_gamma.config.DATA_ROOT``), into
  ``uw-raw/<ISO week>/``. A file unchanged since the previous week's folder (size and mtime) is hard-linked to that
  copy, so four weeks cost one copy plus the new days; a new or changed file is copied through ``.tmp`` and verified
  by size and SHA-256. A file modified in the last ``SETTLE_SECONDS`` waits for the next run (it may still be
  downloading), and in the SPX tree a ``.json`` that is not valid JSON never replaces a copy that is (avo, Q5).
- ``edgar-docs``: the ``edgar_docs/`` tree as one verified zip (every member's CRC and the document count); an
  unchanged tree (same ``stat`` fingerprint) keeps the newest zip instead of zipping again (#336).

Every step has its own folder and its own ``.manifest.json`` (sha256, size, table fingerprints of every copy kept).
A step that fails is logged and the other steps still run; the exit code is 1 when any failed. A failed copy leaves
nothing behind (no ``.tmp``, no manifest entry) and prunes nothing.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from ..core import config
from . import ops, serving

logger = logging.getLogger(__name__)

ROOT = "weekly-v2"                  # never ``weekly/``: the retired nightly job's raw snapshots sit there on homesrv
KEEP = 4
KEEP_MANUAL = 1                     # copies taken by hand with ``--only <kind>`` (before a deploy)
ATTEMPTS = 3
MANIFEST_NAME = ".manifest.json"
SETTLE_SECONDS = 600                # a file modified more recently than this may still be downloading
_MTIME_TOLERANCE_NS = 2 * 10**9     # FAT rounds mtimes to 2 s; copy2 keeps the mtime
_DOCS_FINGERPRINT_PREFIX = b"rc-edgar-docs-tree-sha256:"

STEP_DUCKDB = "duckdb"
STEP_BOARDS = "boards"
STEP_OPS = "ops"
STEP_UW_RAW = "uw-raw"
STEP_EDGAR_DOCS = "edgar-docs"
STEPS = (STEP_DUCKDB, STEP_BOARDS, STEP_OPS, STEP_UW_RAW, STEP_EDGAR_DOCS)

#: The old store's files the ``boards`` step copies while they exist (board results live there until S6 moves them).
LEGACY_BOARD_FILES = ("raw", "edgar", "history", "prices")
BOARDS_DIRNAME = "boards"
#: The ops tables a backup keeps. The queue (``build_requests``), the read log, the call counters, the UW header
#: samples and the check/issue rows are left empty in the copy (avo, 8 Nov 2026: "gerek olmayanlari tutma").
OPS_BACKUP_TABLES = ("dataset_schedule", "kind_automation", "dataset_automation", "switch", "job_runs2")
OPS_BACKUP_PREFIXES = ("watchlist_",)
#: The SPX Gamma tree's two files rewritten in place: a damaged JSON must never replace a good copy (avo, Q5).
SPX_JSON_GUARD = True


class BackupRefused(RuntimeError):
    """The backup cannot start (no backup folder configured, an unknown step)."""


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


def _table_fingerprints(db_path: Path) -> dict[str, tuple[int, int]]:
    """``{table: (row count, content checksum)}`` of a DuckDB file, read-only.

    The checksum is the sum of every row's hash (order-independent, and unlike XOR a duplicated row does not cancel
    out). A file with an intact header but a damaged data block opens and then raises here: that is a failed copy,
    never a kept one."""
    if not db_path.is_file():
        raise BackupFailed(f"file not found: {db_path}")
    try:
        conn = duckdb.connect(str(db_path), read_only=True)
    except duckdb.Error as exc:
        raise BackupFailed(f"could not open {db_path} read-only (another writer?): {exc}") from exc
    try:
        names = [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'").fetchall()]
        out = {}
        for n in names:
            cnt, chk = conn.execute(
                f'SELECT count(*), coalesce(sum(hash(t)::HUGEINT), 0) FROM "{n}" t').fetchone()
            out[n] = (int(cnt), int(chk))
        return out
    except duckdb.Error as exc:
        raise BackupFailed(f"could not read {db_path.name}: {exc}") from exc
    finally:
        conn.close()


def _fingerprints(path: Path) -> dict[str, tuple[int, int]]:
    return _table_fingerprints(path)


def _store_schema(path: Path) -> int | None:
    """The ``store_schema`` version a store file carries, or None (a file without the table, or unreadable)."""
    try:
        conn = duckdb.connect(str(path), read_only=True)
    except duckdb.Error:
        return None
    try:
        row = conn.execute("SELECT max(try_cast(value AS INTEGER)) FROM store_schema "
                           "WHERE key = 'schema_version'").fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except duckdb.Error:
        return None
    finally:
        conn.close()


def _is_kept_ops_table(name: str) -> bool:
    return name in OPS_BACKUP_TABLES or name.startswith(OPS_BACKUP_PREFIXES)


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
            self.data = {"version": 2, "files": {}}
        self.data.setdefault("files", {})

    def add(self, path: Path, **extra) -> None:
        self.data["files"][path.name] = {"sha256": _sha256(path), "bytes": path.stat().st_size,
                                         "at": datetime.now(timezone.utc).isoformat(), **extra}

    def drop(self, name: str) -> None:
        self.data["files"].pop(name, None)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


def _prune(out: Path, pattern: str, manifest: _Manifest, source: str = "weekly", keep: int | None = None) -> None:
    """Keep the newest ``keep`` copies matching ``pattern`` whose manifest ``source`` is ``source``.

    Weekly and manual copies are counted apart, so a copy taken by hand before a deploy never costs a weekly one.
    A file the manifest does not know is treated as weekly (the only kind an older manifest wrote). Listed only while
    it exists: the manifest entry goes first, then the file."""
    keep = KEEP if keep is None else keep
    files = manifest.data["files"]
    mine = sorted(p for p in out.glob(pattern)
                  if (files.get(p.name) or {}).get("source", "weekly") == source)
    for old in mine[:-keep] if keep > 0 else mine:
        manifest.drop(old.name)
        manifest.save()
        old.unlink(missing_ok=True)


def _copy_bytes(src: Path, dest: Path) -> None:
    """One sequential write, which is what the backup disk is good at."""
    with open(src, "rb") as s, open(dest, "wb") as d:
        shutil.copyfileobj(s, d, 1 << 20)


def _sweep_tmp(out: Path) -> None:
    for stale in out.glob("*.tmp*"):                    # a run that was killed mid-copy, its -wal/-shm too
        stale.unlink(missing_ok=True)
    for stale in out.glob("*.partial.zip"):
        stale.unlink(missing_ok=True)


# ── duckdb and boards: a verified copy of one DuckDB file ─────────────────────────────────────────────────────────

def _copy_duckdb(live: Path, out: Path, stem: str, manifest: _Manifest, *, source: str = "weekly",
                 keep: int | None = None, **extra) -> Path:
    """``<stem>_<stamp>.duckdb`` under ``out``: copied, fingerprinted against the source, renamed, pruned."""
    last = ""
    for _ in range(ATTEMPTS):
        dest = out / f"{stem}_{_stamp()}.duckdb"
        tmp = dest.with_name(dest.name + ".tmp")
        _copy_bytes(live, tmp)
        try:
            want, got = _fingerprints(live), _fingerprints(tmp)
        except Exception as e:  # noqa: BLE001 - an unreadable copy is a failed try, never a kept file
            want, got = None, None
            last = f"{type(e).__name__}: {e}"
        if want is not None and want == got:
            os.replace(tmp, dest)
            manifest.add(dest, source=source, live=str(live), tables={t: list(v) for t, v in got.items()}, **extra)
            manifest.save()
            _prune(out, f"{stem}_*.duckdb", manifest, source=source, keep=keep)
            return dest
        tmp.unlink(missing_ok=True)
        last = last or f"{dest.name}: copy {got} != source {want}"
        time.sleep(0.2)
    raise BackupFailed(f"{stem}: no verified copy after {ATTEMPTS} tries ({last})")


def _backup_kind(kind: str, out: Path, manifest: _Manifest, *, manual: bool = False) -> Path | None:
    live = serving.live_path(kind)
    if not live.exists():
        return None
    if manual:
        return _copy_duckdb(live, out, kind, manifest, source="manual", keep=KEEP_MANUAL)
    return _copy_duckdb(live, out, kind, manifest)


def _checkpoint(path: Path) -> None:
    """Flush the ``.wal`` of a live-written store file into the main file, through ``rawstore``'s peer-lock budget.

    The dashboard holds a board file only for one operation, and its ``.rc-proc-<pid>`` marker beside the file is
    what lets the wait last the full reader budget (about 29 s) instead of failing fast on a foreign lock. The
    ``CHECKPOINT`` is the one write this job makes to a live file: it changes no row."""
    from ..core import rawstore
    conn = rawstore._connect_with_retry(rawstore._PEER_LOCK_DELAYS, path)
    try:
        conn.execute("CHECKPOINT")
    finally:
        with rawstore._ATTACH_LOCK:
            conn.close()


def board_files() -> list[Path]:
    """The board-result DuckDBs that exist: the old store's files, then ``<RC_STORE_DIR>/boards/*.duckdb``."""
    from ..core import rawstore
    legacy = {"raw": Path(rawstore.DB_PATH), "edgar": rawstore.edgar_db_path(),
              "history": rawstore.history_db_path(), "prices": rawstore.prices_db_path()}
    found = [legacy[k] for k in LEGACY_BOARD_FILES if legacy[k].is_file()]
    boards = Path(rawstore.DB_PATH).parent / BOARDS_DIRNAME
    if boards.is_dir():
        found += sorted(p for p in boards.glob("*.duckdb") if p.is_file())
    return found


def _backup_board(path: Path, out: Path, manifest: _Manifest) -> Path:
    last: Exception | None = None
    for _ in range(ATTEMPTS):
        try:
            _checkpoint(path)
        except Exception as exc:  # noqa: BLE001 - a held lock: wait a second and try again
            last = exc
            time.sleep(1.0)
            continue
        return _copy_duckdb(path, out, path.stem, manifest, store_schema=_store_schema(path))
    raise BackupFailed(f"{path.name}: could not checkpoint the live file after {ATTEMPTS} tries ({last})")


def _backup_boards(out: Path, manifest: _Manifest) -> dict[str, Path]:
    done: dict[str, Path] = {}
    failed: list[str] = []
    for path in board_files():
        try:
            done[path.stem] = _backup_board(path, out, manifest)
        except Exception as exc:  # noqa: BLE001 - the other files still get their copy
            logger.error("backup of %s failed: %s", path.name, exc)
            failed.append(f"{path.name}: {exc}")
    if failed:
        raise BackupFailed("; ".join(failed))
    return done


# ── ops: the needed tables only ─────────────────────────────────────────────────────────────────────────────────

def _online_backup(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    source.backup(target)


def _drop_unkept(con: sqlite3.Connection) -> None:
    """Empty the tables the backup does not keep (the table stays, so ``ops_copy`` and ``ensure`` read the file)."""
    names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                       "AND name NOT LIKE 'sqlite_%'").fetchall()]
    for n in names:
        if n != "ops_schema" and not _is_kept_ops_table(n):
            con.execute(f'DELETE FROM "{n}"')
    con.commit()


def _kept_counts(counts: dict[str, int]) -> dict[str, int]:
    return {t: n for t, n in counts.items() if t == "ops_schema" or _is_kept_ops_table(t)}


def _backup_ops(out: Path, manifest: _Manifest) -> Path | None:
    if ops.backend() == "redis":
        return _backup_ops_redis(out, manifest)
    src = ops.ops_path()
    if not src.exists():
        return None
    dest = out / f"ops_{_stamp()}.sqlite"
    tmp = dest.with_name(dest.name + ".tmp")
    with tempfile.TemporaryDirectory(prefix="rc-ops-backup-") as staging:
        local = Path(staging) / dest.name
        with ops.connect(src) as source:
            source.execute("BEGIN")
            try:
                want = {r[0]: source.execute(f'SELECT count(*) FROM "{r[0]}"').fetchone()[0]
                        for r in source.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                                "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()}
                target = sqlite3.connect(str(local))
                try:
                    _online_backup(source, target)
                    _drop_unkept(target)
                    target.execute("PRAGMA journal_mode = DELETE")     # one self-contained file
                    target.execute("VACUUM")
                finally:
                    target.close()
            finally:
                source.execute("COMMIT")
        want = _kept_counts(want)
        try:
            _copy_bytes(local, tmp)
            got = _kept_counts(_sqlite_counts(tmp))
            if got != want:
                raise BackupFailed(f"{dest.name}: rows {got} != source {want}")
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, dest)
    manifest.add(dest, tables=got, source="weekly", backend="sqlite")
    manifest.save()
    _prune(out, "ops_*.sqlite", manifest)
    return dest


def _backup_ops_redis(out: Path, manifest: _Manifest) -> Path | None:
    from . import ops_copy, ops_redis
    if ops.schema_version() is None:                    # nothing written to this namespace yet
        return None
    taken = {t: rows for t, rows in ops_redis.snapshot().items() if _is_kept_ops_table(t)}
    dest = out / f"ops_{_stamp()}.sqlite"
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with tempfile.TemporaryDirectory(prefix="rc-ops-backup-") as staging:   # row by row: never on the HDD
            local = Path(staging) / dest.name
            ops.ensure.__wrapped__(local)               # the SQLite tables and stamp, whatever the backend
            with ops.connect(local) as con:
                with ops._tx(con):                      # one transaction, one statement per table (#387, #430)
                    for table, rows in taken.items():
                        if rows:
                            con.executemany(f"INSERT INTO {table} ({', '.join(rows[0])}) "
                                            f"VALUES ({', '.join('?' * len(rows[0]))})",
                                            [tuple(row.values()) for row in rows])
                con.execute("PRAGMA journal_mode = DELETE")
            _copy_bytes(local, tmp)
        got = _kept_counts(_sqlite_counts(tmp))
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
    manifest.add(dest, tables=got, source="weekly", backend="redis")
    manifest.save()
    _prune(out, "ops_*.sqlite", manifest)
    return dest


# ── uw-raw: the UW day files and the SPX Gamma tree, one folder per ISO week ─────────────────────────────────────

def _iso_week(now: datetime | None = None) -> str:
    y, w, _ = (now or datetime.now(timezone.utc)).isocalendar()
    return f"{y}-W{w:02d}"


def _is_json(path: Path) -> bool:
    try:
        json.loads(path.read_bytes().decode("utf-8"))
    except (ValueError, OSError):
        return False
    return True


def _same_stat(a: os.stat_result, b: os.stat_result) -> bool:
    return a.st_size == b.st_size and abs(a.st_mtime_ns - b.st_mtime_ns) <= _MTIME_TOLERANCE_NS


def uw_raw_sources() -> list[tuple[str, Path]]:
    """``(name, root)`` of every tree the ``uw-raw`` step copies: the UW raw folder and the SPX Gamma tree."""
    from ..module_spx_gamma import config as spx_config
    return [("uw", Path(config.MARKET_RAW_DIR) / "raw" / "uw"), ("spx_gamma", Path(spx_config.DATA_ROOT))]


def _copy_tree(src: Path, dest: Path, previous: Path | None, manifest: _Manifest, *, json_guard: bool,
               name: str, now: float) -> dict:
    """Copy ``src`` into ``dest``; link unchanged files to ``previous``'s copy; verify every copied file."""
    copied = linked = skipped = rejected = 0
    cutoff = now - SETTLE_SECONDS
    week, prev_week = dest.parent.name, previous.parent.name if previous is not None else None
    tmp_dir = dest / ".tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    try:
        for n, f in enumerate(sorted(p for p in src.rglob("*") if p.is_file())):
            copied, linked, skipped, rejected = _copy_one(
                f, n, src, dest, previous, manifest, json_guard=json_guard, name=name, cutoff=cutoff, tmp_dir=tmp_dir,
                week=week, prev_week=prev_week, counts=(copied, linked, skipped, rejected))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return {"copied": copied, "linked": linked, "skipped": skipped, "rejected": rejected}


def _copy_one(f: Path, n: int, src: Path, dest: Path, previous: Path | None, manifest: _Manifest, *, json_guard: bool,
              name: str, cutoff: float, tmp_dir: Path, week: str, prev_week: str | None,
              counts: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """One file of ``_copy_tree``: the updated ``(copied, linked, skipped, rejected)``."""
    copied, linked, skipped, rejected = counts
    rel = f.relative_to(src)
    try:
        st = f.stat()
    except FileNotFoundError:
        return counts
    if f.name.endswith(".tmp") or ".tmp" in rel.parts:
        return counts
    final = dest / rel
    if final.exists():                                  # the week's folder already holds it (a rerun)
        return counts
    key = f"{week}/{name}/{rel.as_posix()}"
    old = previous / rel if previous is not None else None
    if old is not None and old.is_file() and _same_stat(old.stat(), st):
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(old, final)
        except OSError:                                 # another filesystem: a copy instead
            pass
        else:
            manifest.data["files"][key] = {"linked_from": f"{prev_week}/{name}/{rel.as_posix()}",
                                           "bytes": st.st_size}
            return copied, linked + 1, skipped, rejected
    if st.st_mtime > cutoff:                            # may still be downloading: next week
        return copied, linked, skipped + 1, rejected
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{n}.part"
    try:
        shutil.copy2(f, tmp)
        if not _same_stat(f.stat(), st):                # changed while copying: the download is still running
            tmp.unlink(missing_ok=True)
            return copied, linked, skipped + 1, rejected
        digest, src_digest = _sha256(tmp), _sha256(f)
    except FileNotFoundError:
        tmp.unlink(missing_ok=True)
        return copied, linked, skipped + 1, rejected
    if tmp.stat().st_size != st.st_size or digest != src_digest:
        tmp.unlink(missing_ok=True)
        raise BackupFailed(f"{name}: copy of {rel.as_posix()} does not match its source (size/SHA-256)")
    if json_guard and f.suffix == ".json" and not _is_json(tmp) and old is not None and old.is_file() \
            and _is_json(old):
        tmp.unlink(missing_ok=True)                     # a damaged JSON never replaces a good copy: keep last week's
        final.parent.mkdir(parents=True, exist_ok=True)
        os.link(old, final)
        manifest.data["files"][key] = {"linked_from": f"{prev_week}/{name}/{rel.as_posix()}", "bytes": old.stat().st_size,
                                       "rejected": "not valid JSON"}
        return copied, linked, skipped, rejected + 1
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, final)
    manifest.data["files"][key] = {"sha256": digest, "bytes": st.st_size}
    return copied + 1, linked, skipped, rejected


def _week_dirs(out: Path) -> list[Path]:
    return sorted(p for p in out.iterdir() if p.is_dir() and len(p.name) == 8 and p.name[4:6] == "-W")


def _backup_uw_raw(out: Path, manifest: _Manifest, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    sources = [(name, root) for name, root in uw_raw_sources() if root.is_dir() and any(root.rglob("*"))]
    if not sources:
        return None
    weeks = _week_dirs(out)
    week = out / _iso_week(now)
    previous = next((w for w in reversed(weeks) if w != week), None)
    week.mkdir(parents=True, exist_ok=True)
    summary: dict = {"week": week.name}
    for name, root in sources:
        summary[name] = _copy_tree(root, week / name, previous / name if previous is not None else None, manifest,
                                   json_guard=(name == "spx_gamma" and SPX_JSON_GUARD), name=name,
                                   now=time.time())
    manifest.data["weeks"] = [w.name for w in _week_dirs(out)]
    manifest.save()
    if any(v.get("rejected") for v in summary.values() if isinstance(v, dict)):
        logger.warning("uw-raw: a damaged JSON was held back and last week's copy kept: %s", summary)
    for old in _week_dirs(out)[:-KEEP]:                 # only after this week's folder is complete
        for key in [k for k in manifest.data["files"] if k.startswith(old.name + "/")]:
            manifest.data["files"].pop(key, None)
        manifest.data["weeks"] = [w for w in manifest.data.get("weeks", []) if w != old.name]
        manifest.save()
        shutil.rmtree(old, ignore_errors=True)
    return summary


# ── edgar-docs: one verified zip of the tree, reused while the tree is unchanged ────────────────────────────────

def _tree_fingerprint(d: Path) -> bytes:
    """SHA-256 over every file of the tree, sorted: relative path, size and mtime in nanoseconds (a ``stat`` walk)."""
    h = hashlib.sha256()
    for rel, size, mtime_ns in sorted((p.relative_to(d).as_posix(), st.st_size, st.st_mtime_ns)
                                      for p in d.rglob("*") if p.is_file() for st in (p.stat(),)):
        h.update(f"{rel}\0{size}\0{mtime_ns}\n".encode("utf-8", "surrogateescape"))
    return _DOCS_FINGERPRINT_PREFIX + h.hexdigest().encode("ascii")


def _archive_reusable(archive: Path, fingerprint: bytes, expected_docs: int) -> bool | None:
    """True: made from a tree with this fingerprint and lists every document. False: another tree or unreadable
    right now. None: damaged (not a zip, or a document missing)."""
    import zipfile
    try:
        with zipfile.ZipFile(archive) as zf:
            if zf.comment != fingerprint:
                return False
            n = sum(1 for i in zf.infolist() if i.filename.endswith(".html.gz"))
    except (zipfile.BadZipFile, zlib.error, EOFError):
        return None
    except OSError:
        return False
    return True if n == expected_docs else None


def _verify_archive(archive: Path, expected_docs: int) -> None:
    import zipfile
    try:
        with zipfile.ZipFile(archive) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise BackupFailed(f"edgar_docs archive corrupt at {bad}")
            n = sum(1 for i in zf.infolist() if i.filename.endswith(".html.gz"))
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as exc:
        raise BackupFailed(f"edgar_docs archive unreadable: {exc}") from exc
    if n < expected_docs:
        raise BackupFailed(f"edgar_docs archive holds {n} documents, expected {expected_docs}")


def _stamp_fingerprint(archive: Path, fingerprint: bytes) -> None:
    import zipfile
    try:
        with zipfile.ZipFile(archive, "a") as zf:
            zf.comment = fingerprint
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as exc:
        raise BackupFailed(f"edgar_docs archive unreadable: {exc}") from exc


def _backup_edgar_docs(out: Path, manifest: _Manifest, src: Path | None = None) -> Path | None:
    d = Path(src or config.EDGAR_DOCS_DIR)
    if not d.is_dir() or not any(d.rglob("*.html.gz")):
        return None
    expected = sum(1 for _ in d.rglob("*.html.gz"))
    fingerprint = _tree_fingerprint(d)
    newest = sorted(p for p in out.glob("edgar_docs_*.zip") if ".partial" not in p.name)[-1:]
    damaged = None
    if newest:
        reusable = _archive_reusable(newest[0], fingerprint, expected)
        if reusable:
            return newest[0]                            # unchanged tree: the newest zip stands for this week
        if reusable is None:
            damaged = newest[0]
    stamp = _stamp()
    tmp_base = out / f"edgar_docs_{stamp}.partial"
    try:
        archive = Path(shutil.make_archive(str(tmp_base), "zip", root_dir=str(d)))
    except OSError as exc:
        with contextlib.suppress(OSError):
            Path(f"{tmp_base}.zip").unlink(missing_ok=True)
        raise BackupFailed(f"could not zip {d}: {exc}") from exc
    try:
        _stamp_fingerprint(archive, fingerprint)
        _verify_archive(archive, expected)
    except BackupFailed:
        with contextlib.suppress(OSError):
            archive.unlink(missing_ok=True)
        raise
    final = out / f"edgar_docs_{stamp}.zip"
    os.replace(archive, final)
    manifest.add(final, source="weekly", documents=expected)
    manifest.save()
    if damaged is not None:
        manifest.drop(damaged.name)
        manifest.save()
        with contextlib.suppress(OSError):
            damaged.unlink(missing_ok=True)
    _prune(out, "edgar_docs_*.zip", manifest)
    return final


# ── the run ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _check_only(only) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(steps, manual kinds)``: ``None`` is every step; a DuckDB kind names a copy by hand of that file alone."""
    if only is None:
        return STEPS, ()
    names = [n.strip() for n in only] if not isinstance(only, str) else [n.strip() for n in only.split(",")]
    names = [n for n in names if n]
    if not names:
        raise BackupRefused("--only was given no step name")
    unknown = [n for n in names if n not in STEPS and n not in serving.KINDS]
    if unknown:
        raise BackupRefused(f"--only: unknown step(s) {', '.join(unknown)}; "
                            f"known: {', '.join(STEPS)}, or a DuckDB kind: {', '.join(serving.KINDS)}")
    steps = tuple(s for s in STEPS if s in names)
    kinds = tuple(k for k in serving.KINDS if k in names)
    return steps, kinds


def backup_root() -> Path:
    folder = (config.RAW_STORE_BACKUP_DIR or "").strip()
    if not folder:
        raise BackupRefused("RAW_STORE_BACKUP_DIR is empty: set it to the backup folder (there is no default)")
    return Path(folder) / ROOT


def step_dir(step: str) -> Path:
    return backup_root() / step


def _open_step(step: str) -> tuple[Path, _Manifest]:
    out = step_dir(step)
    out.mkdir(parents=True, exist_ok=True)
    _sweep_tmp(out)
    foreign = [p.name for p in out.iterdir()
               if p.is_file() and not p.name.startswith(".") and not p.name.startswith(tuple(
                   (*serving.KINDS, *LEGACY_BOARD_FILES, "ops", "edgar_docs")))
               and step != STEP_BOARDS]
    if foreign:                                         # logged, never a refusal and never a delete
        logger.warning("%s: %d file(s) this job did not write, left alone: %s", step, len(foreign), foreign[:5])
    return out, _Manifest(out)


def _record(manifest: _Manifest, failed_before: int, failed: list[str]) -> None:
    """Stamp the step's manifest with this run: ``last_run`` always, ``last_ok`` when every part verified, else
    ``last_error`` (the Data/Health card reads them)."""
    now = datetime.now(timezone.utc).isoformat()
    errors = failed[failed_before:]
    manifest.data["last_run"] = now
    if errors:
        manifest.data["last_error"] = "; ".join(errors)
    else:
        manifest.data["last_ok"] = now
        manifest.data.pop("last_error", None)
    manifest.save()


def run_backup(only=None) -> dict:
    """Run the named steps (every one of them by default); a summary of what was written.

    ``only`` is a list (or a comma-separated string) of step names, or of DuckDB kinds for a copy by hand before a
    deploy (``rcdata backup --only fundamentals``), which lands in ``weekly-v2/duckdb/`` tagged ``manual``."""
    steps, kinds = _check_only(only)
    backup_root()                                       # refused before anything is written
    summary: dict = {}
    failed: list[str] = []

    def run(name: str, fn) -> None:
        try:
            summary[name] = fn()
        except Exception as e:  # noqa: BLE001 - reported below, after every other step had its turn
            logger.error("backup step %s failed: %s", name, e)
            failed.append(f"{name}: {e}")
            summary[name] = None

    if kinds:
        out, manifest = _open_step(STEP_DUCKDB)
        for kind in kinds:
            run(kind, lambda kind=kind: _backup_kind(kind, out, manifest, manual=True))
    for step in steps:
        out, manifest = _open_step(step)
        before = len(failed)
        if step == STEP_DUCKDB:
            for kind in serving.KINDS:
                run(kind, lambda kind=kind: _backup_kind(kind, out, manifest))
        elif step == STEP_BOARDS:
            run(step, lambda: _backup_boards(out, manifest))
        elif step == STEP_OPS:
            run(step, lambda: _backup_ops(out, manifest))
        elif step == STEP_UW_RAW:
            run(step, lambda: _backup_uw_raw(out, manifest))
        elif step == STEP_EDGAR_DOCS:
            run(step, lambda: _backup_edgar_docs(out, manifest))
        _record(manifest, before, failed)
    if failed:
        raise BackupFailed("; ".join(failed))
    return summary


def _queue_is_idle() -> tuple[bool, str]:
    """``(idle, what is running)``: the builder is the DuckDB files' writer, so nothing of its may be in flight."""
    try:
        active = ops.queue_view(finished=0).get("active") or []
    except Exception as exc:                                  # noqa: BLE001 - no queue file yet is idle
        return True, f"the queue could not be read ({type(exc).__name__}), taken as idle"
    if not active:
        return True, "nothing queued or running"
    names = ", ".join(sorted({str(r.get("dataset")) for r in active}))
    return False, f"{len(active)} request(s) queued or running: {names}"


def _create_empty_stores(*, apply: bool) -> int:
    """``create-empty-stores``: a write to the live store, so it carries the three protections CLAUDE.md asks for."""
    from . import scaffold
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
        summary = run_backup(only=[STEP_DUCKDB, STEP_OPS])
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
    bk = sub.add_parser("backup", help="the weekly backup: every step, verified, the newest 4 copies kept")
    bk.add_argument("--only", default=None,
                    help="comma-separated steps to run, out of " + ", ".join(STEPS) + "; or one DuckDB kind ("
                         + ", ".join(serving.KINDS) + ") for a copy by hand before a deploy (kept apart from the "
                         "weekly copies). The default runs every step")
    empty = sub.add_parser("create-empty-stores",
                           help="create a serving file with its tables and no rows for every kind that has none; "
                                "an existing file is never touched. Says what it would do unless --apply is given")
    empty.add_argument("--apply", action="store_true",
                       help="actually create the files (the default only reports what is missing)")
    from . import restore
    restore.add_parsers(sub)
    args = p.parse_args(argv)
    if args.cmd == "create-empty-stores":
        return _create_empty_stores(apply=args.apply)
    if args.cmd in ("restore", "restore-check"):
        return restore.run(args)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        summary = run_backup(only=args.only)
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
