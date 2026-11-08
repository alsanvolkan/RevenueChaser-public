"""``python -m RevenueChaser.rcdata backup``: ops.sqlite and every serving file, verified, 5 kept; the D3 raw copy."""
from __future__ import annotations

import hashlib
import json
import os
import time

import pytest

from pathlib import Path

from RevenueChaser.core import config
from RevenueChaser.rcdata import backup, ops, serving


def _serve(rows: int = 3) -> None:
    with serving.build("market") as b:
        b.con.execute("CREATE TABLE t AS SELECT range AS i FROM range(?)", [rows])
        b.swap({"t": rows})


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    d = tmp_path / "backups"
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_DIR", str(d))
    return d


def test_an_empty_backup_dir_exits_2_and_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_DIR", "")
    _serve()
    before = sorted(p for p in tmp_path.rglob("*"))
    assert backup.main(["backup"]) == 2
    assert sorted(p for p in tmp_path.rglob("*")) == before


def test_a_verified_copy_of_each_file_with_its_manifest_entry(backup_dir):
    _serve(7)
    ops.enqueue("market_series", None, by="test")
    summary = backup.run_backup()
    out = backup_dir / "market"
    copies = sorted(p.name for p in out.iterdir() if not p.name.startswith("."))
    assert [c.split("_")[0] for c in copies] == ["market", "ops"]
    assert not list(out.glob("*.tmp"))
    manifest = json.loads((out / backup.MANIFEST_NAME).read_text())
    for name in copies:
        entry = manifest["files"][name]
        assert entry["sha256"] == hashlib.sha256((out / name).read_bytes()).hexdigest()
    market = next(c for c in copies if c.startswith("market_"))
    assert manifest["files"][market]["tables"]["t"][0] == 7
    assert summary["market"] == out / market


def test_a_copy_that_does_not_match_its_source_is_deleted(backup_dir, monkeypatch):
    _serve()
    real = backup._fingerprints

    def wrong(path):
        got = real(path)
        return {k: (v[0] + 1, v[1]) for k, v in got.items()} if path.name.endswith(".tmp") else got
    monkeypatch.setattr(backup, "_fingerprints", wrong)
    with pytest.raises(backup.BackupFailed):
        backup.run_backup()
    assert not [p for p in (backup_dir / "market").iterdir() if p.name.startswith("market_")]


def test_retention_keeps_the_newest_five_per_kind(backup_dir):
    _serve()
    ops.ensure()
    for _ in range(7):
        backup.run_backup()
    out = backup_dir / "market"
    markets = sorted(p.name for p in out.glob("market_*.duckdb"))
    assert len(markets) == backup.KEEP == 5 and len(list(out.glob("ops_*.sqlite"))) == 5
    manifest = json.loads((out / backup.MANIFEST_NAME).read_text())
    assert sorted(n for n in manifest["files"] if n.startswith("market_")) == markets


def _raw(rel: str, days_old: float = 0) -> None:
    p = config.MARKET_RAW_DIR / "raw" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * 10)
    t = time.time() - days_old * 86400
    os.utime(p, (t, t))


def test_the_raw_copy_takes_the_newest_35_days_into_the_copy_dir(backup_dir, tmp_path, monkeypatch):
    target = tmp_path / "offbox"
    monkeypatch.setattr(config, "MARKET_RAW_COPY_DIR", target)
    _raw("uw/spot_expiry_strike/2026-10-26/SPX.json.gz", 1)
    _raw("cboe/index/2026-09-01/VIX_History.csv", 50)
    summary = backup.run_backup()
    assert (target / "uw/spot_expiry_strike/2026-10-26/SPX.json.gz").exists()
    assert not (target / "cboe").exists()
    assert summary["raw_copy"] == {"files": 1, "bytes": 10, "copied": 1}
    assert backup.run_backup()["raw_copy"]["copied"] == 0          # rsync-like: unchanged files are not copied again


