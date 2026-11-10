"""``python -m RevenueChaser.rcdata restore`` and ``restore-check``: a weekly backup copy back into use.

    rcdata restore --step duckdb --latest market --into /tmp/rc-restore     # verify and copy into a folder
    rcdata restore --step boards --file raw_20261108_023000_000000.duckdb --live
    rcdata restore --step ops --latest --into /tmp/rc-restore
    rcdata restore --step uw-raw --latest --into /mnt/data/scratch          # the whole week folder
    rcdata restore --step edgar-docs --latest --into /tmp/rc-restore        # the zip; unzip it by hand
    rcdata restore-check                                                    # the monthly drill (RC-Restore-Check)

Every restore first verifies the copy against its step's ``.manifest.json`` (sha256 and size; for a DuckDB file also
the table fingerprints the backup recorded; for ``ops`` the row counts of the kept tables): a copy the manifest does
not know, or one that no longer matches, is refused and nothing is written.

``--into <dir>`` writes the verified copy there, under its backup name, and touches nothing live. ``--live`` puts a
DuckDB copy in place of the file it came from: it refuses while another RevenueChaser process uses that store
(``rawstore._peer_pids``) and while a ``<name>.duckdb.wal`` lies beside the target (#214: DuckDB would replay it on
top of the restored file), moves the current file to ``<name>.duckdb.pre-restore``, copies, and fingerprints the
result again. ``ops --live`` is not automated: on Redis the copy goes in with ``rcdata.ops_copy --sqlite <copy>``
into an EMPTY namespace (emptying one is a delete and needs avo's word); on ``ops.sqlite`` the file is copied into
place with the dashboard stopped. ``uw-raw --live`` and ``edgar-docs --live`` are copies by hand too.

``restore-check`` (first Sunday of the month, 02:30) restores the newest ``ops`` copy and the newest board copy
into a scratch folder, verifies both the way a restore does, and removes what it wrote. Exit 1 on any mismatch.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from . import backup, serving

PRE_RESTORE = ".pre-restore"


class RestoreRefused(RuntimeError):
    """Nothing was written: the copy is unknown, does not verify, or the live target is in use."""


# ── what there is ───────────────────────────────────────────────────────────────────────────────────────────────

def _step_dir(step: str) -> tuple[Path, dict]:
    if step not in backup.STEPS:
        raise RestoreRefused(f"unknown step {step!r}; known: {', '.join(backup.STEPS)}")
    out = backup.step_dir(step)
    manifest = backup._Manifest(out)
    return out, manifest.data


def copies(step: str, kind: str | None = None) -> list[Path]:
    """The copies of a step the manifest knows, oldest first; for ``duckdb``/``boards`` those of one ``kind``."""
    out, data = _step_dir(step)
    if step == backup.STEP_UW_RAW:
        return [out / w for w in data.get("weeks") or [] if (out / w).is_dir()]
    names = sorted(n for n, e in data["files"].items() if isinstance(e, dict) and "/" not in n)
    if kind is not None:
        names = [n for n in names if n.startswith(f"{kind}_")]
    return [out / n for n in names if (out / n).is_file()]


def latest(step: str, kind: str | None = None) -> Path:
    found = copies(step, kind)
    if not found:
        what = f"{kind}_* " if kind else ""
        raise RestoreRefused(f"no {what}copy of step {step} in {backup.step_dir(step)}")
    return found[-1]


# ── verify ──────────────────────────────────────────────────────────────────────────────────────────────────────

def verify(step: str, path: Path) -> dict:
    """Check ``path`` against the step's manifest; the manifest entry on success, ``RestoreRefused`` otherwise."""
    out, data = _step_dir(step)
    if step == backup.STEP_UW_RAW:
        return _verify_week(out, data, path)
    entry = data["files"].get(path.name)
    if not isinstance(entry, dict):
        raise RestoreRefused(f"{path.name} is not in {out / backup.MANIFEST_NAME}: not a copy this job verified")
    if not path.is_file():
        raise RestoreRefused(f"{path} is missing")
    if path.stat().st_size != entry.get("bytes") or backup._sha256(path) != entry.get("sha256"):
        raise RestoreRefused(f"{path.name} does not match its manifest entry (size or sha256): do not restore it")
    tables = entry.get("tables")
    if path.suffix == ".duckdb" and isinstance(tables, dict):
        got = {t: list(v) for t, v in backup._fingerprints(path).items()}
        if got != tables:
            raise RestoreRefused(f"{path.name}: table fingerprints differ from the manifest: {got} != {tables}")
    elif path.suffix == ".sqlite" and isinstance(tables, dict):
        got = backup._kept_counts(backup._sqlite_counts(path))
        if got != tables:
            raise RestoreRefused(f"{path.name}: row counts differ from the manifest: {got} != {tables}")
    return entry


