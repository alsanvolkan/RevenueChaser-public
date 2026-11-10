"""Import the old SPX Gamma file tree into ``boards/spx_gamma.duckdb`` (SPX Gamma review 9 Nov 2026, §7).

``python -m RevenueChaser.module_spx_gamma --import-tree [--apply] [--legacy PATH] [--extra-gex FILE ...]``

Dry run by default: reads the tree and prints, per table, what it read, what it would write and what is already
stored. ``--apply`` writes. Idempotent by key: a day, record or GEX row already in the file is left alone, so a second
run writes nothing. It deletes and moves nothing; the tree stays until avo's "sil".

- ``raw/<day>/`` with a ``manifest.json`` becomes a ``spx_raw_days`` row and its ``spx_raw_files`` rows, byte-exact;
  each file's sha256 is checked against the manifest where it lists one. A folder without a manifest is not a day and
  is reported, not imported; so are ``.partial-*`` folders and ``*.corrupt-*`` files.
- ``brier.json``: every record, old rules included; the file's average and n are compared with the recomputed ones.
- the GEX record: ``spx_gamma_gex_history.json``, then each ``--extra-gex`` file (e.g. a ``spx_gamma_versions/``
  copy that still holds a pruned day), then each ``parsed/<day>/snapshot.json`` (D2/F3: every day kept).
- ``latest.json`` becomes ``spx_meta.latest`` when the file has none.
- ``--legacy``: the old in-checkout tree; every file only it has is listed.

After ``--apply`` the parsed files, charts and reports are rebuilt into the cache folder from the stored days (D4).
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from . import brier, store
from .config import BRIER_FILENAME, HISTORY_FILENAME

logger = logging.getLogger(__name__)


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _raw_days(src: Path, apply: bool, out: dict) -> None:
    raw = src / "raw"
    t = out["spx_raw_days"] = {"days_read": 0, "written": 0, "already": 0, "files": 0, "bytes": 0,
                               "sha_mismatch": [], "not_days": [], "partial": []}
    if not raw.is_dir():
        return
    for d in sorted(p for p in raw.iterdir() if p.is_dir()):
        if d.name.startswith("."):
            t["partial"].append(d.name)
            continue
        manifest = _load(d / "manifest.json")
        if not isinstance(manifest, dict):
            t["not_days"].append(d.name)
            continue
        t["days_read"] += 1
        files = {p.name: p.read_bytes() for p in sorted(d.glob("*.json")) if p.name != "manifest.json"}
        listed = manifest.get("sha256") or {}
        for name, body in files.items():
            if name in listed and listed[name] != hashlib.sha256(body).hexdigest():
                t["sha_mismatch"].append(f"{d.name}/{name}")
        t["files"] += len(files)
        t["bytes"] += sum(len(b) for b in files.values())
        if store.has_day(d.name):
            t["already"] += 1
            continue
        t["written"] += 1
        if apply:
            store.put_raw_day(d.name, manifest, files)


def _brier(src: Path, apply: bool, out: dict) -> None:
    data = _load(src / BRIER_FILENAME)
    records = [r for r in (data or {}).get("records", []) if isinstance(r, dict) and r.get("date")]
    stored = {r["date"] for r in store.brier_records()}
    new = [r for r in records if r["date"] not in stored]
    check = {"records": records}
    brier._refresh_average(check)
    out["spx_brier"] = {"read": len(records), "written": len(new), "already": len(records) - len(new),
                        "file_average": (data or {}).get("average"), "file_n": (data or {}).get("n"),
                        "recomputed_average": check["average"], "recomputed_n": check["n"]}
    if apply:
        store.put_brier(new)


def _gex(src: Path, extra: list[Path], apply: bool, out: dict) -> None:
    from .run import gex_record

    found: dict[str, dict] = {}
    sources: dict[str, int] = {}
    for path in [src / HISTORY_FILENAME, *extra]:
        data = _load(path)
        n = 0
        for day, rec in (data or {}).items() if isinstance(data, dict) else ():
            if isinstance(rec, dict) and day not in found:
                found[day] = rec
                n += 1
        sources[str(path)] = n
    n = 0
    for snap_file in sorted((src / "parsed").glob("*/snapshot.json")):
        day = snap_file.parent.name
        snap = _load(snap_file)
        if day not in found and isinstance(snap, dict):
            found[day] = gex_record(snap, day)
            n += 1
    sources["parsed/*/snapshot.json"] = n
    stored = set(store.gex_days())
    new = {d: r for d, r in sorted(found.items()) if d not in stored}
    out["spx_gex_daily"] = {"read": len(found), "written": len(new), "already": len(found) - len(new),
                            "by_source": sources}
    if apply:
        for day, rec in new.items():
            store.put_gex(day, rec)


def _latest(src: Path, apply: bool, out: dict) -> None:
    latest = _load(src / "latest.json")
    has = store.latest() is not None
    out["spx_meta"] = {"latest_read": isinstance(latest, dict), "written": isinstance(latest, dict) and not has,
                       "already": has}
    if apply and isinstance(latest, dict) and not has:
        store.put_meta("latest", latest)


def _legacy_only(src: Path, legacy: Path | None) -> list[str] | None:
    if legacy is None:
        return None
    if not legacy.is_dir():
        return [f"(no legacy tree at {legacy})"]
    return sorted(str(p.relative_to(legacy)) for p in legacy.rglob("*")
                  if p.is_file() and not (src / p.relative_to(legacy)).exists())


def import_tree(src: Path, apply: bool = False, legacy: Path | None = None,
                extra_gex: list[Path] | None = None) -> dict:
    """Read ``src`` (the old tree) into the board file; a dry run unless ``apply``. Returns the per-table report."""
    src = Path(src)
    out: dict = {"status": "applied" if apply else "dry_run", "source": str(src)}
    if not src.is_dir():
        out["status"] = "no_tree"
        return out
    out["corrupt"] = sorted(str(p.relative_to(src)) for p in src.rglob("*.corrupt-*"))
    _raw_days(src, apply, out)
    _brier(src, apply, out)
    _gex(src, [Path(p) for p in (extra_gex or [])], apply, out)
    _latest(src, apply, out)
    out["legacy_only"] = _legacy_only(src, Path(legacy) if legacy else None)
    if apply:
        out["cache_rebuilt"] = rebuild_cache()
    return out


def rebuild_cache() -> list[str]:
    """Rebuild every stored day's parsed snapshot, charts and report into the cache folder, oldest first (no network,
    nothing written to the board file but the latest pointer, which Refresh moves to the newest day)."""
    from .run import refresh

    done = []
    for day in store.stored_days():
        try:
            refresh(day)
            done.append(day)
        except Exception as exc:  # noqa: BLE001 - one bad day must not stop the rest
            logger.warning("spx cache rebuild %s failed: %s", day, exc)
    return done
