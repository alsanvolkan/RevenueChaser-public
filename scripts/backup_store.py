"""Consistent snapshots of the durable store (``.store/``).

The DuckDB files are the source of truth — ``docs/data_architecture.md`` says
they grow over time and CANNOT be regenerated from the app, yet (post-migration)
nothing ships a backup step. This module fixes that:

  python -m RevenueChaser.scripts.backup_store [--out DIR] [--keep N] [--keep-edgar N]
      [--keep-docs N] [--keep-discord N] [--keep-history N] [--keep-prices N] [--no-weekly] [--keep-mirror N]
      [--keep-mirror-big N] [--mirror-big-every-days N] [--mirror-big-now]

- Backs up ``raw.duckdb`` (structured data, ``--keep``), ``edgar.duckdb`` (raw
  10-K/20-F documents, ``--keep-edgar``) and ``discord.duckdb`` (the Discord archive,
  ``--keep-discord``) and ``history.duckdb`` (result history, #287, ``--keep-history``, default 5, plus a
  weekly copy like raw; skipped while the file does not exist) and ``prices.duckdb`` (the daily price tables, #156,
  ``--keep-prices``, default 2 and no weekly copy: the tables are append-only, so the newest snapshot holds every
  row an older one held), plus the ``edgar_docs/`` tree (a verified zip, ``--keep-docs``; an unchanged tree keeps the
  newest zip instead of zipping again, #336)
  and the ``discord_media/`` and SPX Gamma ``data/`` trees (incremental mirrors, see below).
- Acquires the store's write handle through ``rawstore`` (so a lock held by the
  dashboard or a scheduled job is waited out, every attempt), CHECKPOINTs so the
  WAL is flushed into the main file, then copies the file to
  ``<name>_<UTC timestamp>.duckdb``. A busy store or a failed copy is retried
  (bounded) instead of failing the night's backup on the first conflict.
- VERIFIES the snapshot before keeping it: the backup is reopened read-only and
  every table's row count AND content checksum (sum of row hashes) is compared
  against the live store. A torn or raced copy is discarded and retried
  (bounded), never retained; a copy that opens but cannot be read (a damaged data block) counts as a
  mismatch. ``edgar_docs/`` archives are verified too (CRC of every member + member count).
- Mirrors ``discord_media/`` (Discord attachments) into ``<backup dir>/discord_media_mirror/``
  instead of zipping it (#248). Only new or changed files are read (a ``stat`` walk
  decides), each copy goes through ``discord_media_mirror/.tmp/`` and is verified by size
  and SHA-256 before it replaces the mirror file, a file still being written
  (modified in the last ``MEDIA_SETTLE_SECONDS``) waits for the next run, and a file
  that vanished from the source stays in the mirror: that is what protects the
  attachments of messages deleted on Discord, whose links cannot be fetched again.
- Mirrors the SPX Gamma data tree (``module_spx_gamma/data``: the UW day files, ``brier.json``, the GEX
  history) into ``<backup dir>/spx_gamma_mirror/`` the same way (#276). UW serves only recent days, so the
  tree cannot be fetched again; the mirror is additive too.
- Keeps the first verified ``raw.duckdb`` snapshot of every ISO week in ``<backup dir>/weekly/`` and never
  prunes it (#277, a stopgap from before the change history; that has landed, so the owner turns it off with
  ``--no-weekly`` or ``RAW_STORE_BACKUP_WEEKLY=0``; the default is on). ``weekly/`` is a sub-folder, so the prune globs and ``restore_store --latest`` never see it.
- With ``RAW_STORE_BACKUP_MIRROR`` set, copies the newest snapshot of each kind and the three mirror folders
  into ``<that folder>/RevenueChaser-backup`` (it may be an existing Drive folder: nothing outside the sub-folder
  is written, pruned or deleted) after everything above was verified (#275), writing each file under a temporary
  name, fingerprinting it there again and only then renaming it. The small kinds (discord, history) go every run
  and the newest ``--keep-mirror`` stay; the big kinds (raw, edgar, the edgar_docs zip) go only when the newest
  copy there is ``--mirror-big-every-days`` (default 7) days old, or with ``--mirror-big-now``, and the newest
  ``--keep-mirror-big`` stay (#349: Drive keeps every pruned copy in its trash, inside the quota, for 30 days).
  The prices snapshot (#156) goes last, after the mirror folders, so a failed upload of it costs no other copy,
  once every ``MIRROR_PRICES_EVERY_DAYS`` (7) days, and ``KEEP_MIRROR_PRICES`` (1) copy stays.
  The weekly copies stay on this machine. The run logs ``off-machine copy OK <iso>`` (with ``big files skipped
  until <date>`` when it did not send them) or ``off-machine copy FAILED <iso>: <why>``, which the Automations
  row reads; a night in which an earlier step failed makes no copy and logs the second form too. Unset, the
  step does nothing.
- A step that fails is reported as ``<step> FAILED: <why>`` (an unexpected error also prints its type and stack)
  and the steps after it still run; the exit code is 1 when any failed (or a damaged SPX file was held back), and
  only the off-machine copy is skipped.
- Prunes old snapshots beyond the keep counts, only AFTER a verified write, so a
  failure can never eat existing backups.
- Records every file it writes locally (size + SHA-256, at copy time) in ``.backup_manifest.json`` and, as the last
  step every ``--scan-every-days`` (default 7) or with ``--scan-now``, re-reads the local folder against it
  (``integrity_scan``, #309 L1): read-only for the backups, ``integrity scan FAILED: <path>: <why>`` and exit 1 on a
  mismatch. Then logs ``backup sizes <iso>: local <n> bytes[, off-machine <n> bytes]`` and writes ``.backup_sizes.json``
  (#309 part 3): the local size from a ``stat`` walk, the off-machine size from ``.offmachine_manifest.json`` (what
  the copy wrote and kept there). The off-machine folder is never walked or hashed.

Usage notes:

- A snapshot is a plain DuckDB file: restore with
  ``python -m RevenueChaser.scripts.restore_store`` (lists snapshots, verifies,
  keeps the current file aside) — see docs/data_architecture.md, Backups. ``discord_media/``
  is restored by copying ``discord_media_mirror/`` back over it, the SPX Gamma tree by copying
  ``spx_gamma_mirror/`` back over ``module_spx_gamma/data`` (or ``SPX_GAMMA_DATA_ROOT``); a weekly copy
  by ``restore_store --from <backup dir>/weekly --latest raw``.
- ``--out`` defaults to ``%LOCALAPPDATA%\\RevenueChaser\\backups`` on Windows and
  ``~/.local/share/RevenueChaser/backups`` elsewhere; override with
  ``RAW_STORE_BACKUP_DIR`` or ``--out``.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import time
import traceback
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import duckdb

from RevenueChaser.core import config, rawstore


# #248: the Discord attachment tree is mirrored, not zipped. Name chosen so that restore's
# ``discord_*.duckdb`` snapshot glob never matches it.
MEDIA_MIRROR_DIRNAME = "discord_media_mirror"
MEDIA_SETTLE_SECONDS = 600          # a file modified more recently than this may still be downloading
_MEDIA_TMP_DIRNAME = ".tmp"
_MEDIA_MTIME_TOLERANCE_NS = 2 * 10**9   # NTFS/FAT round mtimes (FAT to 2 s); copy2 keeps the mtime

# #276: the SPX Gamma data tree is mirrored the same way (the UW day files cannot be fetched again).
SPX_MIRROR_DIRNAME = "spx_gamma_mirror"
# Q5 (avo, 2026-10-04): two files of that tree are rewritten in place, so every version of them also gets a
# dated copy, in a folder BESIDE the mirror (restoring the mirror over the data tree must not carry it along).
SPX_VERSIONS_DIRNAME = "spx_gamma_versions"
SPX_VERSIONED_FILES = ("brier.json", "spx_gamma_gex_history.json")     # module_spx_gamma/config.py:25-26

# #277: one raw snapshot per ISO week, in a sub-folder the prune and restore globs never reach.
WEEKLY_DIRNAME = "weekly"

# #275 (review M2): RAW_STORE_BACKUP_MIRROR may be an existing Google Drive folder that holds other files, so
# the off-machine copy writes, prunes and cleans only inside this sub-folder of it.
OFFMACHINE_SUBDIR = "RevenueChaser-backup"
# #349: a Drive for desktop folder moves every pruned copy into the Drive trash, which counts toward the Google quota
# for 30 days, so a daily 1.9 GB set held about 60 GB of the account. The big kinds go there once a week instead.
MIRROR_BIG_KINDS = ("raw_*.duckdb", "edgar_*.duckdb", "edgar_docs_*.zip")
MIRROR_SMALL_KINDS = ("discord_*.duckdb", "history_*.duckdb")
KEEP_MIRROR = 3
KEEP_MIRROR_BIG = 2
MIRROR_BIG_EVERY_DAYS = 7
# #156 (review 2026-10-09): the prices snapshot (~2.4 GiB once filled) goes LAST, after the mirror folders, so a failed
# upload of the biggest file (Drive quota, a timeout) costs no other copy: the SPX and Discord mirrors cannot be
# fetched again, the prices can (F1). It goes once every MIRROR_PRICES_EVERY_DAYS and KEEP_MIRROR_PRICES copy stays
# there (avo's card, 12:06Z 2026-10-09: "Haftada bir", keep 1), whatever --mirror-big-every-days and --keep-mirror-big
# say; --mirror-big-now sends it too.
MIRROR_PRICES_KIND = "prices_*.duckdb"
MIRROR_PRICES_EVERY_DAYS = 7
KEEP_MIRROR_PRICES = 1
# #309 L1: every file the run writes into the backup folder is recorded at copy time (relative path, size, SHA-256)
# in MANIFEST_NAME at the top of that folder, beside the mirrors and never inside one (copying a mirror back over its
# tree must not carry it). The integrity scan re-reads the folder against it every SCAN_EVERY_DAYS days; its last
# result is SCAN_MARKER_NAME. OFFMACHINE_MANIFEST_NAME, also in the LOCAL backup folder, records what the off-machine
# copy wrote and kept there (size; SHA-256 where it was computed anyway): the Settings row shows the off-machine folder
# size from it, so neither the job nor the dashboard ever walks that folder (a Drive walk took ~20 min, 2026-10-16).
MANIFEST_NAME = ".backup_manifest.json"
OFFMACHINE_MANIFEST_NAME = ".offmachine_manifest.json"
SCAN_MARKER_NAME = ".integrity_scan.json"
SCAN_EVERY_DAYS = 7
_MANIFEST_VERSION = 1
_MIRROR_STAMP_RE = re.compile(r"_(\d{8})_\d{6}_\d{6}\.(?:duckdb|zip)$")
_WEEKLY_OFF_VALUES = ("0", "false", "no", "off")
_SNAPSHOT_STAMP_RE = re.compile(r"^[a-z]+_(\d{8})_\d{6}_\d{6}\.duckdb$")


class BackupError(RuntimeError):
    pass


class _StoreBusy(BackupError):
    pass


class _VerificationError(BackupError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_backup_dir() -> Path:
    """Platform default destination (env override ``RAW_STORE_BACKUP_DIR`` wins)."""
    override = os.environ.get("RAW_STORE_BACKUP_DIR") or config.RAW_STORE_BACKUP_DIR
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "RevenueChaser" / "backups"


class _Manifest:
    """``{relative path: {"size": bytes, "sha256": hex or None}}`` of the files the run wrote under ``root`` (#309).

    ``sha256`` None = the size is known but the file was not hashed (a file that was already there); the integrity
    scan hashes it once after a self-check. Saved atomically (temporary name, then a rename). A manifest that is
    unreadable, or missing once a scan has run, is replaced by a new one whose files the next scan hashes again; that
    scan reports the loss as a failure (``lost``), so it is never a silent pass.
    """

    def __init__(self, file: Path, root: Path, files: dict | None = None, lost: str | None = None):
        self.file, self.root, self.files = file, root, files if files is not None else {}
        # Why the previous manifest could not be used ("missing", "unreadable (...)"): kept in the file until the
        # next integrity scan reports it, so a lost manifest is never a silent pass (review C, 2026-10-17).
        self.lost = lost

    @classmethod
    def load(cls, file: Path, root: Path, same_root: bool = False) -> "_Manifest":
        """``same_root``: a manifest written for another folder (the off-machine setting changed) starts empty."""
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            files = data["files"]
            if data.get("version") != _MANIFEST_VERSION or not isinstance(files, dict):
                raise ValueError(f"not a version {_MANIFEST_VERSION} manifest")
        except FileNotFoundError:
            # None before the first scan; once a scan marker exists the manifest should be there.
            if not file.with_name(SCAN_MARKER_NAME).is_file():
                return cls(file, root)
            print(f"backup manifest WARNING: {file} is missing; starting a new one, the next integrity scan hashes "
                  f"every file again", file=sys.stderr)
            return cls(file, root, lost="missing")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"backup manifest WARNING: {file} is unreadable ({exc}); starting a new one, the next integrity "
                  f"scan hashes every file again", file=sys.stderr)
            return cls(file, root, lost=f"unreadable ({exc})")
        if same_root and data.get("root") != str(root):
            return cls(file, root)
        return cls(file, root, files, data.get("lost"))

    def key(self, path: Path) -> str:
        return Path(path).relative_to(self.root).as_posix()

    def record(self, path: Path, size: int, sha256: str | None) -> None:
        self.files[self.key(path)] = {"size": int(size), "sha256": sha256}

    def note(self, path: Path, size: int) -> None:
        """A file that is there but was not written now: its size, unless the manifest already has the file."""
        self.files.setdefault(self.key(path), {"size": int(size), "sha256": None})

    def forget(self, paths) -> None:
        for p in paths:
            self.files.pop(self.key(p), None)

    def total_bytes(self) -> int:
        return sum(int(e.get("size") or 0) for e in self.files.values())

    def save(self) -> None:
        tmp = self.file.with_name(self.file.name + ".tmp")
        body = {"version": _MANIFEST_VERSION, "root": str(self.root), "files": dict(sorted(self.files.items()))}
        if self.lost:
            body["lost"] = self.lost
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(body, indent=0), encoding="utf-8")
            os.replace(tmp, self.file)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise BackupError(f"could not write the backup manifest {self.file}: {exc}") from exc


def _local_manifest(out: Path) -> _Manifest:
    return _Manifest.load(out / MANIFEST_NAME, out)


def _save_best_effort(m: _Manifest, what: str = "backup manifest",
                      then: str = "the next integrity scan adopts the file") -> None:
    """Save ``m``; a manifest that cannot be written (a locked file) is a warning, never a failed step (review C
    SHOULD-1, delta review DR-3): the files themselves are already final and verified."""
    try:
        m.save()
    except (BackupError, OSError) as exc:
        print(f"{what} WARNING: {exc}; {then}", file=sys.stderr)


def _record_written(out: Path, written: Path | None = None, removed=()) -> None:
    """Record one file the run just wrote under ``out`` (hashed now, from the file itself) and drop pruned ones.

    Best effort (review C SHOULD-1): the file is already final and verified, so a manifest that cannot be updated is a
    warning, never a failed backup step; the next integrity scan self-checks and adopts a file the manifest lacks.
    """
    try:
        m = _local_manifest(out)
        if written is not None:
            m.record(written, written.stat().st_size, _sha256(written))
        m.forget(removed)
    except (BackupError, OSError) as exc:
        print(f"backup manifest WARNING: {exc}; the next integrity scan adopts the file", file=sys.stderr)
        return
    _save_best_effort(m)


def _table_fingerprints(db_path: Path) -> dict[str, tuple[int, int]]:
    """``{table: (row count, content checksum)}`` for ``db_path``, read-only.

    The checksum is the sum of every row's hash (order-independent, and unlike
    XOR a duplicated row does not cancel out). ~0.2 s for 20 M rows.
    """
    if not db_path.is_file():
        raise _StoreBusy(f"store file not found: {db_path}")
    try:
        conn = duckdb.connect(str(db_path), read_only=True)
    except duckdb.Error as exc:
        raise _StoreBusy(f"could not open {db_path} read-only (another writer?): {exc}") from exc
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
        # A file with an intact header but a damaged data block opens, then raises ``duckdb.IOException`` ("Corrupt
        # database file: computed checksum ...") here, and that is neither a BackupError nor an OSError: it would end
        # the night with a traceback, skip the retry and leave the copy's .tmp behind (review M3).
        raise _VerificationError(f"could not read {db_path.name}: {exc}") from exc
    finally:
        conn.close()


# Kept for callers/tests that only need counts.
def _table_counts(db_path: Path) -> dict[str, int]:
    return {t: c for t, (c, _) in _table_fingerprints(db_path).items()}


def _verify(db_path: Path, backup_path: Path) -> None:
    """Compare every table's row count and content checksum, snapshot vs live."""
    live = _table_fingerprints(db_path)
    snap = _table_fingerprints(backup_path)
    missing_in_snap = sorted(set(live) - set(snap))
    missing_in_live = sorted(set(snap) - set(live))
    if missing_in_snap or missing_in_live:
        raise _VerificationError(
            "table set mismatch: "
            f"{'missing in snapshot: ' + ', '.join(missing_in_snap) if missing_in_snap else ''} "
            f"{'missing in live: ' + ', '.join(missing_in_live) if missing_in_live else ''}".strip())
    diffs = [(t, live[t], snap[t]) for t in live if live[t] != snap[t]]
    if diffs:
        shown = ", ".join(
            f"{t} live={a[0]} snap={b[0]}" + ("" if a[0] != b[0] else " (content differs)")
            for t, a, b in diffs[:6])
        more = f" (+{len(diffs) - 6} more)" if len(diffs) > 6 else ""
        raise _VerificationError(f"mismatch: {shown}{more}")