def test_the_raw_copy_skips_above_20_gb(backup_dir, tmp_path, monkeypatch, caplog):
    target = tmp_path / "offbox"
    monkeypatch.setattr(config, "MARKET_RAW_COPY_DIR", target)
    _raw("uw/spot_expiry_strike/2026-10-26/SPX.json.gz", 1)
    monkeypatch.setattr(backup, "_size", lambda p: backup.RAW_COPY_LIMIT_BYTES)
    summary = backup.run_backup()
    assert summary["raw_copy"]["copied"] == 0 and not target.exists()
    assert "over the" in caplog.text


def test_no_copy_dir_skips_the_raw_copy_with_a_warning(backup_dir, caplog):
    _raw("uw/spot_expiry_strike/2026-10-26/SPX.json.gz", 1)
    assert backup.run_backup()["raw_copy"] is None
    assert "RC_MARKET_RAW_COPY_DIR" in caplog.text


def test_s3_a_write_landing_during_the_ops_backup_does_not_fail_it(sqlite_ops, backup_dir, monkeypatch):
    """The counts and the online backup are one read transaction: a write landing between them is in neither."""
    ops.enqueue("market_series", None, by="test")
    real = backup._online_backup

    def write_then_copy(source, target):
        ops.enqueue("market_series", None, by="a writer racing the backup")
        real(source, target)
    monkeypatch.setattr(backup, "_online_backup", write_then_copy)
    summary = backup.run_backup()
    assert summary["ops"] is not None
    import sqlite3
    con = sqlite3.connect(summary["ops"])
    assert con.execute("SELECT count(*) FROM build_requests").fetchone()[0] == 1
    con.close()


def test_the_ops_copy_is_built_off_the_backup_folder(sqlite_ops, backup_dir, monkeypatch):
    """SQLite writes a database page by page, and the backup folder is slow storage: only the finished file is
    copied there (5 Nov 2026: 8m44s for a 5.8 MB ops.sqlite, and a 30-minute timeout on the 04:30 run)."""
    ops.enqueue("market_series", None, by="test")
    real = backup._online_backup
    targets = []

    def watch(source, target):
        targets.append(Path(target.execute("PRAGMA database_list").fetchone()[2]))
        real(source, target)
    monkeypatch.setattr(backup, "_online_backup", watch)
    copy = backup.run_backup(only="ops")["ops"]
    assert targets and backup_dir not in targets[0].parents
    assert copy.exists() and not list((backup_dir / "market").glob("*.tmp*"))


def test_an_ops_copy_whose_rows_do_not_match_is_failed_and_leaves_nothing(sqlite_ops, backup_dir, monkeypatch):
    """The check reads the copy on the backup disk: a short copy is a failed step, with no ``.tmp`` and no
    manifest entry left behind to look like a backup."""
    ops.enqueue("market_series", None, by="test")
    real = backup._sqlite_counts
    monkeypatch.setattr(backup, "_sqlite_counts", lambda p: {**real(p), "build_requests": 0})
    with pytest.raises(backup.BackupFailed, match="ops"):
        backup.run_backup(only="ops")
    market = backup_dir / "market"
    assert not list(market.glob("*.tmp*")) and not list(market.glob("ops_*.sqlite"))
    manifest = market / "manifest.json"
    assert not manifest.exists() or "ops_" not in manifest.read_text(encoding="utf-8")


def test_a_killed_runs_temp_files_and_their_wal_and_shm_are_swept(sqlite_ops, backup_dir):
    """A run killed mid-copy leaves ``*.tmp`` and, from SQLite, ``*.tmp-wal`` / ``*.tmp-shm``: the next run removes
    them and nothing that is a kept backup."""
    market = backup_dir / "market"
    market.mkdir(parents=True)
    orphans = [market / n for n in ("ops_1.sqlite.tmp", "ops_1.sqlite.tmp-wal", "ops_1.sqlite.tmp-shm",
                                    "market_1.duckdb.tmp")]
    for p in orphans:
        p.write_bytes(b"x")
    ops.enqueue("market_series", None, by="test")
    kept = backup.run_backup(only="ops")["ops"]          # a kept backup and its manifest entry, before the orphans
    for p in orphans:
        p.write_bytes(b"x")
    copy = backup.run_backup(only="ops")["ops"]
    assert not any(p.exists() for p in orphans)
    assert copy.exists() and kept.exists() and kept != copy
    assert kept.name in (market / backup.MANIFEST_NAME).read_text(encoding="utf-8")