def _verify_week(out: Path, data: dict, week: Path) -> dict:
    if week.name not in (data.get("weeks") or []) or not week.is_dir():
        raise RestoreRefused(f"{week.name} is not a week folder {out / backup.MANIFEST_NAME} knows")
    entries = {k: e for k, e in data["files"].items() if k.startswith(week.name + "/")}
    bad = []
    for key, e in entries.items():
        f = out / key
        if not f.is_file() or f.stat().st_size != e.get("bytes"):
            bad.append(key)
        elif "sha256" in e and backup._sha256(f) != e["sha256"]:
            bad.append(key)
    if bad:
        raise RestoreRefused(f"{week.name}: {len(bad)} file(s) differ from the manifest, first {bad[:3]}")
    return {"files": len(entries), "bytes": sum(int(e.get("bytes") or 0) for e in entries.values())}


# ── restore ─────────────────────────────────────────────────────────────────────────────────────────────────────

def restore_into(step: str, src: Path, into: Path) -> Path:
    """Verify ``src`` and copy it (a week folder: the whole tree) into ``into``; the written path."""
    verify(step, src)
    into.mkdir(parents=True, exist_ok=True)
    dest = into / src.name
    if dest.exists():
        raise RestoreRefused(f"{dest} exists already: nothing overwritten")
    if src.is_dir():
        shutil.copytree(src, dest)
    else:
        shutil.copy2(src, dest)
    return dest


def live_target(step: str, src: Path) -> Path:
    """The live file a DuckDB copy stands in for."""
    stem = src.name.rsplit("_", 3)[0]
    if step == backup.STEP_DUCKDB:
        if stem not in serving.KINDS:
            raise RestoreRefused(f"{src.name}: not a copy of a DuckDB kind ({', '.join(serving.KINDS)})")
        return serving.live_path(stem)
    if step == backup.STEP_BOARDS:
        from ..core import rawstore
        legacy = {"raw": Path(rawstore.DB_PATH), "edgar": rawstore.edgar_db_path(),
                  "history": rawstore.history_db_path(), "prices": rawstore.prices_db_path()}
        return legacy.get(stem) or Path(rawstore.DB_PATH).parent / backup.BOARDS_DIRNAME / f"{stem}.duckdb"
    if step == backup.STEP_OPS:
        raise RestoreRefused("ops --live is a step by hand: on Redis `python -m RevenueChaser.rcdata.ops_copy "
                             "--sqlite <copy>` into an EMPTY namespace (emptying one is a delete: avo's word first); "
                             "on ops.sqlite copy the file into place with the dashboard stopped")
    raise RestoreRefused(f"{step} --live is a copy by hand: restore it --into a folder and move the files yourself")


def restore_live(step: str, src: Path) -> Path:
    """Put a verified DuckDB copy in place of its live file; the previous file is kept as ``.pre-restore``."""
    from ..core import rawstore
    target = live_target(step, src)
    entry = verify(step, src)
    peers = rawstore._peer_pids(target)
    if peers:
        raise RestoreRefused(f"{target} is in use by RevenueChaser process(es) {peers}; stop them first")
    wal = target.with_name(target.name + ".wal")
    if wal.exists():
        raise RestoreRefused(f"leftover write-ahead log {wal}: DuckDB would replay it on top of the restored file; "
                             "open the store once so it checkpoints (or move the .wal aside), then restore again")
    aside = target.with_name(target.name + PRE_RESTORE)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        os.replace(target, aside)
    try:
        shutil.copy2(src, target)
        got = {t: list(v) for t, v in backup._fingerprints(target).items()}
    except Exception:
        if aside.exists():
            os.replace(aside, target)
        raise
    want = entry.get("tables")
    if isinstance(want, dict) and got != want:
        os.replace(aside, target) if aside.exists() else target.unlink(missing_ok=True)
        raise RestoreRefused(f"the restored {target.name} does not match the copy; the previous file is back")
    return target


# ── the monthly drill ───────────────────────────────────────────────────────────────────────────────────────────