def _prune(out: Path, prefix: str, keep: int, manifest: _Manifest | None = None) -> list[Path]:
    """Delete oldest ``<prefix>_*.duckdb`` snapshots beyond ``keep`` (keeps newest)."""
    return _prune_sorted(sorted(out.glob(f"{prefix}_*.duckdb")), keep, manifest)


def _prune_sorted(snaps: list[Path], keep: int, manifest: _Manifest | None) -> list[Path]:
    """Delete all but the newest ``keep`` of ``snaps``; with ``manifest`` the removed ones leave it and a kept one it
    lacks (a copy from before #309) is noted with its size: the listing the prune made anyway, no extra walk."""
    removed = []
    cut = snaps[:-keep] if keep > 0 else snaps
    for p in cut:
        p.unlink(missing_ok=True)
        removed.append(p)
    if manifest is not None:
        manifest.forget(removed)
        for p in snaps[len(cut):]:
            if manifest.key(p) not in manifest.files:
                manifest.note(p, p.stat().st_size)
    return removed


def _snapshot_one(db_path: Path, out: Path, prefix: str, keep: int,
                  attempts: int) -> Path:
    """Write one verified snapshot of ``db_path``; return its path."""
    db = db_path.resolve()
    if not db.is_file():
        raise BackupError(f"store file not found: {db}")
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob(f"{prefix}_*.duckdb.tmp"):      # a run that was killed mid-copy
        with contextlib.suppress(OSError):
            stale.unlink(missing_ok=True)

    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        # Microseconds in the name: snapshots are unique and, because the stamp
        # is monotonic per call, sort (and prune) strictly by creation time.
        stamp = _utcnow().strftime("%Y%m%d_%H%M%S_%f")
        dest = out / f"{prefix}_{stamp}.duckdb"
        tmp = dest.with_name(dest.name + ".tmp")

        # CHECKPOINT the target DB so the WAL is flushed into the main file we
        # copy (works for both raw.duckdb and edgar.duckdb). Through rawstore:
        # a lock held by the dashboard / a scheduled job is waited out with the
        # peer budget instead of failing the whole backup on first contact.
        try:
            conn = rawstore._connect_with_retry(rawstore._PEER_LOCK_DELAYS, db)
            try:
                conn.execute("CHECKPOINT")
            finally:
                with rawstore._ATTACH_LOCK:
                    conn.close()
        except (duckdb.Error, rawstore.RawStoreError) as exc:
            last = _StoreBusy(f"could not open {db} to checkpoint: {exc}")
            time.sleep(1.0)
            continue
        try:
            shutil.copy2(db, tmp)
        except OSError as exc:
            # Windows may refuse to share a file another process just opened.
            last = BackupError(f"could not copy {db}: {exc}")
            tmp.unlink(missing_ok=True)
            time.sleep(1.0)
            continue

        try:
            _verify(db, tmp)
        except _VerificationError as exc:
            last = exc
            tmp.unlink(missing_ok=True)
            time.sleep(0.5 * (attempt + 1))
            continue
        except _StoreBusy as exc:
            last = exc
            tmp.unlink(missing_ok=True)
            time.sleep(1.0)
            continue

        try:
            os.replace(tmp, dest)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise BackupError(f"could not finalize {dest}: {exc}") from exc
        # Hash and record the new file, then let the pruned ones leave the manifest, and only then delete them: a run
        # killed in between leaves a listed file that still exists, never a "missing" one (review C SHOULD-1).
        _record_written(out, dest)
        snaps = sorted(out.glob(f"{prefix}_*.duckdb"))
        _record_written(out, removed=snaps[:-keep] if keep > 0 else snaps)
        _prune_sorted(snaps, keep, None)
        return dest

    raise last if last else BackupError("snapshot failed")


