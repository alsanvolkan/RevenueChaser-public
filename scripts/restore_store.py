"""Restore a store file from a verified snapshot written by ``backup_store``.

    python -m RevenueChaser.scripts.restore_store --list
    python -m RevenueChaser.scripts.restore_store raw_20260929_040000_123456.duckdb
    python -m RevenueChaser.scripts.restore_store --latest raw      # or: edgar, history, prices

- Refuses while any RevenueChaser process (dashboard, scheduled job) is using
  the store — stop them first.
- Checks the snapshot opens and fingerprints it (row count + content checksum
  per table) before touching anything.
- Moves the current file aside as ``<name>.duckdb.pre-restore`` (overwritten on
  the next restore) and copies the snapshot in; if the copy fails the original
  is put back.
- ``edgar_docs/`` archives are restored by hand: unzip the chosen
  ``edgar_docs_<stamp>.zip`` over ``edgar_docs/`` (see docs/data_architecture.md).
- The SPX Gamma tree (#276) is restored by copying ``spx_gamma_mirror/`` back over ``module_spx_gamma/data``
  (or ``SPX_GAMMA_DATA_ROOT``); an earlier version of ``brier.json`` or ``spx_gamma_gex_history.json`` is in
  ``spx_gamma_versions/`` next to it. A weekly raw copy (#277) is restored with
  ``--from <backup dir>/weekly --latest raw`` (the ``history_*`` copies earlier runs left there, until #342, read
  the same way); the off-machine copy (#275) is read the same way with
  ``--from <RAW_STORE_BACKUP_MIRROR folder>/RevenueChaser-backup``.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from RevenueChaser.core import rawstore
from RevenueChaser.scripts.backup_store import (
    BackupError, _table_fingerprints, default_backup_dir)

_TARGETS = {"raw": lambda: Path(rawstore.DB_PATH), "edgar": rawstore.edgar_db_path,
            "history": rawstore.history_db_path, "prices": rawstore.prices_db_path}


def snapshots(out: Path, kind: str | None = None) -> list[Path]:
    kinds = [kind] if kind else list(_TARGETS)
    return sorted(p for k in kinds for p in out.glob(f"{k}_*.duckdb"))


def _kind_of(snapshot: Path) -> str:
    kind = snapshot.name.split("_", 1)[0]
    if kind not in _TARGETS:
        raise BackupError(f"not a store snapshot: {snapshot.name} (expected raw_*, edgar_*, history_* or prices_*)")
    return kind


def restore(snapshot: Path) -> Path:
    """Put ``snapshot`` in place of its store file; return the target path."""
    kind = _kind_of(snapshot)
    target = _TARGETS[kind]()
    peers = rawstore._peer_pids(target)
    if peers:
        raise BackupError(f"store in use by RevenueChaser process(es) {peers}; stop them first")
    tables = _table_fingerprints(snapshot)  # raises if the snapshot does not open
    aside = target.with_name(target.name + ".pre-restore")
    if target.exists():
        os.replace(target, aside)
    try:
        shutil.copy2(snapshot, target)
    except OSError:
        if aside.exists():
            os.replace(aside, target)
        raise
    if _table_fingerprints(target) != tables:
        raise BackupError(f"restored file does not match the snapshot; previous file kept at {aside}")
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Restore .store/*.duckdb from a verified snapshot.")
    ap.add_argument("snapshot", nargs="?", help="snapshot file name or path")
    ap.add_argument("--list", action="store_true", help="list available snapshots")
    ap.add_argument("--latest", choices=sorted(_TARGETS), help="restore the newest snapshot of this store")
    ap.add_argument("--from", dest="src", default=None, help="backup directory (default: backup_store's)")
    args = ap.parse_args(argv)
    out = Path(args.src) if args.src else default_backup_dir()

    if args.list or not (args.snapshot or args.latest):
        for p in snapshots(out):
            print(f"{p.name}  {p.stat().st_size / 1e6:8.1f} MB")
        return 0
    if args.latest:
        found = snapshots(out, args.latest)
        if not found:
            print(f"no {args.latest}_* snapshots in {out}", file=sys.stderr)
            return 1
        snap = found[-1]
    else:
        snap = Path(args.snapshot)
        if not snap.is_file():
            snap = out / args.snapshot
    try:
        target = restore(snap)
    except BackupError as exc:
        print(f"restore FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"restored {snap.name} -> {target} (previous file: {target.name}.pre-restore)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