def restore_check(scratch: Path | None = None) -> dict:
    """Restore the newest ``ops`` and the newest board copy into a scratch folder, verify, clean up.

    ``{step: {"file", "bytes", "tables"}}``; raises ``RestoreRefused`` on a mismatch or when a step has no copy."""
    own = scratch is None
    scratch = Path(tempfile.mkdtemp(prefix="rc-restore-check-")) if own else Path(scratch)
    result: dict = {}
    written: list[Path] = []
    try:
        for step, kind in ((backup.STEP_OPS, None), (backup.STEP_BOARDS, _first_board())):
            src = latest(step, kind)
            dest = restore_into(step, src, scratch)
            written.append(dest)
            entry = verify(step, src)
            if step == backup.STEP_OPS:
                con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
                try:
                    ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                finally:
                    con.close()
                if not ok:
                    raise RestoreRefused(f"{dest.name}: integrity_check failed after the restore")
                tables = backup._kept_counts(backup._sqlite_counts(dest))
            else:
                tables = {t: list(v) for t, v in backup._fingerprints(dest).items()}
            if tables != entry.get("tables"):
                raise RestoreRefused(f"{dest.name}: the restored file differs from the manifest")
            result[step] = {"file": src.name, "bytes": dest.stat().st_size, "tables": len(tables)}
    finally:
        for p in written:                                   # only what this run wrote
            p.unlink(missing_ok=True)
        if own:
            shutil.rmtree(scratch, ignore_errors=True)
    return result


def _first_board() -> str | None:
    """The board copy the drill restores: ``raw`` while the old store exists, else the first board by name."""
    _, data = _step_dir(backup.STEP_BOARDS)
    stems = sorted({n.rsplit("_", 3)[0] for n in data["files"] if n.endswith(".duckdb")})
    if not stems:
        return None
    return "raw" if "raw" in stems else stems[0]


# ── the commands ────────────────────────────────────────────────────────────────────────────────────────────────

def add_parsers(sub) -> None:
    r = sub.add_parser("restore", help="verify a weekly backup copy and put it into a folder, or in place (--live)")
    r.add_argument("--step", required=True, choices=backup.STEPS, help="which step's copy")
    pick = r.add_mutually_exclusive_group(required=True)
    pick.add_argument("--file", help="the copy's name as the backup folder lists it (a week folder for uw-raw)")
    pick.add_argument("--latest", nargs="?", const="", metavar="KIND",
                      help="the newest copy; for duckdb and boards name the kind or board file (e.g. market, raw)")
    where = r.add_mutually_exclusive_group(required=True)
    where.add_argument("--into", type=Path, help="write the verified copy into this folder (nothing live is touched)")
    where.add_argument("--live", action="store_true",
                       help="put a DuckDB copy in place of its live file (refuses peers and a leftover .wal)")
    c = sub.add_parser("restore-check", help="the monthly drill: restore the newest ops and board copies into a "
                                             "scratch folder, verify, remove them; exit 1 on a mismatch")
    c.add_argument("--scratch", type=Path, default=None, help="the folder to restore into (default: a temp folder)")


def run(args) -> int:
    try:
        if args.cmd == "restore-check":
            result = restore_check(args.scratch)
            for step, r in result.items():
                print(f"{step}: {r['file']} restored and verified ({r['bytes']} bytes, {r['tables']} tables)",
                      flush=True)
            return 0
        if args.latest is not None:
            kind = args.latest or None
            if args.step in (backup.STEP_DUCKDB, backup.STEP_BOARDS) and kind is None:
                print(f"refused: --latest needs the kind for step {args.step} (e.g. --latest market)", flush=True)
                return 2
            src = latest(args.step, kind)
        else:
            src = Path(args.file)
            if not src.exists():
                src = backup.step_dir(args.step) / args.file
        if args.live:
            target = restore_live(args.step, src)
            print(f"restored {src.name} -> {target} (previous file: {target.name}{PRE_RESTORE})", flush=True)
        else:
            dest = restore_into(args.step, src, args.into)
            print(f"verified {src.name} and wrote {dest}", flush=True)
        return 0
    except RestoreRefused as exc:
        print(f"refused: {exc}", flush=True)
        return 2
    except (backup.BackupFailed, backup.BackupRefused) as exc:
        print(f"failed: {exc}", flush=True)
        return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m RevenueChaser.rcdata")
    sub = p.add_subparsers(dest="cmd", required=True)
    add_parsers(sub)
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