def snapshot(db_path: Path | None = None, out_dir: Path | None = None,
             keep: int = 5, attempts: int = 3) -> Path:
    """Write one verified snapshot of ``raw.duckdb``; return its path.

    ``db_path`` defaults to the configured store (tests pass a tmp DB). ``keep``
    prunes older verified snapshots in the destination.
    """
    return _snapshot_one(db_path or rawstore.DB_PATH, out_dir or default_backup_dir(),
                         "raw", keep, attempts)


def weekly_enabled() -> bool:
    """False when ``RAW_STORE_BACKUP_WEEKLY`` is 0/false/no/off (the switch for when #279's change history lands)."""
    return str(config.RAW_STORE_BACKUP_WEEKLY).strip().lower() not in _WEEKLY_OFF_VALUES


def _iso_week_of(snapshot_name: str) -> tuple[int, int] | None:
    """``(ISO year, ISO week)`` of a ``<kind>_<UTC stamp>.duckdb`` snapshot name, else None."""
    m = _SNAPSHOT_STAMP_RE.match(snapshot_name)
    if not m:
        return None
    iso = datetime.strptime(m.group(1), "%Y%m%d").isocalendar()
    return iso[0], iso[1]


def keep_weekly(snap: Path, out_dir: Path | None = None, enabled: bool | None = None) -> Path | None:
    """Keep ``snap`` (a verified ``raw_*`` or ``history_*`` snapshot) in ``<backup dir>/weekly/`` when it is the
    first of its ISO week for its kind (#287: a history copy never stands in for the raw one of that week).

    Returns the new weekly copy, or None when this week already has one (or the copy is switched off).
    The copy keeps the snapshot's name, so ``restore_store --from <dir>/weekly --latest raw`` reads it. It is
    written under a temporary name, fingerprinted against the snapshot (row count and content checksum per
    table) and only then renamed; a failure leaves nothing under the final name and the next night tries again.
    ``_prune`` globs the top folder only, so nothing here is ever pruned.
    """
    if not (weekly_enabled() if enabled is None else enabled):
        return None
    week = _iso_week_of(snap.name)
    if week is None:
        raise BackupError(f"not a snapshot name: {snap.name}")
    out = out_dir or default_backup_dir()
    wdir = out / WEEKLY_DIRNAME
    wdir.mkdir(parents=True, exist_ok=True)
    for stale in wdir.glob("*.tmp"):                      # a run that was killed mid-copy
        stale.unlink(missing_ok=True)
    kind = snap.name.split("_", 1)[0]
    if any(_iso_week_of(p.name) == week for p in wdir.glob(f"{kind}_*.duckdb")):
        return None
    dest = wdir / snap.name
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        shutil.copy2(snap, tmp)
        if _table_fingerprints(tmp) != _table_fingerprints(snap):
            raise _VerificationError(f"weekly copy of {snap.name} does not match the snapshot")
        os.replace(tmp, dest)
    except BackupError:
        tmp.unlink(missing_ok=True)
        raise
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"could not write the weekly copy of {snap.name}: {exc}") from exc
    _record_written(out, dest)
    return dest


def snapshot_edgar(db_path: Path | None = None, out_dir: Path | None = None,
                   keep: int = 2, attempts: int = 3) -> Path | None:
    """Verified snapshot of ``edgar.duckdb`` (the raw-document store).

    Returns None when the EDGAR store does not exist yet (nothing to back up).
    """
    from RevenueChaser.core import rawstore
    db = db_path or rawstore.edgar_db_path()
    if not Path(db).is_file():
        return None
    return _snapshot_one(db, out_dir or default_backup_dir(), "edgar", keep, attempts)


def snapshot_discord(db_path: Path | None = None, out_dir: Path | None = None,
                     keep: int = 2, attempts: int = 3) -> Path | None:
    """Verified snapshot of ``discord.duckdb`` (the Discord archive, #72).

    Returns None when the Discord store does not exist yet (nothing to back up).
    """
    db = db_path or rawstore.discord_db_path()
    if not Path(db).is_file():
        return None
    return _snapshot_one(db, out_dir or default_backup_dir(), "discord", keep, attempts)


