"""One-time migration of legacy file stores + SQLite DBs into .store/raw.duckdb.

Follows ``docs/data_architecture.md`` (Migration) and the table schemas in
``core/rawstore.py``'s MANIFEST:

  python -m RevenueChaser.scripts.migrate_rawdata_to_duckdb [--no-delete] [--delete] [--purge] [--reset] [--force] [--dataset {name,...}] [--source-dir DIR]

- ``--no-delete`` (default) — migrate + verify only; never delete originals.
- ``--delete``        — delete originals for PASS datasets only.
- ``--purge``         — FULL cleanup (implies --delete): after a fully verified
                        migration, remove the legacy ``rawData/`` folder entirely
                        (13F zips are salvaged to ``.cache/sec_13f/``), plus the
                        legacy ``output/`` and ``data/`` folders (artifacts are
                        moved into ``.cache/`` first, never overwriting newer
                        files). Refuses to purge if any dataset FAILed, if the
                        source dir IS the durable store, or if any file there is
                        neither migrated nor recognisably regenerable.
- ``--reset``         — drop ``rawData/raw.duckdb`` first, then migrate from scratch.
- ``--force``         — allow ``--reset`` even when post-migration growth is detected,
                        and let ``--purge`` delete unclassified files.
- ``--dataset``       — restrict to datasets (schema-manifest keys).
- ``--source-dir``    — legacy folder to read from (default: ``rawData/`` when it
                        exists, else the durable ``.store/``).

Pipeline per dataset: (a) confirm sources present, (b) migrate + cross-check
(count-based verify; SQLite read in bulk via sqlite_scan so multi-GB DBs work),
(c) delete only PASS datasets (``--delete``), (d) re-run via ``--reset``.
``runs/`` and ``*_form13f.zip`` are never deleted as data — zips are salvaged to
the cache. Already-verified datasets are skipped on re-run (idempotent), so an
interrupted run resumes safely.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from RevenueChaser.core import rawstore

REPO = Path(__file__).resolve().parent.parent
LEGACY_DATA_DIR = REPO / "rawData"
LEGACY_OUTPUT_DIR = REPO / "output"
LEGACY_EMPTY_DATA_DIR = REPO / "data"

MANIFEST = rawstore.MANIFEST
SQLITE_STREAM_CHUNK = 200_000


def default_source_dir() -> Path:
    """Legacy folder to migrate from: rawData/ when present, else the store itself.

    NOTE the fallback IS the durable ``.store/`` — safe to *read* from (a
    re-run after the originals are gone finds nothing and skips), but never
    safe to delete. ``_purge_allowed`` refuses it explicitly.
    """
    if LEGACY_DATA_DIR.exists():
        return LEGACY_DATA_DIR
    return rawstore.DB_PATH.parent


# ── source discovery ──────────────────────────────────────────────────────────
def _json_file(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def discover_sources(rd: Path) -> dict[str, list[Path]]:
    """Map dataset name -> concrete source paths found on disk."""
    out: dict[str, list[Path]] = {}

    def add_file(ds, path: Path):
        if path.is_file():
            out.setdefault(ds, []).append(path)

    # Dir scans are extension-filtered so stray files (e.g. a 0-byte "OTC"
    # next to "OTC.json") never enter a dataset.
    def add_dir(ds, path: Path, pattern: str = "*.json"):
        files = sorted(path.glob(pattern)) if path.is_dir() else []
        if files:
            out.setdefault(ds, []).extend(files)

    add_dir("ticker_payload_cache", rd / "tickers")
    add_dir("edgar_financials", rd / "edgar_financials")
    add_dir("swing_info", rd / "swing_potential" / "info")
    add_dir("swing_ohlc", rd / "swing_potential" / "ohlc", pattern="*.csv")
    add_dir("leaps_cache", rd / "leaps" / "cache")
    add_dir("rotation_holdings", rd / "sector_rotation" / "holdings")
    add_dir("rotation_snapshots", rd / "sector_rotation" / "snapshots")
    add_dir("index_constituents", rd / "indexes")
    add_file("equity_universe", rd / "asymmetric_growth_universe.json")
    add_file("equity_universe_provenance", rd / "asymmetric_growth_universe_meta.json")
    add_file("sec_company_tickers", rd / "sec_company_tickers.json")
    add_file("watchlist", rd / "watchlist_tickers.json")
    add_file("sec13f_security_meta", rd / "universe_meta.json")
    add_file("sec13f_security_meta", rd / "sec_13f" / "universe_meta.json")
    add_file("sec13f_enrichment", rd / "sec_13f" / "enrichment.json")
    add_file("etf_holdings", rd / "sector_rotation" / "etf_holdings.json")
    add_file("cusip_ticker", rd / "sec_13f" / "cusip_ticker.json")
    add_file("sec13f_totals", rd / "sec_13f" / "totals.json")
    add_file("sec13f_ticker_names", rd / "sec_13f" / "ticker_name.json")
    # SQLite DBs
    for ds, db in [("ticker_scores", "ticker_store/tickers.db"),
                   ("sec13f", "sec_13f/sec13f.db"),
                   ("leaps", "leaps/radar.db")]:
        add_file(ds, rd / db)
    return out


# ── per-dataset migration ─────────────────────────────────────────────────────
def _key_col(ds: str) -> str:
    """The single key column of a keyed store ('ticker', 'cusip', ...)."""
    key = MANIFEST[ds].get("key") or []
    return key[0] if key else "ticker"


def _value_col(ds: str) -> str:
    """The single non-key column of a ``{key: blob}`` store.

    Read from the manifest rather than hardcoded: these tables are split
    between ``entry`` and ``payload``, and writing the wrong name stored the
    keys with a NULL blob while the row COUNT still matched — so the migration
    reported PASS and ``--purge`` deleted the originals.
    """
    key_cols = set(MANIFEST[ds].get("key") or [])
    value_cols = [c for c in rawstore._columns_of(MANIFEST[ds]) if c not in key_cols]
    if len(value_cols) != 1:
        raise ValueError(f"{ds}: expected exactly one value column, got {value_cols}")
    return value_cols[0]


def _dedupe_by_key(ds: str, rows: list[dict]) -> list[dict]:
    """Keep the last row per FULL key (all key columns) so expected == stored
    under PK upsert. Composite keys (e.g. rotation_snapshots' as_of_date+ticker)
    keep every distinct row — never collapse on the first column alone."""
    key_cols = MANIFEST[ds].get("key") or []
    if not key_cols:
        return rows
    out: dict = {}
    for r in rows:
        kv = tuple(r.get(c) for c in key_cols)
        if any(k is None or (isinstance(k, str) and not k.strip()) for k in kv):
            continue
        out[kv] = r
    return list(out.values())


def _f(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _rows_for(ds: str, paths: list[Path]) -> list[dict]:
    if ds == "ticker_payload_cache":
        out = []
        for p in paths:
            d = _json_file(p)
            if not isinstance(d, dict):
                continue
            out.append({"ticker": p.stem.replace("_raw", "").upper(), "payload": d,
                        "fetched_at": _f(d.get("_fetched_at")),
                        "fetched_at_iso": d.get("_fetched_at_iso", "")})
        return out
    if ds == "edgar_financials":
        out = []
        for p in paths:
            d = _json_file(p) or {}
            out.append({"ticker": p.stem.upper(), "annual": d.get("annual", []),
                        "quarterly": d.get("quarterly", []), "meta": d.get("meta", {})})
        return out
    if ds == "swing_info":
        return [{"ticker": p.stem.upper(), "payload": _json_file(p), "cached_at": p.stat().st_mtime} for p in paths]
    if ds == "swing_ohlc":
        out = []
        for p in paths:
            try:
                df = pd.read_csv(p, index_col=0, parse_dates=True)
            except Exception:
                continue
            cached_at = p.stat().st_mtime
            for ts, r in df.iterrows():
                # .upper() to match load_ohlc(), which uppercases its argument —
                # a lowercase-named CSV would otherwise migrate to a row no
                # reader can ever find.
                out.append({"ticker": p.stem.upper(),
                            "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                            "open": _f(r.get("Open")), "high": _f(r.get("High")),
                            "low": _f(r.get("Low")), "close": _f(r.get("Close")),
                            "volume": _f(r.get("Volume")), "cached_at": cached_at})
        return out
    if ds == "leaps_cache":
        return [{"ticker": p.stem.upper(), "payload": _json_file(p)} for p in paths]
    if ds == "rotation_holdings":
        out = []
        for p in paths:
            d = _json_file(p) or {}
            out.append({"ticker": p.stem.upper(), "date": d.get("date", ""), "rows": d.get("rows", [])})
        return out
    if ds == "rotation_snapshots":
        out = []
        for p in paths:
            d = _json_file(p) or {}
            asof = d.get("as_of_date", p.stem)
            for e in d.get("etfs", []):
                row = {k: e.get(k) for k in ("ticker", "name", "group", "close_price", "rs_1w",
                                             "rs_1m", "rs_3m", "rs_ratio", "rs_momentum",
                                             "rs_ratio_z", "rs_momentum_z", "quadrant")}
                row["as_of_date"] = asof
                row["benchmark"] = d.get("benchmark", "SPY")
                row["generated_at"] = d.get("generated_at", "")
                out.append(row)
        return out
    if ds == "index_constituents":
        out = []
        for p in paths:
            members = _json_file(p) or []
            if isinstance(members, list):
                out.extend({"slug": p.stem, "ticker": t} for t in members if t)
        return out
    if ds == "sec13f_security_meta":
        # union of the two legacy maps (universe_meta + sec_13f/universe_meta)
        out = []
        for p in paths:
            d = _json_file(p) or {}
            out.extend({"ticker": t, "entry": v} for t, v in d.items() if t)
        return out
    if ds == "equity_universe":
        out = []
        for p in paths:
            d = _json_file(p)
            if not isinstance(d, list):
                continue
            # read_json_auto-shaped rows: {ticker, market_cap, sector, price,
            # name, quote: {69-field STRUCT}}. Stored in the typed table.
            for r in d:
                if not isinstance(r, dict) or not r.get("ticker"):
                    continue
                out.append({
                    "ticker": (r.get("ticker") or "").upper(),
                    "market_cap": _f(r.get("market_cap")),
                    "sector": r.get("sector") or "",
                    "price": _f(r.get("price")),
                    "name": r.get("name") or "",
                    "quote": r.get("quote"),
                })
        return out
    if ds == "equity_universe_provenance":
        out = []
        for p in paths:
            d = _json_file(p) or {}
            if "generated_at" in d:
                out.append({"key": "__meta__",
                            "generated_at": str(d.get("generated_at") or ""),
                            "refreshed_by": str(d.get("refreshed_by") or "")})
        return out
    if ds == "sec_company_tickers":
        d = _json_file(paths[0]) or {}
        return [{"ticker": t, "cik": v.get("cik"), "name": v.get("name")}
                for t, v in d.items() if t]
    if ds == "watchlist":
        d = _json_file(paths[0]) or []
        return [{"ticker": t} for t in d if t]
    if ds in ("sec13f_enrichment",
              "etf_holdings",
              "cusip_ticker", "sec13f_totals", "sec13f_ticker_names"):
        d = _json_file(paths[0]) or {}
        if ds == "etf_holdings":
            d = d.get("holdings", {})
        # etf_holdings also has provider/fetched_at (#303), which a legacy file does not carry
        key_col, value_col = _key_col(ds), "entry" if ds == "etf_holdings" else _value_col(ds)
        return [{key_col: t, value_col: v} for t, v in d.items() if t]
    if ds == "ticker_scores":
        return _rows_from_ticker_db(paths[0])
    raise ValueError(f"no migrator for dataset {ds}")


def _sqlite_table_cols(conn: sqlite3.Connection, table: str) -> list[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def _rows_from_ticker_db(path: Path) -> list[dict]:
    if not path.exists():
        return []
    conn = sqlite3.connect(str(path))
    try:
        cols = _sqlite_table_cols(conn, "tickers")
        if not cols:
            return []
        return [dict(zip(cols, r)) for r in conn.execute(f"SELECT {', '.join(cols)} FROM tickers")]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _copy_ticker_store_meta(path: Path) -> int:
    """Legacy tickers.db ``meta`` table (asof / schema_version) -> store_meta."""
    if not path.exists():
        return 0
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    if rows:
        rawstore.write_rows("store_meta", [{"key": k, "value": v} for k, v in rows])
    return len(rows)


# SQLite DBs that map to MULTIPLE manifest tables. Migrated in bulk via
# DuckDB's native sqlite_scanner (INSERT ... SELECT FROM sqlite_scan) — the
# 16.6M-row sec13f holdings table copies in seconds, no Python row loop.
COMPOSITE_SQLITE = {
    "sec13f": ("sec_13f/sec13f.db", [("submissions", "sec13f_submissions"),
                                     ("holdings", "sec13f_holdings"),
                                     ("cusip_names", "sec13f_cusip_names"),
                                     ("ingested_zips", "sec13f_ingested_zips"),
                                     ("edgar_backfill", "sec13f_edgar_backfill")]),
    "leaps": ("leaps/radar.db", [("flow_prints", "leaps_flow_prints"),
                                 ("daily_scores", "leaps_daily_scores")]),
}


# Columns a target table needs that its legacy source cannot supply, as the SQL that derives
# them during the copy (#208).
#
# ``line_no``: ``sec13f_holdings`` gained the (accession, line_no) PRIMARY KEY in schema v12, so
# the column is NOT NULL, but the legacy ``holdings`` table carries no INFOTABLE_SK. The ORDER BY
# is what makes the synthetic number deterministic, which a full-refresh re-run needs — the same
# source must produce the same numbers, not just the same row count.
#
# ``sshprnamttype``: the v11 ingest filtered to long equity BEFORE storing a row
# (``if row.get("SSHPRNAMTTYPE") != "SH" or row.get("PUTCALL"): continue``), so every row in a
# legacy table is an SH row with no PUTCALL by construction; v11 just had nowhere to record it.
# Writing 'SH' recovers that fact. Left NULL instead, the derive's long-equity filter would match
# nothing and a migrated store would silently produce no totals at all.
#
# ``period`` is deliberately NOT synthesized: no query reads ``h.period`` (every period filter
# joins ``sec13f_submissions``), so filling it would be inventing data no reader wants.
SYNTHETIC_COLUMNS = {
    ("sec13f_holdings", "line_no"):
        'row_number() OVER (PARTITION BY "accession" ORDER BY "cusip", "shares", "value")',
    ("sec13f_holdings", "sshprnamttype"): "'SH'",
}


# Non-composite target tables cleared before import so a re-run never leaves
# rows the source no longer has (full-refresh semantics).
CLEAR_BEFORE_IMPORT = {"ticker_scores"}


def _bulk_copy_sqlite(db_path: Path, tables: list[tuple[str, str]]) -> tuple[int, int]:
    """Bulk-copy SQLite tables into raw.duckdb via sqlite_scan.

    Returns (expected_total, stored_total). Each target table is cleared first —
    migration is a full refresh from source, so re-runs never duplicate rows.
    Columns missing in a legacy SQLite table are inserted as NULL.
    """
    expected = 0
    stored = 0
    with sqlite3.connect(str(db_path)) as src_db:
        src_tables = {r[0] for r in src_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    with rawstore.connect() as conn:
        conn.execute("LOAD sqlite")
        src = str(db_path)
        for table, tds in tables:
            # A legacy db may predate a later-added table (e.g. edgar_backfill):
            # skip it rather than failing the whole migration.
            if table not in src_tables:
                continue
            src_cols = [r[0] for r in conn.execute(
                f"DESCRIBE SELECT * FROM sqlite_scan('{src}', '{table}')").fetchall()]
            cols = rawstore._columns_of(rawstore.MANIFEST[tds])
            if not src_cols:
                continue
            conn.execute(f"DELETE FROM {tds}")
            col_sql = ", ".join(f'"{c}"' for c in cols)
            select_sql = ", ".join(
                f'"{c}"' if c in src_cols else SYNTHETIC_COLUMNS.get((tds, c), "NULL")
                for c in cols)
            expected += conn.execute(
                f"SELECT COUNT(*) FROM sqlite_scan('{src}', '{table}')").fetchone()[0]
            conn.execute(f"INSERT INTO {tds} ({col_sql}) SELECT {select_sql} FROM sqlite_scan('{src}', '{table}')")
            stored += conn.execute(f"SELECT COUNT(*) FROM {tds}").fetchone()[0]
    return expected, stored


def _stored_count(ds: str) -> int:
    if ds == "sec13f":
        return sum(rawstore.count(t) for t in ("sec13f_submissions", "sec13f_holdings",
                                               "sec13f_cusip_names", "sec13f_ingested_zips"))
    if ds == "leaps":
        return sum(rawstore.count(t) for t in ("leaps_flow_prints", "leaps_daily_scores"))
    return rawstore.count(ds)


def migrate_dataset(ds: str, paths: list[Path]) -> dict:
    """Import one dataset and verify stored==expected. Returns a report dict."""
    if ds in COMPOSITE_SQLITE:
        _rel, tables = COMPOSITE_SQLITE[ds]
        expected, stored = _bulk_copy_sqlite(paths[0], tables)
        return {"dataset": ds, "status": "PASS" if stored == expected else "FAIL",
                "expected": expected, "stored": stored,
                "sources": [str(p) for p in paths]}
    rows = _dedupe_by_key(ds, _rows_for(ds, paths))
    if ds in CLEAR_BEFORE_IMPORT:
        # One transaction: clear() then write_rows() was two, so a failed
        # import left the table empty.
        # allow_empty: a legacy file with no rows migrates as an empty table (stored == expected == 0).
        rawstore.replace_all(ds, rows, allow_empty=True)
    elif rows:
        rawstore.write_rows(ds, rows)
    total = len(rows)
    if ds == "ticker_scores":
        # side-effect: legacy meta (asof / schema_version) -> store_meta
        _copy_ticker_store_meta(paths[0])

    stored = _stored_count(ds)
    status = "PASS" if stored == total else "FAIL"
    return {"dataset": ds, "status": status, "expected": total, "stored": stored,
            "sources": [str(p) for p in paths]}


def _delete_sources(res: dict) -> None:
    """Remove source files/dirs for a PASS dataset (never zips, never runs/)."""
    for p in res.get("sources", []):
        path = Path(p)
        if path.suffix == ".zip":
            continue
        if path.is_dir():
            for f in path.glob("*"):
                if f.suffix == ".zip":
                    continue
                f.unlink(missing_ok=True)
            try:
                path.rmdir()
            except OSError:
                pass
        else:
            path.unlink(missing_ok=True)


def _already_verified(ds: str) -> bool:
    row = rawstore.query("SELECT verify FROM migration_manifest WHERE dataset = ?", [ds])
    return bool(row) and row[0]["verify"] == "PASS"


def run(args) -> int:
    if args.reset:
        # Guard: refuse --reset if a completed migration has since grown.
        if not args.force:
            grown = []
            for row in rawstore.query("SELECT dataset FROM migration_manifest"):
                ds = row["dataset"]
                try:
                    now = _stored_count(ds)
                except Exception:
                    continue
                mrow = rawstore.get("migration_manifest", ds)
                if mrow and now > int(mrow.get("source_count") or 0):
                    grown.append(ds)
            if grown:
                print(f"✗ --reset would destroy post-migration data in: {', '.join(grown)} "
                      f"→ use --force to override")
                return 1
        # Build the fresh DB at a TEMP path and atomically rename it over the
        # old one — external readers (editors, backup agents) holding the old
        # file never block the migration, and an interrupted run can never
        # leave a half-written DB in place.
        real_path = rawstore.DB_PATH
        tmp_path = rawstore.DB_PATH.with_name(rawstore.DB_PATH.name + ".new")
        if tmp_path.exists():
            tmp_path.unlink()
        rawstore.DB_PATH = tmp_path
        # Raw file ONLY: edgar_db_path() follows DB_PATH's folder, so it is
        # still the live edgar.duckdb here — resetting it would destroy it.
        rawstore.reset_db(include_edgar=False)
        print(f"✓ building fresh DB at {tmp_path.name} (will replace {real_path.name} on success)")
    else:
        real_path = None

    try:
        rd = Path(args.source_dir).resolve() if args.source_dir else default_source_dir()
        sources = discover_sources(rd)
        # iterate manifest tables PLUS the composite SQLite source datasets
        all_datasets = sorted(set(MANIFEST) | set(COMPOSITE_SQLITE))
        selected = [ds for ds in all_datasets if not args.dataset or ds in args.dataset]
        results = []
        for ds in selected:
            paths = sources.get(ds, [])
            if not paths:
                print(f"  SKIP {ds}: no sources on disk")
                continue
            if not args.dataset and not args.reset and _already_verified(ds):
                print(f"  SKIP {ds}: already migrated (PASS in migration_manifest)")
                continue
            res = migrate_dataset(ds, paths)
            results.append(res)
            print(f"  {res['status']:4s} {ds}: expected={res['expected']} stored={res['stored']}")
            if res["status"] == "PASS":
                total_bytes = sum(p.stat().st_size for p in paths)
                rawstore.write_rows("migration_manifest", [{
                    "dataset": ds, "migrated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "schema_version": rawstore.schema_version(), "source_count": res["expected"],
                    "source_bytes": total_bytes, "source_hash": "", "verify": "PASS",
                }])
                if args.delete:
                    _delete_sources(res)
    except Exception:
        if real_path is not None:
            rawstore.DB_PATH = real_path
            if tmp_path.exists():
                tmp_path.unlink()
                print("✗ aborted — temp DB discarded, existing raw.duckdb untouched")
        raise

    failed = [r for r in results if r["status"] == "FAIL"]
    if failed:
        if real_path is not None:
            rawstore.DB_PATH = real_path
            if tmp_path.exists():
                tmp_path.unlink()
            print("✗ migration FAILED — temp DB discarded, existing raw.duckdb untouched")
        print(f"\n✗ {len(failed)} dataset(s) failed — rerun with --reset to start fresh from originals")
        return 1

    if real_path is not None:
        rawstore.DB_PATH = real_path
        import os
        os.replace(tmp_path, real_path)
        print(f"✓ replaced {real_path} atomically")
    print(f"\n✓ migrated {len(results)} dataset(s) from {rd}")

    if args.purge:
        args.rd = rd
        ok, reason = _purge_allowed(args, results, sources, selected)
        if not ok:
            print(f"\n✗ purge SKIPPED — {reason} (legacy folders left in place)")
            return 1
        zips = _salvage_zips(rd)
        regenerable, _unknown = classify_for_purge(rd, sources)
        artifacts = _salvage_output() + _salvage_stray_artifacts(rd, regenerable)
        shutil.rmtree(rd, ignore_errors=True)
        shutil.rmtree(LEGACY_OUTPUT_DIR, ignore_errors=True)
        shutil.rmtree(LEGACY_EMPTY_DATA_DIR, ignore_errors=True)
        print(f"✓ purged legacy folders (rawData/, output/, data/) — "
              f"{zips} zip(s) salvaged to .cache/sec_13f/, {artifacts} artifact(s) moved to .cache/")
    return 0


def _purge_allowed(args, results: list[dict], sources: dict, selected: list[str]) -> tuple[bool, str]:
    """Purge only after a FULLY verified migration — never lose a source the
    store doesn't already contain."""
    if args.dataset:
        return False, "--purge requires a full run (no --dataset filter)"
    # --purge rmtree's the source dir. Once rawData/ is gone, default_source_dir()
    # falls back to .store/ — which holds raw.duckdb itself.
    rd = args.rd
    if rd.resolve() == rawstore.DB_PATH.parent.resolve():
        return False, (f"refusing to purge {rd} — that is the durable store holding "
                       f"{rawstore.DB_PATH.name}; pass --source-dir to name the legacy folder")
    if not rd.exists():
        return False, f"source dir {rd} does not exist"
    if any(r["status"] == "FAIL" for r in results):
        return False, "a dataset FAILed; originals kept"
    source_datasets = [ds for ds in selected if sources.get(ds)]
    migrated = {r["dataset"] for r in results if r["status"] == "PASS"}
    unverified = [ds for ds in source_datasets
                  if ds not in migrated and not _already_verified(ds)]
    if unverified:
        return False, ("dataset(s) with sources on disk were not migrated/verified: "
                       f"{', '.join(sorted(unverified))}")
    if not migrated and not source_datasets:
        return False, "nothing was migrated (no sources on disk)"
    _regen, unknown = classify_for_purge(rd, sources)
    if unknown and not args.force:
        listed = ", ".join(str(p.relative_to(rd)) for p in unknown[:8])
        more = f" (+{len(unknown) - 8} more)" if len(unknown) > 8 else ""
        return False, (f"{len(unknown)} file(s) under {rd} are neither migrated nor "
                       f"recognised as regenerable: {listed}{more} → migrate them, or "
                       f"pass --force to delete them anyway")
    return True, ""


# ── purge safety: classify every file before deleting the tree ──────────────
# --purge rmtree's the whole source dir, so anything neither migrated nor
# recognised as regenerable would vanish unexamined. That is how
# sec_13f/ticker_name.json (durable OpenFIGI input, read by run_sec13f and
# written by nothing) was nearly lost. Anything unclassified now BLOCKS the
# purge unless --force is passed.
_REGENERABLE_GLOBS = (
    "*_latest.json", "*_latest.html", "*_latest.csv",   # rendered board artifacts
    "*.db-shm", "*.db-wal",                             # SQLite sidecars, replayed at migration
    "*_form13f.zip",                                    # salvaged to .cache/ separately
    "aggregate.json", "meta.json",                      # legacy 13F scratch, no reader
)


def classify_for_purge(rd: Path, sources: dict) -> tuple[list[Path], list[Path]]:
    """Split the files under ``rd`` into (regenerable, unclassified).

    Migrated sources are excluded from both. Empty files are treated as strays
    (the dir scans are extension-filtered, so a 0-byte "OTC" beside "OTC.json"
    is deliberate noise).
    """
    migrated = {p.resolve() for paths in sources.values() for p in paths}
    regenerable, unknown = [], []
    for f in sorted(rd.rglob("*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        if f.resolve() in migrated:
            continue
        if any(f.match(g) for g in _REGENERABLE_GLOBS) or f.stat().st_size == 0:
            regenerable.append(f)
        else:
            unknown.append(f)
    return regenerable, unknown


def _salvage_stray_artifacts(rd: Path, files: list[Path]) -> int:
    """Move regenerable board artifacts out of the legacy tree into .cache/,
    preserving their relative path and never overwriting a newer file."""
    moved = 0
    for f in files:
        if f.suffix not in (".json", ".html", ".csv") or "_latest" not in f.name:
            continue
        target = _cache_root() / f.relative_to(rd)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(target))
        moved += 1
    return moved


def _cache_root() -> Path:
    """The .cache root (looked up lazily so tests can redirect it)."""
    from RevenueChaser.core.config import OUTPUT_DIR
    return OUTPUT_DIR


def _salvage_zips(rd: Path) -> int:
    """Move any downloaded *_form13f.zip into .cache/sec_13f/ (source material
    is kept as regenerable files, and the ingest glob points there)."""
    moved = 0
    zips_dir = rd / "sec_13f"
    if not zips_dir.is_dir():
        return 0
    dest = _cache_root() / "sec_13f"
    for z in sorted(zips_dir.glob("*_form13f.zip")):
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / z.name
        if not target.exists():
            shutil.move(str(z), str(target))
            moved += 1
    return moved


def _salvage_output() -> int:
    """Move legacy output/ artifacts into .cache/ preserving relative paths,
    never overwriting a newer .cache file."""
    moved = 0
    if not LEGACY_OUTPUT_DIR.is_dir():
        return 0
    for f in sorted(LEGACY_OUTPUT_DIR.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(LEGACY_OUTPUT_DIR)
        target = _cache_root() / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(target))
        moved += 1
    return moved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Migrate legacy rawData files + SQLite DBs into .store/raw.duckdb")
    ap.add_argument("--no-delete", action="store_true", help="migrate + verify only (default)")
    ap.add_argument("--delete", action="store_true", help="delete originals for PASS datasets only")
    ap.add_argument("--purge", action="store_true",
                    help="FULL cleanup (implies --delete): after a fully verified migration, "
                         "remove rawData/ (zips salvaged to .cache/sec_13f/) and legacy output/, data/")
    ap.add_argument("--reset", action="store_true", help="drop raw.duckdb first, then migrate")
    ap.add_argument("--force", action="store_true", help="allow --reset despite post-migration growth")
    ap.add_argument("--dataset", nargs="*", default=None, help="restrict to specific datasets")
    ap.add_argument("--source-dir", default=None, help="legacy folder to read from (default: rawData/ if present)")
    args = ap.parse_args(argv)
    if args.purge:
        args.delete = True  # purge includes deleting migrated originals
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
