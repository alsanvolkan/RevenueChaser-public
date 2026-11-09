"""``python -m RevenueChaser.rcdata backup``: the weekly backup (avo, 8 Nov 2026): every step verified, 4 kept."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from RevenueChaser.core import config, rawstore
from RevenueChaser.rcdata import backup, ops, serving


def _serve(rows: int = 3, kind: str = "market") -> None:
    with serving.build(kind) as b:
        b.con.execute("CREATE TABLE t AS SELECT range AS i FROM range(?)", [rows])
        b.swap({"t": rows})


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    d = tmp_path / "backups"
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_DIR", str(d))
    return d / backup.ROOT


def _manifest(step_dir: Path) -> dict:
    return json.loads((step_dir / backup.MANIFEST_NAME).read_text(encoding="utf-8"))


# ── the frame: one root, one folder and manifest per step, keep 4 ────────────────────────────────────────────────


def test_the_design_constants():
    assert backup.ROOT == "weekly-v2" and backup.KEEP == 4 and backup.KEEP_MANUAL == 1
    assert backup.STEPS == ("duckdb", "boards", "ops", "uw-raw", "edgar-docs")
    assert not hasattr(backup, "SUBDIRS") and not hasattr(backup, "RAW_COPY_DAYS")


def test_an_empty_backup_dir_exits_2_and_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_DIR", "")
    _serve()
    before = sorted(p for p in tmp_path.rglob("*"))
    assert backup.main(["backup"]) == 2
    assert sorted(p for p in tmp_path.rglob("*")) == before


def test_only_an_unknown_step_exits_2_and_writes_nothing(backup_dir):
    _serve()
    assert backup.main(["backup", "--only", "ops,market,gamma"]) == 2
    assert backup.main(["backup", "--only", " "]) == 2
    assert not backup_dir.exists()


def test_each_step_has_its_own_folder_under_the_root(backup_dir):
    _serve(7)
    ops.ensure()
    summary = backup.run_backup()
    assert set(summary) >= {"market", "ops", "boards", "uw-raw", "edgar-docs"}
    assert (backup_dir / "duckdb" / backup.MANIFEST_NAME).exists()
    assert (backup_dir / "ops" / backup.MANIFEST_NAME).exists()
    assert summary["uw-raw"] is None and summary["edgar-docs"] is None          # nothing of theirs here
    assert not (backup_dir / "market").exists()                                  # the old nightly layout


# ── duckdb: the builder's files, verified against the live file ──────────────────────────────────────────────────


def test_a_verified_copy_of_each_kind_with_its_manifest_entry(backup_dir):
    _serve(7)
    summary = backup.run_backup(only="duckdb")
    out = backup_dir / "duckdb"
    copies = sorted(p.name for p in out.iterdir() if not p.name.startswith("."))
    assert [c.split("_")[0] for c in copies] == ["market"]
    assert not list(out.glob("*.tmp"))
    entry = _manifest(out)["files"][copies[0]]
    assert entry["sha256"] == hashlib.sha256((out / copies[0]).read_bytes()).hexdigest()
    assert entry["tables"]["t"][0] == 7 and entry["source"] == "weekly"
    assert summary["market"] == out / copies[0]


def test_a_copy_that_does_not_match_its_source_is_deleted(backup_dir, monkeypatch):
    _serve()
    real = backup._fingerprints

    def wrong(path):
        got = real(path)
        return {k: (v[0] + 1, v[1]) for k, v in got.items()} if path.name.endswith(".tmp") else got
    monkeypatch.setattr(backup, "_fingerprints", wrong)
    with pytest.raises(backup.BackupFailed):
        backup.run_backup(only="duckdb")
    assert not [p for p in (backup_dir / "duckdb").iterdir() if p.name.startswith("market_")]


def test_retention_keeps_the_newest_four_per_file(backup_dir, sqlite_ops):
    _serve()
    ops.ensure()
    for _ in range(6):
        backup.run_backup(only=["duckdb", "ops"])
    out = backup_dir / "duckdb"
    markets = sorted(p.name for p in out.glob("market_*.duckdb"))
    assert len(markets) == backup.KEEP == 4
    assert len(list((backup_dir / "ops").glob("ops_*.sqlite"))) == 4
    assert sorted(n for n in _manifest(out)["files"] if n.startswith("market_")) == markets


def test_a_copy_by_hand_is_tagged_manual_and_never_costs_a_weekly_copy(backup_dir):
    """``--only <kind>`` before a deploy: kept on its own count (1), the 4 weekly copies untouched."""
    _serve(2)
    for _ in range(4):
        backup.run_backup(only="duckdb")
    for _ in range(3):
        assert backup.main(["backup", "--only", "market"]) == 0
    out = backup_dir / "duckdb"
    manifest = _manifest(out)["files"]
    weekly = [n for n, e in manifest.items() if e["source"] == "weekly"]
    manual = [n for n, e in manifest.items() if e["source"] == "manual"]
    assert len(weekly) == 4 and len(manual) == backup.KEEP_MANUAL == 1
    assert len(list(out.glob("market_*.duckdb"))) == 5
    assert not (backup_dir / "ops").exists()                                     # a kind alone runs no other step


# ── boards: the board-result DuckDBs the dashboard writes live ─────────────────────────────────────────────────


def _board(path: Path, rows: int, schema: int | None = None, keep_wal: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    if keep_wal:
        con.execute("PRAGMA disable_checkpoint_on_shutdown")
    con.execute("CREATE TABLE r AS SELECT range AS i FROM range(?)", [rows])
    if schema is not None:
        con.execute("CREATE TABLE store_schema (key TEXT, value TEXT)")
        con.execute("INSERT INTO store_schema VALUES ('schema_version', ?)", [str(schema)])
    con.close()


def test_board_files_are_the_legacy_store_files_and_boards_dir(backup_dir):
    raw = Path(rawstore.DB_PATH)
    _board(raw, 2)
    _board(rawstore.history_db_path(), 3)
    _board(raw.parent / "boards" / "sec_13f.duckdb", 4)
    _board(raw.parent / "boards" / "value_chain.duckdb", 5)
    (raw.parent / "boards" / "notes.txt").write_text("x")
    assert [p.name for p in backup.board_files()] == ["raw.duckdb", "history.duckdb", "sec_13f.duckdb",
                                                       "value_chain.duckdb"]


def test_the_boards_step_checkpoints_the_wal_then_copies_and_verifies(backup_dir):
    raw = Path(rawstore.DB_PATH)
    _board(raw, 6, schema=41, keep_wal=True)
    _board(raw.parent / "boards" / "sec_13f.duckdb", 4)
    assert raw.with_name("raw.duckdb.wal").exists()
    summary = backup.run_backup(only="boards")["boards"]
    assert set(summary) == {"raw", "sec_13f"}
    assert not raw.with_name("raw.duckdb.wal").exists()                           # the CHECKPOINT flushed it
    out = backup_dir / "boards"
    copy = summary["raw"]
    con = duckdb.connect(str(copy), read_only=True)
    assert con.execute("SELECT count(*) FROM r").fetchone()[0] == 6
    con.close()
    entry = _manifest(out)["files"][copy.name]
    assert entry["store_schema"] == 41 and entry["tables"]["r"][0] == 6 and entry["source"] == "weekly"
    assert _manifest(out)["files"][summary["sec_13f"].name]["store_schema"] is None


def test_a_board_held_by_a_peer_is_waited_out_and_the_other_boards_still_copied(backup_dir, monkeypatch):
    raw = Path(rawstore.DB_PATH)
    _board(raw, 1)
    _board(raw.parent / "boards" / "a.duckdb", 2)
    _board(raw.parent / "boards" / "b.duckdb", 3)
    monkeypatch.setattr(backup, "ATTEMPTS", 2)
    monkeypatch.setattr(backup.time, "sleep", lambda s: None)
    calls: dict[str, int] = {}
    real = backup._checkpoint

    def flaky(path):
        calls[path.name] = calls.get(path.name, 0) + 1
        if path.name == "a.duckdb" and calls[path.name] == 1:
            raise rawstore.StoreLockedError("held by the dashboard")
        if path.name == "b.duckdb":
            raise rawstore.StoreLockedError("held for good")
        real(path)
    monkeypatch.setattr(backup, "_checkpoint", flaky)
    with pytest.raises(backup.BackupFailed, match="b.duckdb"):
        backup.run_backup(only="boards")
    names = sorted(p.name.split("_")[0] for p in (backup_dir / "boards").glob("*.duckdb"))
    assert names == ["a", "raw"] and calls == {"raw.duckdb": 1, "a.duckdb": 2, "b.duckdb": 2}


def test_the_boards_step_with_no_board_file_writes_nothing_but_the_folder(backup_dir):
    assert backup.run_backup(only="boards")["boards"] == {}
    assert not list((backup_dir / "boards").glob("*.duckdb"))


# ── ops: the needed tables only ─────────────────────────────────────────────────────────────────────────────────


def _fill_ops() -> None:
    ops.enqueue("market_series", {"x": 1}, by="test")
    ops.sync_issues("prices/serving", ["price_gap_no_action"], [("price_gap_no_action", "SPY", "price missing")])
    ops.watch_add("stock", "AAPL", by="avo", note="pinned")
    ops.count_calls("uw", 3)
    ops.kind_mode_set("market", "full", by="test")


def test_the_ops_copy_holds_the_kept_tables_and_empties_the_rest(sqlite_ops, backup_dir):
    _fill_ops()
    copy = backup.run_backup(only="ops")["ops"]
    assert copy.parent == backup_dir / "ops" and copy.name.startswith("ops_") and copy.suffix == ".sqlite"
    con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    try:
        counts = {r[0]: con.execute(f'SELECT count(*) FROM "{r[0]}"').fetchone()[0]
                  for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        con.close()
    assert set(counts) == set(ops.TABLES)                                        # every table is there (ops_copy reads it)
    assert counts["watchlist_stock"] == 1 and counts["watchlist_stock_changes"] == 1
    assert counts["kind_automation"] == 1 and counts["ops_schema"] >= 1
    assert counts["build_requests"] == 0 and counts["build_issues"] == 0 and counts["provider_calls_daily"] == 0
    entry = _manifest(backup_dir / "ops")["files"][copy.name]
    assert entry["backend"] == "sqlite" and "build_requests" not in entry["tables"]
    assert entry["tables"]["watchlist_stock"] == 1


def test_the_ops_copy_restores_by_taking_the_file_into_an_empty_market_root(sqlite_ops, backup_dir, monkeypatch,
                                                                            tmp_path):
    _fill_ops()
    taken = backup.run_backup(only="ops")["ops"]
    want = {t: ops.rows(t) for t in ops.TABLES if backup._is_kept_ops_table(t)}
    restored = tmp_path / "restored"
    restored.mkdir()
    monkeypatch.setattr(config, "MARKET_DIR", restored)                          # an empty market root
    (restored / "ops.sqlite").write_bytes(taken.read_bytes())
    ops.ensure()                                                                 # the stamp is current: no upgrade
    assert {t: ops.rows(t) for t in want} == want
    assert ops.rows("build_requests") == []


def test_s3_a_write_landing_during_the_ops_backup_does_not_fail_it(sqlite_ops, backup_dir, monkeypatch):
    """The counts and the online backup are one read transaction: a write landing between them is in neither."""
    ops.watch_add("stock", "AAPL", by="test")
    real = backup._online_backup

    def write_then_copy(source, target):
        ops.watch_add("stock", "MSFT", by="a writer racing the backup")
        real(source, target)
    monkeypatch.setattr(backup, "_online_backup", write_then_copy)
    copy = backup.run_backup(only="ops")["ops"]
    con = sqlite3.connect(copy)
    assert con.execute("SELECT count(*) FROM watchlist_stock").fetchone()[0] == 1
    con.close()


def test_the_ops_copy_is_built_off_the_backup_folder(sqlite_ops, backup_dir, monkeypatch):
    """SQLite writes a database page by page, and the backup folder is slow storage: only the finished file is
    copied there (5 Nov 2026: 8m44s for a 5.8 MB ops.sqlite)."""
    ops.ensure()
    real = backup._online_backup
    targets = []

    def watch(source, target):
        targets.append(Path(target.execute("PRAGMA database_list").fetchone()[2]))
        real(source, target)
    monkeypatch.setattr(backup, "_online_backup", watch)
    copy = backup.run_backup(only="ops")["ops"]
    assert targets and backup_dir not in targets[0].parents
    assert copy.exists() and not list((backup_dir / "ops").glob("*.tmp*"))


def test_an_ops_copy_whose_kept_rows_do_not_match_is_failed_and_leaves_nothing(sqlite_ops, backup_dir, monkeypatch):
    ops.watch_add("stock", "AAPL", by="test")
    real = backup._sqlite_counts
    monkeypatch.setattr(backup, "_sqlite_counts", lambda p: {**real(p), "watchlist_stock": 0})
    with pytest.raises(backup.BackupFailed, match="ops"):
        backup.run_backup(only="ops")
    out = backup_dir / "ops"
    assert not list(out.glob("*.tmp*")) and not list(out.glob("ops_*.sqlite"))
    manifest = out / backup.MANIFEST_NAME
    assert not manifest.exists() or "ops_" not in manifest.read_text(encoding="utf-8")


def test_a_difference_in_a_table_the_backup_empties_is_not_a_failure(sqlite_ops, backup_dir, monkeypatch):
    ops.enqueue("market_series", None, by="test")
    real = backup._sqlite_counts
    monkeypatch.setattr(backup, "_sqlite_counts", lambda p: {**real(p), "build_requests": 99})
    assert backup.run_backup(only="ops")["ops"].exists()


def test_a_killed_runs_temp_files_and_their_wal_and_shm_are_swept(sqlite_ops, backup_dir):
    out = backup_dir / "ops"
    out.mkdir(parents=True)
    orphans = [out / n for n in ("ops_1.sqlite.tmp", "ops_1.sqlite.tmp-wal", "ops_1.sqlite.tmp-shm")]
    for p in orphans:
        p.write_bytes(b"x")
    ops.ensure()
    kept = backup.run_backup(only="ops")["ops"]
    for p in orphans:
        p.write_bytes(b"x")
    copy = backup.run_backup(only="ops")["ops"]
    assert not any(p.exists() for p in orphans)
    assert copy.exists() and kept.exists() and kept != copy
    assert kept.name in (out / backup.MANIFEST_NAME).read_text(encoding="utf-8")


def test_a_file_this_job_did_not_write_is_logged_and_left_alone(sqlite_ops, backup_dir, caplog):
    out = backup_dir / "ops"
    out.mkdir(parents=True)
    foreign = out / "notes-from-avo.txt"
    foreign.write_text("keep me")
    ops.ensure()
    with caplog.at_level("WARNING"):
        assert backup.run_backup(only="ops")["ops"].exists()
    assert foreign.read_text() == "keep me" and "notes-from-avo.txt" in caplog.text


def test_s3_a_failed_ops_backup_does_not_block_the_duckdb_copy(backup_dir, monkeypatch):
    _serve(4)
    ops.ensure()

    def broken(out, manifest):
        raise backup.BackupFailed("ops: simulated")
    monkeypatch.setattr(backup, "_backup_ops", broken)
    assert backup.main(["backup", "--only", "duckdb,ops"]) == 1
    assert len(list((backup_dir / "duckdb").glob("market_*.duckdb"))) == 1


# ── On Redis: a filtered snapshot, kept as an ops.sqlite ─────────────────────────────────────────────────────────

_on_redis = pytest.mark.skipif(not os.getenv("RC_REDIS_URL"), reason="needs a Redis at RC_REDIS_URL (CI runs one)")


@_on_redis
def test_on_redis_the_ops_backup_holds_the_kept_tables_only(backup_dir):
    _fill_ops()
    copy = backup.run_backup(only="ops")["ops"]
    from RevenueChaser.rcdata import ops_copy
    con = ops_copy._open_read_only(copy)
    try:
        for table in ops_copy.COPIED:
            want = ops.rows(table) if backup._is_kept_ops_table(table) else []
            assert ops_copy._table_rows(con, table) == want, table
        assert con.execute("SELECT max(version) FROM ops_schema").fetchone()[0] == ops.OPS_SCHEMA_VERSION
    finally:
        con.close()
    entry = _manifest(backup_dir / "ops")["files"][copy.name]
    assert entry["backend"] == "redis" and entry["tables"]["watchlist_stock"] == 1
    assert "build_issues" not in entry["tables"]


@_on_redis
def test_on_redis_the_backup_restores_with_the_one_time_copy(backup_dir, monkeypatch, tmp_path):
    _fill_ops()
    taken = backup.run_backup(only="ops")["ops"]
    want = {t: ops.rows(t) for t in ops.TABLES if backup._is_kept_ops_table(t)}
    monkeypatch.setattr(config, "MARKET_DIR", tmp_path / "restored")          # an empty namespace
    from RevenueChaser.rcdata import ops_copy
    assert ops_copy.main(["--sqlite", str(taken)]) == 0
    assert {t: ops.rows(t) for t in want} == want
    assert ops.rows("build_requests") == []


def test_the_redis_ops_copy_is_written_in_one_transaction_and_filtered(backup_dir, monkeypatch):
    # #387: an autocommit insert per row made 42k commits, 9-11 min a night and past the 30 min unit limit.
    from RevenueChaser.rcdata import ops_copy, ops_redis
    ops.watch_add("stock", "AAPL", by="test")
    ops.watch_add("stock", "MSFT", by="test")
    ops.enqueue("market_series", {"x": 1}, by="test")
    taken = {t: ops.rows(t) for t in ops.TABLES if t != "ops_schema"}
    assert len(taken["watchlist_stock"]) == 2 and len(taken["build_requests"]) == 1
    monkeypatch.setattr(ops_redis, "snapshot", lambda: taken)
    monkeypatch.setattr(ops, "schema_version", lambda: ops.OPS_SCHEMA_VERSION)
    real, seen = ops.connect, []

    def traced(path=None):
        cm = real(path)
        con = cm.__enter__()
        con.set_trace_callback(seen.append)
        return _Exit(cm, con)

    monkeypatch.setattr(ops, "connect", traced)
    backup_dir.mkdir(parents=True)
    dest = backup._backup_ops_redis(backup_dir, backup._Manifest(backup_dir))
    inserts = [i for i, s in enumerate(seen) if s.startswith("INSERT INTO watchlist_stock ")]
    marks = [(i, s) for i, s in enumerate(seen) if s.startswith(("BEGIN", "COMMIT"))]
    assert len(inserts) == 2                                                      # the trace fires once per row
    assert not [s for s in seen if s.startswith("INSERT INTO build_requests")]
    before = [s for i, s in marks if i < inserts[0]]
    assert before and before[-1].startswith("BEGIN")
    assert [i for i, s in marks if i > inserts[-1] and s == "COMMIT"]
    con = ops_copy._open_read_only(dest)
    try:
        assert ops_copy._table_rows(con, "watchlist_stock") == taken["watchlist_stock"]
        assert ops_copy._table_rows(con, "build_requests") == []
    finally:
        con.close()


class _Exit:
    def __init__(self, cm, con):
        self.cm, self.con = cm, con

    def __enter__(self):
        return self.con

    def __exit__(self, *exc):
        return self.cm.__exit__(*exc)


# ── uw-raw: the UW day files and the SPX Gamma tree, one folder per ISO week, unchanged files hard-linked ──────


def _file(path: Path, data: bytes, age_s: float = 3600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    t = time.time() - age_s
    os.utime(path, (t, t))


@pytest.fixture
def raw_trees(tmp_path, monkeypatch):
    uw = config.MARKET_RAW_DIR / "raw" / "uw"
    spx = tmp_path / "spx_gamma"
    from RevenueChaser.module_spx_gamma import config as spx_config
    monkeypatch.setattr(spx_config, "DATA_ROOT", spx)
    _file(uw / "spot_expiry_strike" / "2026-10-26" / "SPX.json.gz", b"a" * 20)
    _file(uw / "greek_expiry_strike" / "2026-10-26" / "SPX.json.gz", b"b" * 30)
    _file(spx / "spx_gamma_gex_history.json", b'{"days": [1]}')
    _file(spx / "raw" / "2026-10-26" / "chain.json", b'{"c": 1}')
    return uw, spx


def _week(n: int) -> datetime:
    """A Monday in ISO week ``n`` of 2026 (week 1 starts Monday 29 Dec 2025)."""
    return datetime(2025, 12, 29, tzinfo=timezone.utc) + timedelta(weeks=n - 1)


def test_uw_raw_copies_both_trees_into_the_weeks_folder_and_verifies(backup_dir, raw_trees):
    uw, spx = raw_trees
    summary = backup.run_backup(only="uw-raw")["uw-raw"]
    week = backup._iso_week()
    out = backup_dir / "uw-raw" / week
    assert summary["week"] == week
    assert summary["uw"] == {"copied": 2, "linked": 0, "skipped": 0, "rejected": 0}
    assert summary["spx_gamma"]["copied"] == 2
    copy = out / "uw" / "spot_expiry_strike" / "2026-10-26" / "SPX.json.gz"
    assert copy.read_bytes() == b"a" * 20
    assert (out / "spx_gamma" / "raw" / "2026-10-26" / "chain.json").exists()
    manifest = _manifest(backup_dir / "uw-raw")
    key = f"{week}/uw/spot_expiry_strike/2026-10-26/SPX.json.gz"
    assert manifest["files"][key]["sha256"] == hashlib.sha256(b"a" * 20).hexdigest()
    assert manifest["weeks"] == [week]
    assert not (out / ".tmp").exists()


def test_uw_raw_hard_links_unchanged_files_to_last_weeks_copy(backup_dir, raw_trees):
    uw, spx = raw_trees
    out = backup_dir / "uw-raw"
    out.mkdir(parents=True)
    manifest = backup._Manifest(out)
    backup._backup_uw_raw(out, manifest, now=_week(1))
    _file(uw / "spot_expiry_strike" / "2026-10-27" / "SPX.json.gz", b"c" * 10)   # a new day
    _file(spx / "spx_gamma_gex_history.json", b'{"days": [1, 2]}')                  # rewritten in place
    summary = backup._backup_uw_raw(out, manifest, now=_week(2))
    assert summary["uw"] == {"copied": 1, "linked": 2, "skipped": 0, "rejected": 0}
    assert summary["spx_gamma"] == {"copied": 1, "linked": 1, "skipped": 0, "rejected": 0}
    w1, w2 = out / backup._iso_week(_week(1)), out / backup._iso_week(_week(2))
    rel = Path("uw/spot_expiry_strike/2026-10-26/SPX.json.gz")
    assert (w1 / rel).stat().st_ino == (w2 / rel).stat().st_ino                   # one copy on disk
    assert (w2 / "spx_gamma" / "spx_gamma_gex_history.json").read_bytes() == b'{"days": [1, 2]}'
    assert (w1 / "spx_gamma" / "spx_gamma_gex_history.json").read_bytes() == b'{"days": [1]}'
    entry = _manifest(out)["files"][f"{w2.name}/{rel.as_posix()}"]
    assert entry["linked_from"] == f"{w1.name}/{rel.as_posix()}"


def test_uw_raw_waits_for_a_file_still_being_written(backup_dir, raw_trees):
    uw, _ = raw_trees
    _file(uw / "spot_expiry_strike" / "2026-10-28" / "SPX.json.gz", b"d", age_s=5)   # modified 5 s ago
    summary = backup.run_backup(only="uw-raw")["uw-raw"]
    assert summary["uw"]["skipped"] == 1 and summary["uw"]["copied"] == 2
    week = backup_dir / "uw-raw" / backup._iso_week()
    assert not (week / "uw" / "spot_expiry_strike" / "2026-10-28").exists()


def test_uw_raw_a_damaged_json_never_replaces_a_good_copy(backup_dir, raw_trees, caplog):
    _, spx = raw_trees
    out = backup_dir / "uw-raw"
    out.mkdir(parents=True)
    manifest = backup._Manifest(out)
    backup._backup_uw_raw(out, manifest, now=_week(1))
    _file(spx / "spx_gamma_gex_history.json", b'{"days": [1, 2')                   # cut mid-write
    with caplog.at_level("WARNING"):
        summary = backup._backup_uw_raw(out, manifest, now=_week(2))
    assert summary["spx_gamma"]["rejected"] == 1
    w2 = out / backup._iso_week(_week(2))
    assert (w2 / "spx_gamma" / "spx_gamma_gex_history.json").read_bytes() == b'{"days": [1]}'
    assert "damaged JSON" in caplog.text


def test_uw_raw_a_copy_whose_hash_differs_is_a_failed_step(backup_dir, raw_trees, monkeypatch):
    real = backup._sha256
    monkeypatch.setattr(backup, "_sha256", lambda p: "0" * 64 if ".part" in p.name else real(p))
    with pytest.raises(backup.BackupFailed, match="SHA-256"):
        backup.run_backup(only="uw-raw")
    week = backup_dir / "uw-raw" / backup._iso_week()
    assert not list(p for p in week.rglob("*") if p.is_file())


def test_uw_raw_keeps_the_newest_four_weeks(backup_dir, raw_trees):
    out = backup_dir / "uw-raw"
    out.mkdir(parents=True)
    manifest = backup._Manifest(out)
    for n in range(1, 7):
        backup._backup_uw_raw(out, manifest, now=_week(n))
    weeks = [p.name for p in backup._week_dirs(out)]
    assert weeks == [backup._iso_week(_week(n)) for n in range(3, 7)]
    data = _manifest(out)
    assert data["weeks"] == weeks
    assert all(k.split("/")[0] in weeks for k in data["files"])
    # the oldest copy is gone but the linked file lives on in the kept weeks (a hard link survives its sibling)
    rel = "uw/spot_expiry_strike/2026-10-26/SPX.json.gz"
    assert (out / weeks[0] / rel).read_bytes() == b"a" * 20


def test_uw_raw_with_no_source_tree_writes_nothing(backup_dir, tmp_path, monkeypatch):
    from RevenueChaser.module_spx_gamma import config as spx_config
    monkeypatch.setattr(spx_config, "DATA_ROOT", tmp_path / "nowhere")
    assert backup.run_backup(only="uw-raw")["uw-raw"] is None
    assert not list((backup_dir / "uw-raw").iterdir())


# ── edgar-docs: one verified zip, reused while the tree is unchanged ────────────────────────────────────────────


@pytest.fixture
def docs_tree(tmp_path, monkeypatch):
    d = tmp_path / "edgar_docs"
    monkeypatch.setattr(config, "EDGAR_DOCS_DIR", d)
    _file(d / "0000320193" / "a.html.gz", b"doc-a")
    _file(d / "0000320193" / "b.html.gz", b"doc-b")
    _file(d / "index.json", b"{}")
    return d


def test_edgar_docs_is_one_verified_zip_with_the_document_count(backup_dir, docs_tree):
    import zipfile
    copy = backup.run_backup(only="edgar-docs")["edgar-docs"]
    assert copy.parent == backup_dir / "edgar-docs" and copy.name.startswith("edgar_docs_") and copy.suffix == ".zip"
    with zipfile.ZipFile(copy) as zf:
        assert sorted(n for n in zf.namelist() if not n.endswith("/")) == ["0000320193/a.html.gz",
                                                                            "0000320193/b.html.gz", "index.json"]
        assert zf.comment.startswith(b"rc-edgar-docs-tree-sha256:")
    entry = _manifest(copy.parent)["files"][copy.name]
    assert entry["documents"] == 2 and entry["sha256"] == hashlib.sha256(copy.read_bytes()).hexdigest()
    assert not list(copy.parent.glob("*.partial*"))


def test_edgar_docs_an_unchanged_tree_reuses_the_newest_zip(backup_dir, docs_tree):
    first = backup.run_backup(only="edgar-docs")["edgar-docs"]
    assert backup.run_backup(only="edgar-docs")["edgar-docs"] == first
    assert len(list((backup_dir / "edgar-docs").glob("edgar_docs_*.zip"))) == 1
    _file(docs_tree / "0000320193" / "c.html.gz", b"doc-c")
    third = backup.run_backup(only="edgar-docs")["edgar-docs"]
    assert third != first and len(list((backup_dir / "edgar-docs").glob("edgar_docs_*.zip"))) == 2
    assert _manifest(backup_dir / "edgar-docs")["files"][third.name]["documents"] == 3


def test_edgar_docs_a_damaged_zip_is_replaced_and_retention_keeps_four(backup_dir, docs_tree):
    zips = []
    for n in range(6):
        _file(docs_tree / "0000320193" / f"n{n}.html.gz", b"x")
        zips.append(backup.run_backup(only="edgar-docs")["edgar-docs"])
    out = backup_dir / "edgar-docs"
    kept = sorted(out.glob("edgar_docs_*.zip"))
    assert len(kept) == backup.KEEP == 4 and kept == sorted(zips[-4:])
    kept[-1].write_bytes(b"not a zip")                                            # the newest got damaged on disk
    fresh = backup.run_backup(only="edgar-docs")["edgar-docs"]
    assert fresh != kept[-1] and not kept[-1].exists()
    assert kept[-1].name not in _manifest(out)["files"] and fresh.name in _manifest(out)["files"]


def test_edgar_docs_with_no_documents_writes_nothing(backup_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EDGAR_DOCS_DIR", tmp_path / "empty_docs")
    assert backup.run_backup(only="edgar-docs")["edgar-docs"] is None


# ── --only and the CLI ──────────────────────────────────────────────────────────────────────────────────────────


def test_check_only_splits_steps_from_kinds():
    assert backup._check_only(None) == (backup.STEPS, ())
    assert backup._check_only("ops, boards") == (("boards", "ops"), ())
    assert backup._check_only(["market", "ops"]) == (("ops",), ("market",))
    with pytest.raises(backup.BackupRefused):
        backup._check_only("")


def test_only_runs_just_the_named_steps(backup_dir, raw_trees):
    _serve(5)
    ops.ensure()
    summary = backup.run_backup(only=["duckdb"])
    assert set(summary) == set(serving.KINDS) and summary["market"] is not None
    assert not (backup_dir / "ops").exists() and not (backup_dir / "uw-raw").exists()


def test_the_cli_runs_every_step_and_reports_each(backup_dir, raw_trees, sqlite_ops, capsys):
    _serve(3)
    ops.ensure()
    assert backup.main(["backup"]) == 0
    text = capsys.readouterr().out
    assert "market:" in text and "ops:" in text and "uw-raw:" in text and "edgar-docs: nothing to back up" in text
    assert len(list((backup_dir / "duckdb").glob("market_*.duckdb"))) == 1
    assert len(list((backup_dir / "ops").glob("ops_*.sqlite"))) == 1
    assert (backup_dir / "uw-raw" / backup._iso_week()).is_dir()