def snapshot_history(db_path: Path | None = None, out_dir: Path | None = None,
                     keep: int = 5, attempts: int = 3) -> Path | None:
    """Verified snapshot of ``history.duckdb`` (result history, #287); the last ``keep`` nightly copies stay.

    Returns None when the history file does not exist yet (no writer has created it; nothing to back up).
    """
    db = db_path or rawstore.history_db_path()
    if not Path(db).is_file():
        return None
    return _snapshot_one(db, out_dir or default_backup_dir(), "history", keep, attempts)


def snapshot_prices(db_path: Path | None = None, out_dir: Path | None = None,
                    keep: int = 2, attempts: int = 3) -> Path | None:
    """Verified snapshot of ``prices.duckdb`` (the daily price tables, #156); the last ``keep`` nightly copies stay.

    No weekly copy: the price tables are append-only (a bar, an action, a watermark key is never removed), so the
    newest snapshot holds every row an older one held, and a weekly copy kept forever would cost ~2.4 GiB a week.
    Returns None when the file does not exist yet (nothing to back up).
    """
    db = db_path or rawstore.prices_db_path()
    if not Path(db).is_file():
        return None
    return _snapshot_one(db, out_dir or default_backup_dir(), "prices", keep, attempts)


def _prune_archives(out: Path, prefix: str, keep: int, manifest: _Manifest | None = None) -> list[Path]:
    return _prune_sorted(sorted(p for p in out.glob(f"{prefix}_*.zip") if ".partial" not in p.name), keep, manifest)


# #336: the fingerprint of the tree an archive was made from, kept in the zip's own comment (no extra file, so
# prune and the off-machine copy carry it unchanged). A run whose tree has the same fingerprint as the newest archive
# keeps that archive instead of zipping the whole tree (~1 GB on 2026-10-08) again.
_DOCS_FINGERPRINT_PREFIX = b"revenuechaser-edgar-docs-fingerprint v1 sha256:"


class DocsArchive(NamedTuple):
    """The ``edgar_docs`` archive a run ends with; ``reused`` = the tree was unchanged, no new archive was made."""
    path: Path
    reused: bool


def _tree_fingerprint(d: Path) -> bytes:
    """SHA-256 over every file of the tree, sorted: relative path, size and modified time in nanoseconds.

    A ``stat`` walk, no file is read. Taken before zipping, so a file that changes while the zip runs makes the
    next run's fingerprint differ and that run zips again (never the other way round)."""
    h = hashlib.sha256()
    for rel, size, mtime_ns in sorted((p.relative_to(d).as_posix(), st.st_size, st.st_mtime_ns)
                                      for p in d.rglob("*") if p.is_file() for st in (p.stat(),)):
        h.update(f"{rel}\0{size}\0{mtime_ns}\n".encode("utf-8", "surrogateescape"))
    return _DOCS_FINGERPRINT_PREFIX + h.hexdigest().encode("ascii")


def _archive_reusable(archive: Path, fingerprint: bytes, expected_docs: int) -> bool | None:
    """True: ``archive`` was made from a tree with this fingerprint and still lists every document.
    False: another tree (or an archive from before #336, without a fingerprint), or it could not be opened (a file
    another process holds: zip again, keep it). None: it is damaged (not a readable zip, or a document is missing).

    The cheap nightly check: the zip opens, its comment carries the fingerprint and its central directory lists the
    ``expected_docs`` documents. Member CRCs were checked when the archive was written; reading ~1 GB again every
    night is left to a periodic scan (#309 L1)."""
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


def archive_edgar_docs(src: Path | None = None, out_dir: Path | None = None, keep: int = 2) -> DocsArchive | None:
    """Zip the ``edgar_docs/`` tree (the durable raw HTML) unless the newest archive already holds it; prune.

    Returns None when there is nothing to back up. The docs are files, not a DuckDB snapshot, so they need their
    own tree copy. When the tree's fingerprint equals the one stored in the newest archive and that archive passes
    the cheap check, it is returned with ``reused=True`` and nothing is written (#336). A newest archive that is
    damaged is never reused: a new one is zipped and verified, and then the damaged one is removed, so the prune
    never keeps it in place of a good older archive.
    """
    d = Path(src or config.EDGAR_DOCS_DIR)
    if not d.is_dir() or not any(d.rglob("*.html.gz")):
        return None
    out = out_dir or default_backup_dir()
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("edgar_docs_*.partial.zip"):    # a run that was killed mid-zip
        with contextlib.suppress(OSError):
            stale.unlink(missing_ok=True)
    expected = sum(1 for _ in d.rglob("*.html.gz"))
    fingerprint = _tree_fingerprint(d)
    newest = sorted(p for p in out.glob("edgar_docs_*.zip") if ".partial" not in p.name)[-1:]
    damaged = None
    if newest:
        reusable = _archive_reusable(newest[0], fingerprint, expected)
        if reusable:
            removed = _prune_archives(out, "edgar_docs", keep)
            if removed:
                _record_written(out, removed=removed)
            return DocsArchive(newest[0], True)
        if reusable is None:
            damaged = newest[0]
    stamp = _utcnow().strftime("%Y%m%d_%H%M%S_%f")
    tmp_base = out / f"edgar_docs_{stamp}.partial"
    try:
        archive = Path(shutil.make_archive(str(tmp_base), "zip", root_dir=str(d)))
    except OSError as exc:                                # a full disk leaves a half-written zip: remove it
        with contextlib.suppress(OSError):
            Path(f"{tmp_base}.zip").unlink(missing_ok=True)
        raise BackupError(f"could not zip {d}: {exc}") from exc
    try:
        _stamp_fingerprint(archive, fingerprint)
        _verify_archive(archive, expected)
    except BackupError:
        with contextlib.suppress(OSError):
            archive.unlink(missing_ok=True)
        raise
    final = out / f"edgar_docs_{stamp}.zip"
    try:
        os.replace(archive, final)
    except OSError as exc:                                # N4: the next run's sweep would find it, but say so now
        with contextlib.suppress(OSError):
            archive.unlink(missing_ok=True)
        raise BackupError(f"could not finalize {final}: {exc}") from exc
    removed = []
    if damaged is not None:
        with contextlib.suppress(OSError):              # the new archive is verified; a locked file is pruned later
            damaged.unlink(missing_ok=True)
            removed.append(damaged)
    removed += _prune_archives(out, "edgar_docs", keep)
    _record_written(out, final, removed)
    return DocsArchive(final, False)


def _stamp_fingerprint(archive: Path, fingerprint: bytes) -> None:
    """Write the tree's fingerprint into the zip comment (append mode rewrites only the central directory)."""
    import zipfile
    try:
        with zipfile.ZipFile(archive, "a") as zf:
            zf.comment = fingerprint
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as exc:
        raise BackupError(f"edgar_docs archive unreadable: {exc}") from exc


def snapshot_edgar_docs(src: Path | None = None, out_dir: Path | None = None,
                        keep: int = 2) -> Path | None:
    """The path of the archive ``archive_edgar_docs`` ends with (new or reused), or None when there are no docs."""
    res = archive_edgar_docs(src, out_dir, keep)
    return res.path if res else None


class MirrorResult(NamedTuple):
    """What one mirror run did (``skipped`` = in flight, retried next run).

    ``rejected`` names the JSON files whose source is damaged: the previous mirror copy was kept (SPX tree only).
    """
    copied: int
    unchanged: int
    skipped: int
    total: int
    dest: Path
    rejected: tuple[str, ...] = ()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_file(src: Path, dst: Path) -> None:
    """Copy bytes and metadata (``copy2`` keeps the mtime the next run compares against)."""
    shutil.copy2(src, dst)


def _stamp(st: os.stat_result) -> tuple[int, int]:
    return st.st_size, st.st_mtime_ns


def _mirror_is_current(mirror_file: Path, st: os.stat_result) -> bool:
    try:
        ms = mirror_file.stat()
    except FileNotFoundError:
        return False
    return (ms.st_size == st.st_size
            and abs(ms.st_mtime_ns - st.st_mtime_ns) <= _MEDIA_MTIME_TOLERANCE_NS)


def _is_json(path: Path) -> bool:
    try:
        json.loads(path.read_bytes().decode("utf-8"))
    except (ValueError, OSError):          # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return False
    return True


