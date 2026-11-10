"""The SPX Gamma board's own file, ``boards/spx_gamma.duckdb`` (SPX Gamma review 9 Nov 2026, D1-D5; DECISIONS S9, S16).

The one module that writes the file. Five tables (``core/rawstore.py`` MANIFEST, ``"db": "spx_gamma"``):

- ``spx_raw_days`` / ``spx_raw_files``: a stored UW day, its manifest and every answer byte-exact (zlib, sha256
  kept). Written together in one transaction, so a day is either whole or absent. UW serves no history, so these
  rows cannot be fetched again.
- ``spx_brier``: the scenario-probability records; a scored record is never changed (SPX-4).
- ``spx_gex_daily``: the daily GEX record, every day kept (D2: no 10-day prune), written by a Fetch only (F2).
- ``spx_meta``: ``latest``, the manifest of the newest day the board shows.

Parsed snapshots, charts and reports are regenerable and stay files under the cache folder (D4).
"""
from __future__ import annotations

import hashlib
import json
import time

from RevenueChaser.core import rawstore

DB = "spx_gamma"
RAW_DAYS = "spx_raw_days"
RAW_FILES = "spx_raw_files"
BRIER = "spx_brier"
GEX = "spx_gex_daily"
META = "spx_meta"

GEX_FIELDS = ("is_friday_close", "spot", "net_dex", "net_gex_oi", "net_vanna", "live_charm", "vanna_at_spot",
              "charm_live_sum", "matrix_gex", "source_chip", "regime", "vix")
BRIER_FIELDS = ("scenario_probs", "outcome", "brier", "rule", "target", "bands", "resolved_on", "resolved_close")


def _iso(v) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _query(sql: str, params: list | None = None) -> list[dict]:
    return rawstore.query(sql, params, db=DB)


# ── raw days ──────────────────────────────────────────────────────────────────

def stored_days() -> list[str]:
    """Every complete stored day, oldest first."""
    return [_iso(r["d"]) for r in _query(f"SELECT session_date AS d FROM {RAW_DAYS} ORDER BY session_date")]


def has_day(day: str) -> bool:
    return bool(_query(f"SELECT 1 FROM {RAW_DAYS} WHERE session_date = ?", [day]))


def latest_day() -> str | None:
    rows = _query(f"SELECT max(session_date) AS d FROM {RAW_DAYS}")
    return _iso(rows[0]["d"]) if rows and rows[0]["d"] is not None else None


def put_raw_day(day: str, manifest: dict, files: dict[str, bytes]) -> None:
    """Store one whole day: its manifest row and every answer file, in one transaction (replaces the staging-folder
    rename). A day already stored is replaced whole; the caller decides when that is allowed (force only)."""
    now = time.time()
    spot = manifest.get("spot")
    if isinstance(spot, dict):              # a Fetch writes {"source", "value"}
        spot = spot.get("value")
    day_row = {
        "session_date": day, "requested_date": manifest.get("requested_date"),
        "api_snapshot_time": manifest.get("api_snapshot_time"), "origin": manifest.get("origin") or "fetch",
        "spot": spot, "zero_dte_expiry": manifest.get("zero_dte_expiry"),
        "file_count": len(files), "manifest": manifest, "stored_at": now,
    }
    file_rows = [{"session_date": day, "file": name, "sha256": hashlib.sha256(body).hexdigest(),
                  "bytes": len(body), "body": body.decode("utf-8"), "stored_at": now}
                 for name, body in sorted(files.items())]

    def _write(tx):
        tx.ensure_table(RAW_FILES)
        tx.execute(f"DELETE FROM {RAW_FILES} WHERE session_date = ?", [day])
        tx.write_rows(RAW_FILES, file_rows)
        tx.write_rows(RAW_DAYS, [day_row])

    rawstore.atomic(_write, db=DB)


def raw_manifest(day: str) -> dict | None:
    rows = _query(f"SELECT manifest FROM {RAW_DAYS} WHERE session_date = ?", [day])
    if not rows:
        return None
    return rawstore._decode_row(RAW_DAYS, rows[0])["manifest"]


def raw_files(day: str) -> dict[str, str]:
    """``{file name: answer text}`` of a stored day, exactly as UW sent it."""
    rows = _query(f"SELECT file, body FROM {RAW_FILES} WHERE session_date = ? ORDER BY file", [day])
    return {r["file"]: rawstore._decode_row(RAW_FILES, r)["body"] for r in rows}


def raw_file_hashes(day: str) -> dict[str, str]:
    return {r["file"]: r["sha256"] for r in _query(
        f"SELECT file, sha256 FROM {RAW_FILES} WHERE session_date = ? ORDER BY file", [day])}


# ── Brier records ─────────────────────────────────────────────────────────────

def brier_records() -> list[dict]:
    """Every record in the old ``brier.json`` shape (``date`` plus the fields), oldest first."""
    out = []
    for r in _query(f"SELECT * FROM {BRIER} ORDER BY asof_date"):
        r = rawstore._decode_row(BRIER, r)
        out.append({"date": _iso(r["asof_date"]), **{k: r.get(k) for k in BRIER_FIELDS}})
    return out


def put_brier(records: list[dict]) -> None:
    """Upsert records by date. A row already scored in the file is never changed (SPX-4), whatever is passed."""
    if not records:
        return
    scored = {_iso(r["d"]) for r in _query(
        f"SELECT asof_date AS d FROM {BRIER} WHERE outcome IS NOT NULL OR brier IS NOT NULL")}
    now = time.time()
    rows = [{"asof_date": r["date"], **{k: r.get(k) for k in BRIER_FIELDS}, "updated_at": now}
            for r in records if r["date"] not in scored]
    rawstore.write_rows(BRIER, rows)


# ── GEX record ────────────────────────────────────────────────────────────────

def gex_days() -> dict[str, dict]:
    """``{session date: record}``, every stored day, oldest first (the old history file's shape)."""
    return {_iso(r["session_date"]): {k: r.get(k) for k in GEX_FIELDS}
            for r in _query(f"SELECT * FROM {GEX} ORDER BY session_date")}


def put_gex(day: str, record: dict) -> None:
    rawstore.write_rows(GEX, [{"session_date": day, **{k: record.get(k) for k in GEX_FIELDS},
                               "updated_at": time.time()}])


# ── meta ──────────────────────────────────────────────────────────────────────

def get_meta(key: str):
    rows = _query(f"SELECT value FROM {META} WHERE key = ?", [key])
    return json.loads(rows[0]["value"]) if rows and rows[0]["value"] is not None else None


def put_meta(key: str, value) -> None:
    rawstore.write_rows(META, [{"key": key, "value": json.dumps(value, ensure_ascii=False, default=str),
                                "updated_at": time.time()}])


def latest() -> dict | None:
    """The manifest of the day the board shows (was ``latest.json``)."""
    return get_meta("latest")
