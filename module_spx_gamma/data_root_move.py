"""S4 (#38): copy the SPX Gamma tree out of the checkout into the store root, verified, without deleting it.

``python -m RevenueChaser.module_spx_gamma --migrate-data-root`` reports what it would copy; ``--apply`` copies.
Run it with the dashboard stopped (it holds the board's files open between Fetches). The copy goes into
``<target>.partial``, every file is checked by size and SHA-256 against its source, and only then is the folder
renamed to ``<target>``: ``config.resolve_data_root`` switches to it on the next process start. The legacy tree is
left in place until avo says to delete it; rollback is removing ``<target>`` (the legacy tree is then read again).
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan(legacy: Path, target: Path) -> dict:
    files = sorted(p for p in legacy.rglob("*") if p.is_file()) if legacy.is_dir() else []
    return {"legacy": str(legacy), "target": str(target), "files": len(files),
            "bytes": sum(p.stat().st_size for p in files), "target_exists": target.exists()}


def migrate(legacy: Path, target: Path, apply: bool = False) -> dict:
    """Copy ``legacy`` to ``target``; a dry run unless ``apply``. Refuses when the target already exists."""
    out = plan(legacy, target)
    if out["target_exists"]:
        return {**out, "status": "target_exists"}
    if not out["files"]:
        return {**out, "status": "nothing_to_copy"}
    if not apply:
        return {**out, "status": "dry_run"}
    partial = target.with_name(target.name + ".partial")
    if partial.exists():
        shutil.rmtree(partial)   # our own leftover from an interrupted run, never a source
    shutil.copytree(legacy, partial, copy_function=shutil.copy2)
    bad = []
    for src in (p for p in legacy.rglob("*") if p.is_file()):
        dst = partial / src.relative_to(legacy)
        if not dst.is_file() or dst.stat().st_size != src.stat().st_size or _sha(dst) != _sha(src):
            bad.append(str(src.relative_to(legacy)))
    if bad:
        return {**out, "status": "verify_failed", "mismatched": bad[:20], "partial": str(partial)}
    target.parent.mkdir(parents=True, exist_ok=True)
    partial.rename(target)
    return {**out, "status": "copied", "verified": out["files"]}


__all__ = ["plan", "migrate"]