def _keep_dated_copy(tmp: Path, versions_dir: Path, name: str, st: os.stat_result,
                     manifest: _Manifest | None = None) -> None:
    """File a verified copy of one version under ``<stem>.<UTC mtime>.<ext>``; an existing dated copy is never replaced.

    The dated file is a second write, so it is checked against the verified copy it was cut from (SHA-256) before it
    gets its name; a mismatch discards it and fails the run (the mirror file then waits for the next night)."""
    stem, dot, ext = name.rpartition(".")
    when = datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    dated = versions_dir / f"{stem}.{when}.{ext}"
    if dated.exists():
        return
    versions_dir.mkdir(parents=True, exist_ok=True)
    part = dated.with_name(dated.name + ".part")
    try:
        shutil.copyfile(tmp, part)
        part_hash = _sha256(part)
        if part_hash != _sha256(tmp):
            raise BackupError(f"the dated copy of {name} does not match its verified copy (SHA-256); not kept")
        os.replace(part, dated)
        if manifest is not None:
            manifest.record(dated, st.st_size, part_hash)
    except BackupError:
        part.unlink(missing_ok=True)
        raise
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise BackupError(f"could not keep a dated copy of {name}: {exc}") from exc


def _mirror_one(src_file: Path, final: Path, tmp_dir: Path, st: os.stat_result, n: int,
                what: str = "discord_media", json_guard: bool = False,
                versions_dir: Path | None = None, manifest: _Manifest | None = None) -> str:
    """Copy ``src_file`` to ``final`` through ``tmp_dir``, verified; return what happened.

    ``"copied"``; ``"skipped"`` = the source changed while it was copied (the download is still running),
    not an error, the file is left for the next run; ``"rejected"`` = ``json_guard`` is on, the new copy is
    not valid JSON and the mirror holds a valid copy: that one is kept (a corrupt file must never replace a
    good one). A STABLE source whose copy hashes differently is an error: ``BackupError``, and ``final``
    keeps its previous content. With ``versions_dir`` every copied version also gets a dated copy there.
    """
    tmp = tmp_dir / f"{n}.part"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        _copy_file(src_file, tmp)
        src_hash, tmp_hash = _sha256(src_file), _sha256(tmp)
        if _stamp(src_file.stat()) != _stamp(st):
            tmp.unlink(missing_ok=True)
            return "skipped"
        if tmp.stat().st_size != st.st_size or tmp_hash != src_hash:
            raise BackupError(f"{what} copy of {src_file.name} does not match its source "
                              f"(size/SHA-256); the previous mirror copy was kept")
        if json_guard and not _is_json(tmp) and _is_json(final):
            tmp.unlink(missing_ok=True)
            return "rejected"
        if versions_dir is not None and _is_json(tmp):
            _keep_dated_copy(tmp, versions_dir, final.name, st, manifest)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, final)
        if manifest is not None:                    # the hash of the verified copy that now has the final name
            manifest.record(final, st.st_size, tmp_hash)
        return "copied"
    except FileNotFoundError:
        tmp.unlink(missing_ok=True)      # the source vanished mid-copy: nothing to back up
        return "skipped"
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"could not mirror {src_file.name}: {exc}") from exc
    except BackupError:
        tmp.unlink(missing_ok=True)
        raise


def _mirror_tree(d: Path, dest: Path, settle_s: float, what: str, json_guard: bool = False,
                 versions_dir: Path | None = None, versioned: tuple[str, ...] = (),
                 manifest: _Manifest | None = None) -> MirrorResult | None:
    """Additive, incremental, verified mirror of ``d`` into ``dest`` (the #248 mechanism, any tree).

    ``json_guard`` and ``versions_dir`` + ``versioned`` (relative paths) are the SPX tree's extras, see ``_mirror_one``.
    ``manifest`` (#309) records every copied file with its hash and notes the size of an unchanged one it lacks.
    """
    if not d.is_dir():
        return None
    files = sorted(p for p in d.rglob("*") if p.is_file())
    if not files:
        return None
    tmp_dir = dest / _MEDIA_TMP_DIRNAME
    dest.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(tmp_dir, ignore_errors=True)       # leftovers of a run that was killed

    cutoff = time.time() - settle_s
    copied = unchanged = skipped = total = 0
    rejected: list[str] = []
    for n, f in enumerate(files):
        rel = f.relative_to(d)
        try:
            st = f.stat()
        except FileNotFoundError:
            continue                                  # deleted since the walk: gone, not skipped
        except OSError as exc:
            raise BackupError(f"could not read {f.name}: {exc}") from exc
        total += 1
        if rel.parts[0] == _MEDIA_TMP_DIRNAME:
            skipped += 1                              # would collide with the temp dir
            continue
        final = dest / rel
        if _mirror_is_current(final, st):
            unchanged += 1
            if manifest is not None:
                manifest.note(final, st.st_size)
        elif st.st_mtime > cutoff:
            skipped += 1
        else:
            outcome = _mirror_one(f, final, tmp_dir, st, n, what, json_guard,
                                  versions_dir if rel.as_posix() in versioned else None, manifest)
            if outcome == "copied":
                copied += 1
            elif outcome == "rejected":
                rejected.append(rel.as_posix())
            else:
                skipped += 1
    return MirrorResult(copied, unchanged, skipped, total, dest, tuple(rejected))


def mirror_discord_media(src: Path | None = None, out_dir: Path | None = None,
                         settle_s: float = MEDIA_SETTLE_SECONDS) -> MirrorResult | None:
    """Mirror the ``discord_media/`` tree into ``<backup dir>/discord_media_mirror/`` (#248).

    Additive and incremental: a file is "unchanged" when the mirror holds a copy of the same
    size and an mtime within 2 s (``stat`` only, no file is read), so a nightly run reads
    only new or changed files. A file modified within ``settle_s`` seconds may still be
    downloading and is skipped (counted in ``skipped``; the next run picks it up). Files
    that disappeared from the source stay in the mirror. Returns None when there is no
    media to back up (nothing is created then).
    """
    out = out_dir or default_backup_dir()
    return _mirror_into(out, lambda m: _mirror_tree(Path(src or config.DISCORD_MEDIA_DIR), out / MEDIA_MIRROR_DIRNAME,
                                                    settle_s, "discord_media", manifest=m))


def _mirror_into(out: Path, run) -> MirrorResult | None:
    """Run one local mirror with the backup folder's manifest, saved even when a file fails half-way (#309)."""
    m = _local_manifest(out)
    try:
        return run(m)
    finally:
        if out.is_dir():
            _save_best_effort(m)


def mirror_spx_gamma(src: Path | None = None, out_dir: Path | None = None,
                     settle_s: float = MEDIA_SETTLE_SECONDS) -> MirrorResult | None:
    """Mirror the SPX Gamma data tree into ``<backup dir>/spx_gamma_mirror/`` (#276, review B3).

    Same rules as ``mirror_discord_media``: new or changed files only, each verified by size and
    SHA-256 before it replaces the mirror copy, nothing ever deleted from the mirror. ``src`` defaults
    to the SPX module's data root (``module_spx_gamma/data``, or ``SPX_GAMMA_DATA_ROOT``). Returns None
    when the tree does not exist or is empty.

    Two extras (avo, Q5): a ``.json`` file that is no longer valid JSON never replaces a mirror copy that
    is (it is reported in ``rejected``; a repaired source replaces it on the next run), and every version
    of ``brier.json`` and ``spx_gamma_gex_history.json``, the two files rewritten in place, also gets a dated
    copy in ``<backup dir>/spx_gamma_versions/`` that is never replaced or pruned.
    """
    if src is None:
        from RevenueChaser.module_spx_gamma import config as spx_config
        src = spx_config.DATA_ROOT
    out = out_dir or default_backup_dir()
    return _mirror_into(out, lambda m: _mirror_tree(Path(src), out / SPX_MIRROR_DIRNAME, settle_s, "spx_gamma",
                                                    json_guard=True, versions_dir=out / SPX_VERSIONS_DIRNAME,
                                                    versioned=SPX_VERSIONED_FILES, manifest=m))


class OffMachineResult(NamedTuple):
    """What one ``offmachine_copy`` run did: files written, files already there, damaged SPX files held back, and
    the day the big kinds are due again when this run did not send them (#349)."""
    copied: int
    present: int
    rejected: tuple[str, ...]
    dest: Path
    big_due: date | None = None


def _verify_copy(src: Path, copy: Path) -> None:
    """The second-folder copy of ``src`` must equal it: per-table row count and content checksum for a DuckDB
    snapshot (as the local verify does), size + SHA-256 and a clean CRC check for a zip."""
    if src.suffix == ".duckdb":
        if _table_fingerprints(copy) != _table_fingerprints(src):
            raise _VerificationError(f"{src.name}: the off-machine copy does not match the snapshot")
        return
    import zipfile
    if copy.stat().st_size != src.stat().st_size or _sha256(copy) != _sha256(src):
        raise _VerificationError(f"{src.name}: the off-machine copy does not match (size/SHA-256)")
    try:
        with zipfile.ZipFile(copy) as zf:
            bad = zf.testzip()
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:     # N4: a corrupt deflate stream is zlib.error
        raise _VerificationError(f"{src.name}: the off-machine copy is not a readable zip: {exc}") from exc
    if bad is not None:
        raise _VerificationError(f"{src.name}: the off-machine copy is corrupt at {bad}")