def test_s3_a_failed_ops_backup_does_not_block_the_market_copy(backup_dir, monkeypatch):
    _serve(4)
    ops.ensure()

    def broken(out, manifest):
        raise backup.BackupFailed("ops: simulated")
    monkeypatch.setattr(backup, "_backup_ops", broken)
    assert backup.main(["backup"]) == 1
    assert len(list((backup_dir / "market").glob("market_*.duckdb"))) == 1


# ── On Redis (master plan Order 3): a consistent snapshot, kept as an ops.sqlite ───────────────────────────────

_on_redis = pytest.mark.skipif(not os.getenv("RC_REDIS_URL"), reason="needs a Redis at RC_REDIS_URL (CI runs one)")


def _fill() -> None:
    ops.enqueue("market_series", {"x": 1}, by="test")
    ops.sync_issues("prices/serving", ["price_gap_no_action"], [("price_gap_no_action", "SPY", "price missing")])
    ops.watch_add("stock", "AAPL", by="avo", note="pinned")
    ops.count_calls("uw", 3)


@_on_redis
def test_on_redis_the_ops_backup_is_an_ops_sqlite_holding_every_row(backup_dir):
    _fill()
    summary = backup.run_backup()
    assert summary["ops"].name.startswith("ops_") and summary["ops"].suffix == ".sqlite"
    from RevenueChaser.rcdata import ops_copy
    con = ops_copy._open_read_only(summary["ops"])
    try:
        for table in ops_copy.COPIED:
            assert ops_copy._table_rows(con, table) == ops.rows(table), table
        assert con.execute("SELECT max(version) FROM ops_schema").fetchone()[0] == ops.OPS_SCHEMA_VERSION
    finally:
        con.close()
    manifest = json.loads((backup_dir / "market" / backup.MANIFEST_NAME).read_text())
    assert manifest["files"][summary["ops"].name]["tables"]["build_issues"] == 1


@_on_redis
def test_on_redis_the_backup_restores_with_the_one_time_copy(backup_dir, monkeypatch, tmp_path):
    _fill()
    taken = backup.run_backup()["ops"]
    want = {t: ops.rows(t) for t in ops.TABLES if t != "ops_schema"}
    monkeypatch.setattr(config, "MARKET_DIR", tmp_path / "restored")          # an empty namespace
    from RevenueChaser.rcdata import ops_copy
    assert ops_copy.main(["--sqlite", str(taken)]) == 0
    assert {t: ops.rows(t) for t in want} == want


# ── The light/heavy split (avo, 5 Nov 2026): --only picks the steps, --subdir the folder ───────────────────────


def test_only_an_unknown_step_exits_2_and_writes_nothing(backup_dir):
    _serve()
    assert backup.main(["backup", "--only", "ops,market,gamma"]) == 2
    assert not backup_dir.exists()


def test_an_empty_or_nested_subdir_exits_2_and_writes_nothing(backup_dir):
    _serve()
    # "C:" is refused too (review N1): on Windows ``Path(folder) / "C:"`` resolves outside the backup folder.
    for bad in ("", " ", "market/heavy", "..", "../market", "a\\b", "C:"):
        assert backup.main(["backup", "--subdir", bad]) == 2, bad
    assert not backup_dir.exists()


