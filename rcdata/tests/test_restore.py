"""``python -m RevenueChaser.rcdata restore`` / ``restore-check``: a verified weekly copy back into use."""
from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pytest

from RevenueChaser.core import config, rawstore
from RevenueChaser.rcdata import backup, ops, restore, serving


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    d = tmp_path / "backups"
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_DIR", str(d))
    return d / backup.ROOT


def _serve(rows: int = 3, kind: str = "market") -> None:
    with serving.build(kind) as b:
        b.con.execute("CREATE TABLE t AS SELECT range AS i FROM range(?)", [rows])
        b.swap({"t": rows})


def _board(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute("CREATE OR REPLACE TABLE r AS SELECT range AS i FROM range(?)", [rows])
    con.close()


def _rows(path: Path, table: str) -> int:
    con = duckdb.connect(str(path), read_only=True)
    try:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def test_the_cli_knows_restore_and_restore_check(backup_dir, capsys):
    assert backup.main(["restore", "--step", "ops", "--latest", "--into", str(backup_dir / "x")]) == 2
    assert "no copy of step ops" in capsys.readouterr().out
    assert backup.main(["restore-check"]) == 2


def test_copies_and_latest_follow_the_manifest_not_the_folder(backup_dir, sqlite_ops):
    _serve(2)
    _serve(4, "spine")
    ops.ensure()
    for _ in range(2):
        backup.run_backup(only=["duckdb", "ops"])
    (backup_dir / "duckdb" / "market_99999999_000000_000000.duckdb").write_bytes(b"not ours")   # a foreign file
    found = restore.copies("duckdb", "market")
    assert len(found) == 2 and all(p.name.startswith("market_") for p in found)
    assert restore.latest("duckdb", "market") == found[-1]
    assert restore.latest("ops").name.startswith("ops_")
    with pytest.raises(restore.RestoreRefused, match="not in"):
        restore.verify("duckdb", backup_dir / "duckdb" / "market_99999999_000000_000000.duckdb")


def test_restore_into_a_folder_verifies_and_writes_the_copy_only(backup_dir, tmp_path):
    _serve(5)
    backup.run_backup(only="duckdb")
    src = restore.latest("duckdb", "market")
    into = tmp_path / "out"
    dest = restore.restore_into("duckdb", src, into)
    assert dest == into / src.name and _rows(dest, "t") == 5
    assert _rows(serving.live_path("market"), "t") == 5                         # untouched
    with pytest.raises(restore.RestoreRefused, match="exists already"):
        restore.restore_into("duckdb", src, into)


def test_a_copy_that_no_longer_matches_its_manifest_is_refused(backup_dir, tmp_path):
    _serve(5)
    backup.run_backup(only="duckdb")
    src = restore.latest("duckdb", "market")
    with open(src, "r+b") as f:                                                  # a flipped byte on the HDD
        f.seek(100)
        b = f.read(1)
        f.seek(100)
        f.write(bytes([b[0] ^ 0xFF]))
    with pytest.raises(restore.RestoreRefused, match="sha256"):
        restore.restore_into("duckdb", src, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_restore_live_replaces_the_duckdb_kind_and_keeps_the_previous_file(backup_dir):
    _serve(5)
    backup.run_backup(only="duckdb")
    _serve(9)                                                                    # the live file moved on
    live = serving.live_path("market")
    assert _rows(live, "t") == 9
    target = restore.restore_live("duckdb", restore.latest("duckdb", "market"))
    assert target == live and _rows(live, "t") == 5
    assert _rows(live.with_name(live.name + restore.PRE_RESTORE), "t") == 9


def test_restore_live_of_a_board_file_refuses_a_peer_and_a_wal(backup_dir, monkeypatch):
    raw = Path(rawstore.DB_PATH)
    _board(raw, 3)
    _board(raw.parent / "boards" / "sec_13f.duckdb", 2)
    backup.run_backup(only="boards")
    src = restore.latest("boards", "sec_13f")
    assert restore.live_target("boards", src) == raw.parent / "boards" / "sec_13f.duckdb"
    assert restore.live_target("boards", restore.latest("boards", "raw")) == raw
    monkeypatch.setattr(rawstore, "_peer_pids", lambda path: [4242])
    with pytest.raises(restore.RestoreRefused, match="4242"):
        restore.restore_live("boards", src)
    monkeypatch.setattr(rawstore, "_peer_pids", lambda path: [])
    wal = raw.parent / "boards" / "sec_13f.duckdb.wal"
    wal.write_bytes(b"x")
    with pytest.raises(restore.RestoreRefused, match="write-ahead log"):
        restore.restore_live("boards", src)
    wal.unlink()
    _board(raw.parent / "boards" / "sec_13f.duckdb", 7)
    assert _rows(restore.restore_live("boards", src), "r") == 2


def test_ops_uw_raw_and_edgar_docs_live_are_refused_with_the_steps_by_hand(backup_dir, sqlite_ops, capsys):
    ops.ensure()
    backup.run_backup(only="ops")
    assert backup.main(["restore", "--step", "ops", "--latest", "--live"]) == 2
    out = capsys.readouterr().out
    assert "ops_copy --sqlite" in out and "EMPTY namespace" in out


def test_a_week_folder_is_restored_whole_and_verified(backup_dir, tmp_path, monkeypatch):
    from RevenueChaser.module_spx_gamma import config as spx_config
    monkeypatch.setattr(spx_config, "DATA_ROOT", tmp_path / "nowhere")
    uw = config.MARKET_RAW_DIR / "raw" / "uw" / "spot_expiry_strike" / "2026-10-26"
    uw.mkdir(parents=True)
    (uw / "SPX.json.gz").write_bytes(b"a" * 20)
    t = os.path.getmtime(uw / "SPX.json.gz") - 3600
    os.utime(uw / "SPX.json.gz", (t, t))
    backup.run_backup(only="uw-raw")
    week = restore.latest("uw-raw")
    assert week.is_dir() and week.name == backup._iso_week()
    dest = restore.restore_into("uw-raw", week, tmp_path / "out")
    assert (dest / "uw" / "spot_expiry_strike" / "2026-10-26" / "SPX.json.gz").read_bytes() == b"a" * 20
    (week / "uw" / "spot_expiry_strike" / "2026-10-26" / "SPX.json.gz").write_bytes(b"b" * 20)
    with pytest.raises(restore.RestoreRefused, match="differ"):
        restore.verify("uw-raw", week)


def test_restore_check_restores_ops_and_a_board_into_scratch_and_cleans_up(backup_dir, sqlite_ops, tmp_path):
    raw = Path(rawstore.DB_PATH)
    _board(raw, 3)
    _board(raw.parent / "boards" / "a_board.duckdb", 2)
    ops.watch_add("stock", "AAPL", by="test")
    backup.run_backup(only=["boards", "ops"])
    scratch = tmp_path / "scratch"
    result = restore.restore_check(scratch)
    assert set(result) == {"ops", "boards"} and result["boards"]["file"].startswith("raw_")
    assert result["ops"]["tables"] >= 2
    assert scratch.is_dir() and not list(scratch.iterdir())                      # its own files removed
    assert restore.restore_check() == result                                     # a temp folder of its own


def test_restore_check_fails_on_a_copy_that_does_not_match(backup_dir, sqlite_ops, monkeypatch, capsys):
    raw = Path(rawstore.DB_PATH)
    _board(raw, 3)
    ops.ensure()
    backup.run_backup(only=["boards", "ops"])
    m = backup_dir / "boards" / backup.MANIFEST_NAME
    data = json.loads(m.read_text())
    name = next(iter(data["files"]))
    data["files"][name]["tables"]["r"][0] = 99
    m.write_text(json.dumps(data))
    assert backup.main(["restore-check"]) == 2
    assert "fingerprints differ" in capsys.readouterr().out