def _copy_verified(src: Path, dest: Path) -> bool:
    """Copy ``src`` to ``dest`` under a temporary name, verify it there, rename it; False = ``dest`` was already there."""
    if dest.exists():
        return False
    dest.parent.mkdir(exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        shutil.copy2(src, tmp)
        _verify_copy(src, tmp)
        os.replace(tmp, dest)
    except BackupError:
        tmp.unlink(missing_ok=True)
        raise
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"could not copy {src.name} to {dest.parent}: {exc}") from exc
    return True


def _mirror_folder(out: Path, mirror: str | Path) -> Path:
    """The folder the off-machine copy writes into: ``<RAW_STORE_BACKUP_MIRROR>/OFFMACHINE_SUBDIR``, created if missing.

    ``mirror`` must not be the backup folder (or inside / around it) and must be reachable. Only its own last
    level and the sub-folder are created, so a disk that is not mounted is an error and never a new folder on this
    one. Everything the copy writes, prunes or cleans up is inside the sub-folder: ``mirror`` itself may be an
    existing folder with other people's files in it.
    """
    folder = Path(mirror).expanduser().resolve()
    here = out.resolve()
    if folder == here or here in folder.parents or folder in here.parents:
        raise BackupError(f"RAW_STORE_BACKUP_MIRROR ({folder}) must be a different folder than the backup "
                          f"folder ({here}), not inside it and not around it")
    try:
        folder.mkdir(exist_ok=True)
        dest = folder / OFFMACHINE_SUBDIR
        dest.mkdir(exist_ok=True)
    except OSError as exc:
        raise BackupError(f"off-machine folder {folder} is not reachable (or {OFFMACHINE_SUBDIR} in it is not a "
                          f"folder): {exc}") from exc
    return dest


def _copy_day(copy: Path) -> date:
    """The UTC day a second-folder copy was made: the stamp in its name, else its modified time."""
    m = _MIRROR_STAMP_RE.search(copy.name)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    return datetime.fromtimestamp(copy.stat().st_mtime, timezone.utc).date()


def _big_due(dest: Path, pattern: str, every_days: int, today: date) -> date | None:
    """The day the next copy of this big kind is due, or None when it goes this run (no copy there yet, or the
    newest one is at least ``every_days`` calendar days old; 0 = every run). Days, not hours: the 04:00 task a
    week after an evening run still sends."""
    if every_days <= 0:
        return None
    days = [_copy_day(p) for p in dest.glob(pattern) if p.is_file()]
    if not days:
        return None
    due = max(days) + timedelta(days=every_days)
    return due if due > today else None


def offmachine_copy(out_dir: Path, mirror: str | Path, keep: int = KEEP_MIRROR, keep_big: int = KEEP_MIRROR_BIG,
                    big_every_days: int = MIRROR_BIG_EVERY_DAYS, big_now: bool = False) -> OffMachineResult:
    """Copy the newest backup set into ``<mirror>/OFFMACHINE_SUBDIR`` and prune it (#275, #349).

    Per kind the newest local file is copied unless the folder already has it. The small kinds
    (``discord_*``, ``history_*`` snapshots) go every run and the newest ``keep`` stay. The big kinds (``raw_*``,
    ``edgar_*`` snapshots, ``edgar_docs_*.zip``) go only when the newest copy of that kind there is at least
    ``big_every_days`` days old (or there is none, or ``big_now``) and the newest ``keep_big`` stay. The local
    ``weekly/`` copies stay on this machine; ``weekly/`` copies an older version sent are left alone. The
    ``discord_media_mirror``, ``spx_gamma_mirror`` and ``spx_gamma_versions`` folders are mirrored
    incrementally with the rules of the local mirrors (the SPX JSON guard included). The ``prices_*`` snapshot
    goes last, once every ``MIRROR_PRICES_EVERY_DAYS`` (or ``big_now``), and the newest ``KEEP_MIRROR_PRICES`` stay
    (#156). Pruning follows the
    verified copy, never precedes it. Raises ``BackupError`` on the first problem.
    """
    out = Path(out_dir)
    dest = _mirror_folder(out, mirror)
    for folder in (dest, dest / WEEKLY_DIRNAME):          # half-copies of a run that was killed
        for stale in folder.glob("*.tmp"):
            if stale.is_file():
                stale.unlink(missing_ok=True)
    today = _utcnow().date()
    copied = present = 0
    big_due: list[date] = []
    # #309: what is written and kept there is recorded in the LOCAL backup folder, so its size is known without a walk.
    om = _Manifest.load(out / OFFMACHINE_MANIFEST_NAME, dest, same_root=True)
    om.lost = None                                        # only the local scan reports a lost manifest

    def send(pattern: str, every_days: int | None) -> None:
        nonlocal copied, present
        if every_days is not None and not big_now:
            due = _big_due(dest, pattern, every_days, today)
            if due is not None:
                big_due.append(due)
                return
        for src in sorted(p for p in out.glob(pattern) if ".partial" not in p.name)[-1:]:
            if _copy_verified(src, dest / src.name):
                copied += 1
                om.record(dest / src.name, src.stat().st_size, None)     # verified equal to src
            else:
                present += 1
                om.note(dest / src.name, src.stat().st_size)

    try:
        for pattern in MIRROR_BIG_KINDS + MIRROR_SMALL_KINDS:
            send(pattern, big_every_days if pattern in MIRROR_BIG_KINDS else None)
        for kind in ("raw", "edgar"):
            _prune(dest, kind, keep_big, om)
        _prune_archives(dest, "edgar_docs", keep_big, om)
        for kind in ("discord", "history"):
            _prune(dest, kind, keep, om)
        rejected: list[str] = []
        for name, guard in ((MEDIA_MIRROR_DIRNAME, False), (SPX_MIRROR_DIRNAME, True), (SPX_VERSIONS_DIRNAME, False)):
            res = _mirror_tree(out / name, dest / name, 0, name, json_guard=guard, manifest=om)
            if res is not None:
                copied += res.copied
                present += res.unchanged
                rejected += [f"{name}/{r}" for r in res.rejected]
        send(MIRROR_PRICES_KIND, MIRROR_PRICES_EVERY_DAYS)    # last: see MIRROR_PRICES_KIND
        _prune(dest, "prices", KEEP_MIRROR_PRICES, om)
    finally:                                              # what did reach the folder before a failure stays recorded
        _save_best_effort(om, "off-machine manifest", "the size card keeps the last recorded off-machine size")
    return OffMachineResult(copied, present, tuple(rejected), dest, min(big_due) if big_due else None)


def _verify_archive(archive: Path, expected_docs: int) -> None:
    """Every member's CRC checks out and no ``.html.gz`` document is missing.

    ``expected_docs`` is counted before zipping; a sync adding files meanwhile
    can only make the archive hold MORE, so fewer is a real loss.
    """
    import zipfile
    try:
        with zipfile.ZipFile(archive) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise BackupError(f"edgar_docs archive corrupt at {bad}")
            n = sum(1 for i in zf.infolist() if i.filename.endswith(".html.gz"))
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as exc:
        raise BackupError(f"edgar_docs archive unreadable: {exc}") from exc
    if n < expected_docs:
        raise BackupError(f"edgar_docs archive holds {n} documents, expected {expected_docs}")


# ── #309 L1: the periodic integrity scan of the LOCAL backup folder ─────────────────────────────────────────────

class ScanResult(NamedTuple):
    """One ``integrity_scan`` call. ``ran`` False = not due (``next_due`` says when). ``failures`` = ``(relative
    path, why)``; ``adopted`` = files the manifest did not have yet, self-checked and hashed now."""
    ran: bool
    checked: int = 0
    adopted: int = 0
    failures: tuple[tuple[str, str], ...] = ()
    bytes: int = 0
    next_due: date | None = None


_SCAN_SKIP_SUFFIXES = (".tmp", ".part", ".partial.zip", ".probe")
_BACKUP_META = (MANIFEST_NAME, OFFMACHINE_MANIFEST_NAME, SCAN_MARKER_NAME)


def _read_json(path: Path) -> dict | None:
    """A small JSON object the job wrote, or None when it is missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise BackupError(f"could not write {path}: {exc}") from exc


def _backup_files(out: Path):
    """Every backup file under ``out`` (a local walk): no temp file, no half-written copy, not the job's own JSON."""
    for root, dirs, files in os.walk(out):
        dirs[:] = [d for d in dirs if d != _MEDIA_TMP_DIRNAME]
        for name in files:
            if name.endswith(_SCAN_SKIP_SUFFIXES) or (name in _BACKUP_META + (SIZES_NAME,) and Path(root) == out):
                continue
            yield Path(root) / name


