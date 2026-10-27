"""``python -m RevenueChaser.rcdata backup``: ops.sqlite and every serving file, verified, 5 kept; the D3 raw copy."""
from __future__ import annotations

import hashlib
import json
import os
import time

import pytest

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


def test_s3_a_write_landing_during_the_ops_backup_does_not_fail_it(backup_dir, monkeypatch):
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


def test_s3_a_failed_ops_backup_does_not_block_the_market_copy(backup_dir, monkeypatch):
    _serve(4)
    ops.ensure()

    def broken(out, manifest):
        raise backup.BackupFailed("ops: simulated")
    monkeypatch.setattr(backup, "_backup_ops", broken)
    assert backup.main(["backup"]) == 1
    assert len(list((backup_dir / "market").glob("market_*.duckdb"))) == 1