def test_only_runs_just_the_named_steps(backup_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MARKET_RAW_COPY_DIR", tmp_path / "offbox")
    _raw("uw/spot_expiry_strike/2026-10-26/SPX.json.gz", 1)
    _serve(5)
    ops.ensure()
    summary = backup.run_backup(only=["market"])
    assert set(summary) == {"market"} and summary["market"] is not None
    out = backup_dir / "market"
    assert not list(out.glob("ops_*.sqlite")) and not (tmp_path / "offbox").exists()


def test_the_raw_copy_is_a_step_of_its_own(backup_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MARKET_RAW_COPY_DIR", tmp_path / "offbox")
    _raw("uw/spot_expiry_strike/2026-10-26/SPX.json.gz", 1)
    _serve()
    summary = backup.run_backup(only=["raw"])
    assert set(summary) == {"raw_copy"} and summary["raw_copy"]["copied"] == 1
    assert not (backup_dir / "market").exists()


def test_a_subdir_keeps_its_own_manifest_and_its_own_retention(backup_dir):
    _serve(2)
    ops.ensure()
    for _ in range(backup.KEEP + 2):
        backup.run_backup(only=["market"], subdir="market-heavy")
    backup.run_backup(only=["market"], subdir=backup.SUBDIR)
    light, heavy = backup_dir / "market", backup_dir / "market-heavy"
    assert len(list(light.glob("market_*.duckdb"))) == 1
    assert len(list(heavy.glob("market_*.duckdb"))) == backup.KEEP
    heavy_manifest = json.loads((heavy / backup.MANIFEST_NAME).read_text())
    light_manifest = json.loads((light / backup.MANIFEST_NAME).read_text())
    assert len(heavy_manifest["files"]) == backup.KEEP and len(light_manifest["files"]) == 1
    for name, entry in heavy_manifest["files"].items():      # every copy carries its sha256
        assert entry["sha256"] == hashlib.sha256((heavy / name).read_bytes()).hexdigest()


def test_the_cli_passes_only_and_subdir_through(backup_dir):
    _serve(3)
    ops.ensure()
    assert backup.main(["backup", "--only", "market", "--subdir", "market-heavy"]) == 0
    assert len(list((backup_dir / "market-heavy").glob("market_*.duckdb"))) == 1
    assert not (backup_dir / "market").exists()


def test_the_redis_ops_copy_is_written_in_one_transaction(backup_dir, monkeypatch):
    # #387: an autocommit insert per row made 42k commits, 9-11 min a night and past the 30 min unit limit.
    from RevenueChaser.rcdata import ops_redis
    ops.enqueue("market_series", {"x": 1}, by="test")
    ops.enqueue("market_series", {"x": 2}, by="test")
    taken = {t: ops.rows(t) for t in ops.TABLES if t != "ops_schema"}
    assert len(taken["build_requests"]) == 2
    monkeypatch.setattr(ops_redis, "snapshot", lambda: taken)
    real, seen = ops.connect, []

    def traced(path=None):
        cm = real(path)
        con = cm.__enter__()
        con.set_trace_callback(seen.append)
        return _Exit(cm, con)

    monkeypatch.setattr(ops, "connect", traced)
    backup_dir.mkdir()
    dest = backup._backup_ops_redis(backup_dir, backup._Manifest(backup_dir))
    inserts = [i for i, s in enumerate(seen) if s.startswith("INSERT INTO build_requests")]
    marks = [(i, s) for i, s in enumerate(seen) if s.startswith(("BEGIN", "COMMIT"))]
    assert len(inserts) == 2
    before = [s for i, s in marks if i < inserts[0]]
    assert before and before[-1].startswith("BEGIN")             # on main the last mark is ensure's own COMMIT
    assert not [i for i, _ in marks if inserts[0] < i < inserts[-1]]
    assert [i for i, s in marks if i > inserts[-1] and s == "COMMIT"]
    from RevenueChaser.rcdata import ops_copy
    con = ops_copy._open_read_only(dest)
    try:
        assert ops_copy._table_rows(con, "build_requests") == taken["build_requests"]
    finally:
        con.close()


class _Exit:
    def __init__(self, cm, con):
        self.cm, self.con = cm, con

    def __enter__(self):
        return self.con

    def __exit__(self, *exc):
        return self.cm.__exit__(*exc)