def _self_check(p: Path) -> None:
    """A file the manifest does not have yet is checked by its own format before it is hashed: every table of a
    DuckDB file is read, every member CRC of a zip. Other files have no check of their own."""
    if p.suffix == ".duckdb":
        _table_fingerprints(p)
    elif p.suffix == ".zip":
        import zipfile
        try:
            with zipfile.ZipFile(p) as zf:
                bad = zf.testzip()
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            raise _VerificationError(f"not a readable zip: {exc}") from exc
        if bad is not None:
            raise _VerificationError(f"corrupt at {bad}")


def _scan_due(marker: dict | None, today: date, every_days: int) -> date | None:
    """None when the scan is due now, else the day it is due. A scan that found a mismatch is due again the next
    run (the row stays red until one passes); 0 days = every run."""
    if not marker or every_days <= 0 or not marker.get("ok"):
        return None
    try:
        last = date.fromisoformat(str(marker.get("last_day")))
    except ValueError:
        return None
    due = last + timedelta(days=every_days)
    return due if due > today else None


def integrity_scan(out_dir: Path | None = None, every_days: int = SCAN_EVERY_DAYS, now: bool = False) -> ScanResult:
    """Re-read the local backup folder against the copy-time manifest when it is due (#309 L1).

    Every file the manifest lists is hashed again (SHA-256) and its size compared; a file the manifest does not
    have yet (made before #309, or its record was lost) is self-checked (``_self_check``) and then recorded, so the
    next scan compares it too. A mismatch, a missing file or a failed self-check is a failure. Read-only for the
    backups: nothing is deleted, moved or rewritten; the scan writes only the manifest (files it adopted, a missing
    file it reported) and its marker ``SCAN_MARKER_NAME``. The off-machine folder is never read (cost, 2026-10-16).
    """
    out = Path(out_dir or default_backup_dir())
    today = _utcnow().date()
    marker_path = out / SCAN_MARKER_NAME
    m = _local_manifest(out)
    # A lost manifest is due tonight (delta review DR-4): otherwise its files would go up to every_days nights
    # without a baseline, at exit 0.
    due = None if now or m.lost else _scan_due(_read_json(marker_path), today, every_days)
    if due is not None:
        return ScanResult(False, next_due=due)
    failures: list[tuple[str, str]] = []
    checked = adopted = nbytes = 0
    for key in sorted(m.files):
        entry, p = m.files[key], out / key
        try:
            size = p.stat().st_size
        except FileNotFoundError:
            failures.append((key, "missing"))
            del m.files[key]                            # reported once; a file removed by hand is not red forever
            continue
        checked += 1
        nbytes += size
        if size != entry.get("size"):
            failures.append((key, f"size {size} bytes, {entry.get('size')} when it was copied"))
            continue
        try:
            if entry.get("sha256") is None:
                _self_check(p)
                m.record(p, size, _sha256(p))
                adopted += 1
            elif _sha256(p) != entry["sha256"]:
                failures.append((key, "SHA-256 differs from the hash taken when it was copied"))
        except (BackupError, OSError) as exc:
            failures.append((key, str(exc)))
    for p in _backup_files(out):
        key = m.key(p)
        if key in m.files or any(key == f for f, _ in failures):
            continue
        checked += 1
        try:
            size = p.stat().st_size
            nbytes += size
            _self_check(p)
            m.record(p, size, _sha256(p))
            adopted += 1
        except (BackupError, OSError) as exc:
            failures.append((key, str(exc)))
    if m.lost:                                          # reported once, red for one night, then cleared
        failures.append((MANIFEST_NAME, f"the manifest was {m.lost}; {adopted} file(s) were re-adopted with a "
                                        f"self-check only, so a file without one (media, SPX JSON) is not compared"))
        m.lost = None
    m.save()
    next_due = today + timedelta(days=every_days if every_days > 0 and not failures else 1)
    _write_json(marker_path, {"last_day": today.isoformat(), "at": _utcnow().isoformat(timespec="seconds"),
                              "ok": not failures, "checked": checked, "failed": len(failures), "adopted": adopted,
                              "bytes": nbytes, "every_days": every_days, "next_due": next_due.isoformat()})
    return ScanResult(True, checked, adopted, tuple(failures), nbytes, next_due)


# ── #309 part 3: the size of both backup folders, recorded by the job ───────────────────────────────────────────

SIZES_NAME = ".backup_sizes.json"


class BackupSizes(NamedTuple):
    """``local``: a ``stat`` walk of the local backup folder (seconds on the PC). ``offmachine``: the sum the
    off-machine manifest recorded, with ``offmachine_state`` "ok"; "not set" (no folder) or "unknown" (no copy
    recorded yet) leave it None. The off-machine folder itself is never listed or stat-ed here."""
    local: int
    offmachine: int | None
    offmachine_state: str = "ok"
    local_files: int = 0
    duckdb_bytes: int = 0
    duckdb_files: int = 0
    docs_zip_bytes: int = 0
    docs_zip_files: int = 0
    offmachine_files: int | None = None


def backup_sizes(out_dir: Path | None = None, mirror: str | Path | None = None) -> BackupSizes:
    out = Path(out_dir or default_backup_dir())
    local = files = db_b = db_n = zip_b = zip_n = 0
    for root, _dirs, names in os.walk(out):
        for name in names:
            if Path(root) == out and name == SIZES_NAME:
                continue
            try:
                size = os.stat(os.path.join(root, name)).st_size
            except OSError:
                continue                                  # removed while the walk ran
            local += size
            files += 1
            if name.endswith(".duckdb"):
                db_b, db_n = db_b + size, db_n + 1
            elif name.startswith("edgar_docs_") and name.endswith(".zip") and ".partial" not in name:
                zip_b, zip_n = zip_b + size, zip_n + 1
    if not str(mirror or "").strip():
        off, state, off_n = None, "not set", None
    elif not (out / OFFMACHINE_MANIFEST_NAME).is_file():
        off, state, off_n = None, "unknown", None
    else:
        om = _Manifest.load(out / OFFMACHINE_MANIFEST_NAME, out)
        off, state, off_n = om.total_bytes(), "ok", len(om.files)
    return BackupSizes(local, off, state, files, db_b, db_n, zip_b, zip_n, off_n)


def record_sizes(out_dir: Path | None = None, mirror: str | Path | None = None) -> tuple[BackupSizes, str]:
    """Measure, write ``SIZES_NAME`` (what the Settings board shows) and return the sizes with their time stamp."""
    out = Path(out_dir or default_backup_dir())
    sizes = backup_sizes(out, mirror)
    at = _utcnow().isoformat(timespec="seconds")
    if out.is_dir():
        _write_json(out / SIZES_NAME, {"recorded_at": at, **sizes._asdict()})
    return sizes, at


def backup_facts(out_dir: Path | None = None) -> dict:
    """What the Settings board shows about the backups: the job's own records only (``SIZES_NAME`` and the scan
    marker), so the page reads two small local files and never the off-machine folder. A missing or unreadable
    record is None ("unknown" on the page)."""
    out = Path(out_dir or default_backup_dir())
    return {"folder": str(out), "sizes": _read_json(out / SIZES_NAME), "scan": _read_json(out / SCAN_MARKER_NAME)}


def _describe(exc: Exception) -> str:
    """``why`` for a log line: a ``BackupError`` already says it; any other exception also names its type."""
    return str(exc) if isinstance(exc, BackupError) else f"{type(exc).__name__}: {exc}"


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1 (0 would prune every copy)")
    return value


def _days(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 (every run) or more days")
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Back up .store/raw.duckdb, edgar.duckdb, discord.duckdb, history.duckdb and prices.duckdb to verified "
                    "snapshots, the edgar_docs/ tree to a verified zip and the discord_media/ and "
                    "SPX Gamma data trees to incremental, verified mirrors.")
    ap.add_argument("--out", default=None, help="backup directory (default: platform dir / RAW_STORE_BACKUP_DIR)")
    ap.add_argument("--keep", type=int, default=5, help="retain this many newest raw.duckdb snapshots (default 5)")
    ap.add_argument("--keep-edgar", type=int, default=2, help="retain this many newest edgar.duckdb snapshots (default 2)")
    ap.add_argument("--keep-docs", type=int, default=2, help="retain this many newest edgar_docs/ archives (default 2)")
    ap.add_argument("--keep-discord", type=int, default=2, help="retain this many newest discord.duckdb snapshots (default 2)")
    ap.add_argument("--keep-history", type=int, default=5, help="retain this many newest history.duckdb snapshots (default 5)")
    ap.add_argument("--keep-prices", type=_positive_int, default=2,
                    help="retain this many newest prices.duckdb snapshots (default 2; no weekly copy)")
    ap.add_argument("--keep-mirror", type=_positive_int, default=KEEP_MIRROR,
                    help=f"retain this many newest discord/history copies in RAW_STORE_BACKUP_MIRROR "
                         f"(default {KEEP_MIRROR})")
    ap.add_argument("--keep-mirror-big", type=_positive_int, default=KEEP_MIRROR_BIG,
                    help=f"retain this many newest raw/edgar/edgar_docs copies there (default {KEEP_MIRROR_BIG}; "
                         f"prices keeps {KEEP_MIRROR_PRICES})")
    ap.add_argument("--mirror-big-every-days", type=_days, default=MIRROR_BIG_EVERY_DAYS,
                    help=f"send the raw/edgar/edgar_docs copies there only when the newest one there is at least this "
                         f"many days old (default {MIRROR_BIG_EVERY_DAYS}; 0 = every run; prices goes every "
                         f"{MIRROR_PRICES_EVERY_DAYS} days). A Drive folder keeps every pruned copy in its trash for "
                         f"30 days, and the trash counts toward the quota (#349)")
    ap.add_argument("--mirror-big-now", action="store_true",
                    help="send the raw/edgar/edgar_docs/prices copies there this run, whatever the age of the newest one")
    ap.add_argument("--scan-every-days", type=_days, default=SCAN_EVERY_DAYS,
                    help=f"re-read the local backup folder against the copy-time manifest every this many days "
                         f"(default {SCAN_EVERY_DAYS}; 0 = every run). Read-only; a mismatch makes the run exit 1 (#309)")
    ap.add_argument("--scan-now", action="store_true", help="run the integrity scan this run, whatever its marker says")
    ap.add_argument("--no-weekly", action="store_true",
                    help="do not keep the first raw snapshot of each ISO week in weekly/ (history keeps its own) "
                         "(RAW_STORE_BACKUP_WEEKLY=0 does the same)")
    args = ap.parse_args(argv)

    out = Path(args.out) if args.out else None
    failed = 0                  # steps that raised: the run is red and tonight's off-machine copy is skipped
    held_back = False           # a damaged SPX file was kept out of a mirror: red, but the rest is verified

    def step(label: str, run):
        """Run one backup step. A failure is reported and counted; the steps after it still run, so one failure
        (a busy edgar store, a full disk for the weekly copy) never costs the rest of the night's backup.

        A ``BackupError`` prints its own message. Anything else a step raises (an ``OSError`` from a prune that meets
        a file another process holds open, a ``duckdb.Error``) prints its type and its stack, so the log shows it.
        """
        nonlocal failed
        try:
            return run()
        except Exception as exc:
            print(f"{label} FAILED: {_describe(exc)}", file=sys.stderr)
            if not isinstance(exc, BackupError):
                traceback.print_exc()
            failed += 1
            return None

    dest = step("backup", lambda: snapshot(out_dir=out, keep=args.keep))
    if dest is not None:
        print(f"backup OK: {dest}")
        weekly = step("weekly backup", lambda: keep_weekly(dest, out_dir=out,
                                                           enabled=False if args.no_weekly else None))
        if weekly is not None:
            print(f"weekly backup OK: {weekly}")

    for label, run in (("edgar backup", lambda: snapshot_edgar(out_dir=out, keep=args.keep_edgar)),
                       ("edgar_docs backup", lambda: archive_edgar_docs(out_dir=out, keep=args.keep_docs)),
                       ("discord backup", lambda: snapshot_discord(out_dir=out, keep=args.keep_discord))):
        snap = step(label, run)
        if isinstance(snap, DocsArchive):   # #336: an unchanged tree keeps the newest archive
            print(f"backup OK: {snap.path}" + (" (unchanged, kept)" if snap.reused else ""))
        elif snap is not None:
            print(f"backup OK: {snap}")

    hist = step("history backup", lambda: snapshot_history(out_dir=out, keep=args.keep_history))
    if hist is not None:
        print(f"backup OK: {hist}")
        weekly_hist = step("history weekly backup", lambda: keep_weekly(
            hist, out_dir=out, enabled=True))   # not the raw stopgap's switch (#279)
        if weekly_hist is not None:
            print(f"weekly backup OK: {weekly_hist}")

    prices = step("prices backup", lambda: snapshot_prices(out_dir=out, keep=args.keep_prices))
    if prices is not None:                  # no weekly copy: the price tables are append-only (#156)
        print(f"backup OK: {prices}")

    media = step("discord_media backup", lambda: mirror_discord_media(out_dir=out))
    if media is not None:
        print(f"discord_media mirror OK: {media.copied} copied, {media.unchanged} unchanged, "
              f"{media.skipped} in flight, {media.total} files -> {media.dest}")

    spx = step("spx backup", lambda: mirror_spx_gamma(out_dir=out))
    if spx is not None:
        print(f"spx mirror OK: {spx.copied} copied, {spx.unchanged} unchanged, "
              f"{spx.skipped} in flight, {spx.total} files -> {spx.dest}")
        if spx.rejected:
            # Everything else is backed up and verified, so the run goes on; the exit code turns the row red.
            print(f"spx mirror WARNING: kept the previous copy of {len(spx.rejected)} file(s) whose source "
                  f"is not valid JSON: {', '.join(spx.rejected)}", file=sys.stderr)
            held_back = True

    if str(config.RAW_STORE_BACKUP_MIRROR).strip():
        # The ISO stamp is what the Automations row parses.
        stamp = _utcnow().isoformat(timespec="seconds")
        if failed:
            # No copy tonight, and the log must say so: without a line the row keeps showing the last good copy
            # as green until it is OFFMACHINE_STALE_HOURS old (review L3).
            print(f"off-machine copy FAILED {stamp}: skipped, {failed} backup step(s) failed earlier tonight",
                  file=sys.stderr)
        else:
            # Everything above is verified, so this copies a good set.
            try:
                moved = offmachine_copy(out or default_backup_dir(), config.RAW_STORE_BACKUP_MIRROR.strip(),
                                        keep=args.keep_mirror, keep_big=args.keep_mirror_big,
                                        big_every_days=args.mirror_big_every_days, big_now=args.mirror_big_now)
            except Exception as exc:                      # the row must read "FAILED", whatever went wrong
                print(f"off-machine copy FAILED {stamp}: {_describe(exc)}", file=sys.stderr)
                if not isinstance(exc, BackupError):
                    traceback.print_exc()
                failed += 1
                moved = None
            if moved is not None:
                skipped = f", big files skipped until {moved.big_due.isoformat()}" if moved.big_due else ""
                print(f"off-machine copy OK {stamp}: {moved.copied} copied, {moved.present} already there{skipped} -> "
                      f"{moved.dest}")
                if moved.rejected:
                    print(f"off-machine copy WARNING: kept the previous copy of {len(moved.rejected)} file(s) whose "
                          f"source is not valid JSON: {', '.join(moved.rejected)}", file=sys.stderr)
                    held_back = True

    # #309 L1: last, so tonight's files are in the manifest and a damaged old file never holds back tonight's copy.
    scan = step("integrity scan", lambda: integrity_scan(out, every_days=args.scan_every_days, now=args.scan_now))
    if scan is not None:
        if not scan.ran:
            print(f"integrity scan not due: next on {scan.next_due.isoformat()}")
        elif scan.failures:
            for rel, why in scan.failures:
                print(f"integrity scan FAILED: {rel}: {why}", file=sys.stderr)
            held_back = True
        else:
            print(f"integrity scan OK: {scan.checked} files, {scan.bytes / 1e9:.2f} GB match the manifest "
                  f"({scan.adopted} added to it); next on {scan.next_due.isoformat()}")

    # #309 part 3: the sizes the Settings board and the Automations row show; never a walk of the off-machine folder.
    sized = step("backup sizes", lambda: record_sizes(out, config.RAW_STORE_BACKUP_MIRROR))
    if sized is not None:
        sizes, at = sized
        # The off-machine part only when the size is known: no folder set, or no copy recorded yet, prints none
        # (.backup_sizes.json says which, for the Settings card).
        off = f", off-machine {sizes.offmachine} bytes" if sizes.offmachine is not None else ""
        print(f"backup sizes {at}: local {sizes.local} bytes{off}")
    return 1 if failed or held_back else 0

if __name__ == "__main__":
    sys.exit(main())
