"""#287 J3: ``.store/history.duckdb`` is a further store file for the backup. Since schema 11 the MANIFEST declares its tables, but the
tests here create the file by hand. Backup, weekly copy, off-machine copy and restore cover it; a store without the file
backs up exactly as before. Temp stores only; the clock is driven through ``backup_store._utcnow``."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from RevenueChaser.core import automations, config, rawstore
from RevenueChaser.scripts import backup_store as bs
from RevenueChaser.scripts import restore_store as rs

SUBDIR = "RevenueChaser-backup"


def _day(y, m, d, hh=2):
    return datetime(y, m, d, hh, 0, 5, 123456, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    state = {"now": _day(2026, 9, 28)}
    monkeypatch.setattr(bs, "_utcnow", lambda: state["now"])
    return lambda dt: state.update(now=dt)


def _write_history(rows: int = 3, path: Path | None = None) -> Path:
    """Create ``history.duckdb`` with one hand-made table (the MANIFEST declares none yet)."""
    path = path or rawstore.history_db_path()
    conn = duckdb.connect(str(path))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS hist_probe (session_date TEXT, v BIGINT)")
        conn.execute("DELETE FROM hist_probe")
        for i in range(rows):
            conn.execute("INSERT INTO hist_probe VALUES ('2026-09-28', ?)", [i])
        conn.execute("CHECKPOINT")
    finally:
        conn.close()
    return path


def _names(folder: Path, pattern: str) -> list[str]:
    return sorted(p.name for p in folder.glob(pattern))


# --- the path ---------------------------------------------------------------------------------------------------

def test_history_db_path_sits_beside_the_raw_store_and_follows_redirects(rawstore_tmp_db):
    assert rawstore.history_db_path() == Path(rawstore.DB_PATH).parent / "history.duckdb"
    assert rawstore._db_path("history") == rawstore.history_db_path()


def test_the_other_db_paths_are_unchanged(rawstore_tmp_db):
    assert rawstore._db_path("raw") == Path(rawstore.DB_PATH)
    assert rawstore._db_path("edgar") == rawstore.edgar_db_path()
    assert rawstore._db_path("discord") == rawstore.discord_db_path()
    assert rawstore._db_path("anything-else") == Path(rawstore.DB_PATH)


def test_reset_db_removes_history_only_on_a_full_reset(rawstore_tmp_db):
    hist = _write_history()
    rawstore.reset_db(include_edgar=False)
    assert hist.exists()
    rawstore.reset_db(include_edgar=True)
    assert not hist.exists()


def test_every_history_file_table_is_an_append_only_history_table(rawstore_tmp_db):
    """Schema 11 (history redesign C): the 18 result-history tables live in ``history.duckdb`` and nowhere else, each
    flagged ``history`` (append-only); the file is made by the first recorded run, not by the first store call. The
    18th is ``rotation_snapshots_history`` (#374, an addition with no bump); the nightly snapshot copies the whole file,
    so it is backed up with the rest."""
    names = [n for n in rawstore.MANIFEST if rawstore._db_of(n) == "history"]
    assert len(names) == 18 and "rotation_snapshots_history" in names
    assert all(rawstore.MANIFEST[n].get("history") and rawstore.MANIFEST[n]["write"] == "append" for n in names)
    rawstore.ensure_all_tables()
    assert not rawstore.history_db_path().exists()                  # no recorded run yet, so no empty file either


# --- snapshot ---------------------------------------------------------------------------------------------------

def test_snapshot_history_writes_a_verified_snapshot(rawstore_tmp_db, tmp_path):
    _write_history(3)
    dest = bs.snapshot_history(out_dir=tmp_path)
    assert dest.name.startswith("history_") and dest.suffix == ".duckdb"
    assert bs._table_counts(dest) == {"hist_probe": 3}
    assert bs._table_fingerprints(dest) == bs._table_fingerprints(rawstore.history_db_path())


def test_snapshot_history_is_none_when_the_file_does_not_exist(rawstore_tmp_db, tmp_path):
    assert bs.snapshot_history(out_dir=tmp_path) is None
    assert not list(tmp_path.glob("history_*"))


def test_snapshot_history_keep_prunes_only_history_snapshots(rawstore_tmp_db, tmp_path):
    _write_history()
    raw = bs.snapshot(out_dir=tmp_path, keep=5)
    first = bs.snapshot_history(out_dir=tmp_path, keep=1)
    second = bs.snapshot_history(out_dir=tmp_path, keep=1)
    assert not first.exists() and second.is_file() and raw.is_file()


def test_snapshot_history_default_keeps_five(rawstore_tmp_db, tmp_path):
    _write_history()
    made = [bs.snapshot_history(out_dir=tmp_path) for _ in range(7)]
    assert _names(tmp_path, "history_*.duckdb") == sorted(p.name for p in made[-5:])


# --- weekly copy ------------------------------------------------------------------------------------------------

def test_weekly_keeps_the_first_history_snapshot_of_a_week_next_to_the_raw_one(rawstore_tmp_db, tmp_path, clock):
    _write_history()
    out = tmp_path / "out"
    raw1, hist1 = bs.snapshot(out_dir=out), bs.snapshot_history(out_dir=out)
    wr, wh = bs.keep_weekly(raw1, out_dir=out), bs.keep_weekly(hist1, out_dir=out)
    assert wr == out / "weekly" / raw1.name and wh == out / "weekly" / hist1.name
    clock(_day(2026, 9, 29))                                       # same ISO week: nothing new for either kind
    raw2, hist2 = bs.snapshot(out_dir=out), bs.snapshot_history(out_dir=out)
    assert bs.keep_weekly(raw2, out_dir=out) is None and bs.keep_weekly(hist2, out_dir=out) is None
    clock(_day(2026, 10, 5))                                       # next ISO week: both get a new one
    raw3, hist3 = bs.snapshot(out_dir=out), bs.snapshot_history(out_dir=out)
    assert bs.keep_weekly(raw3, out_dir=out) is not None and bs.keep_weekly(hist3, out_dir=out) is not None
    assert len(_names(out / "weekly", "raw_*.duckdb")) == 2 and len(_names(out / "weekly", "history_*.duckdb")) == 2


def test_weekly_history_copy_is_verified_and_never_pruned(rawstore_tmp_db, tmp_path, clock):
    _write_history()
    out = tmp_path / "out"
    for i in range(4):
        clock(_day(2026, 9, 28) + timedelta(weeks=i))
        bs.keep_weekly(bs.snapshot_history(out_dir=out, keep=1), out_dir=out)
    weekly = _names(out / "weekly", "history_*.duckdb")
    assert len(weekly) == 4 and len(_names(out, "history_*.duckdb")) == 1
    assert bs._table_fingerprints(out / "weekly" / weekly[0]) == bs._table_fingerprints(rawstore.history_db_path())


# --- the nightly run --------------------------------------------------------------------------------------------

def test_main_backs_up_history_with_a_weekly_copy_and_logs_it(rawstore_tmp_db, tmp_path, clock, capsys):
    rawstore.write_rows("watchlist", [{"ticker": "NOK"}])
    _write_history()
    out = tmp_path / "out"
    assert bs.main(["--out", str(out)]) == 0
    text = capsys.readouterr().out
    snap = _names(out, "history_*.duckdb")
    assert len(snap) == 1 and f"backup OK: {out / snap[0]}" in text
    assert _names(out / "weekly", "history_*.duckdb") == snap and f"weekly backup OK: {out / 'weekly' / snap[0]}" in text


def test_main_keeps_five_history_snapshots_by_default_and_keep_history_overrides(rawstore_tmp_db, tmp_path, clock):
    rawstore.write_rows("watchlist", [{"ticker": "NOK"}])
    _write_history()
    out = tmp_path / "out"
    for i in range(7):
        clock(_day(2026, 9, 28) + timedelta(days=i))
        assert bs.main(["--out", str(out)]) == 0
    assert len(_names(out, "history_*.duckdb")) == 5
    clock(_day(2026, 10, 6))
    assert bs.main(["--out", str(out), "--keep-history", "2"]) == 0
    assert len(_names(out, "history_*.duckdb")) == 2


def test_the_history_weekly_copy_ignores_the_raw_stopgap_switch(rawstore_tmp_db, tmp_path, clock, monkeypatch):
    """M1 (review): ``--no-weekly`` / ``RAW_STORE_BACKUP_WEEKLY=0`` end the raw stopgap (#279). History keeps its
    weekly copy, or its retention would fall below raw's ("longer retention", #287)."""
    rawstore.write_rows("watchlist", [{"ticker": "NOK"}])
    _write_history()
    out = tmp_path / "out"
    assert bs.main(["--out", str(out), "--no-weekly"]) == 0
    assert len(_names(out, "history_*.duckdb")) == 1
    assert len(_names(out / "weekly", "history_*.duckdb")) == 1 and not _names(out / "weekly", "raw_*.duckdb")
    out2 = tmp_path / "out2"
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_WEEKLY", "0")
    assert bs.main(["--out", str(out2)]) == 0
    assert len(_names(out2 / "weekly", "history_*.duckdb")) == 1 and not _names(out2 / "weekly", "raw_*.duckdb")


def test_a_store_without_history_backs_up_exactly_as_before(rawstore_tmp_db, tmp_path, clock, capsys):
    rawstore.write_rows("watchlist", [{"ticker": "NOK"}])
    out = tmp_path / "out"
    assert bs.main(["--out", str(out)]) == 0
    text = capsys.readouterr()
    assert "history_" not in (text.out + text.err).replace(str(out), "")     # the tmp path carries the test name
    assert "history backup" not in text.out + text.err
    assert not list(out.rglob("history_*"))
    assert sorted(p.name.split("_")[0] for p in out.glob("*.duckdb")) == ["discord", "edgar", "prices", "raw"]
    assert sorted(p.name.split("_")[0] for p in (out / "weekly").glob("*")) == ["raw"]


def test_a_failed_history_backup_is_logged_and_the_rest_of_the_night_still_runs(
        rawstore_tmp_db, tmp_path, clock, capsys, monkeypatch):
    rawstore.write_rows("watchlist", [{"ticker": "NOK"}])
    _write_history()
    out, mirror = tmp_path / "out", tmp_path / "drive"
    mirror.mkdir()
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", str(mirror))

    def boom(*a, **k):
        raise bs.BackupError("disk full")

    monkeypatch.setattr(bs, "snapshot_history", boom)
    assert bs.main(["--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "history backup FAILED: disk full" in err and "skipped, 1 backup step(s) failed" in err
    assert _names(out, "raw_*.duckdb") and _names(out / "weekly", "raw_*.duckdb")
    assert list(mirror.iterdir()) == []


# --- off-machine copy -------------------------------------------------------------------------------------------

def test_the_off_machine_copy_carries_history_but_not_its_weekly_copies(rawstore_tmp_db, tmp_path, clock, monkeypatch):
    rawstore.write_rows("watchlist", [{"ticker": "NOK"}])
    _write_history()
    out, mirror = tmp_path / "out", tmp_path / "drive"
    mirror.mkdir()
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", str(mirror))
    assert bs.main(["--out", str(out)]) == 0
    drive = mirror / SUBDIR
    assert _names(drive, "history_*.duckdb") == _names(out, "history_*.duckdb") and _names(drive, "history_*.duckdb")
    assert _names(out / "weekly", "history_*.duckdb") and not (drive / "weekly").exists()     # #349: weekly stays local
    name = _names(drive, "history_*.duckdb")[-1]
    assert bs._table_fingerprints(drive / name) == bs._table_fingerprints(out / name)


def test_the_off_machine_copy_keeps_the_newest_keep_mirror_history_copies(rawstore_tmp_db, tmp_path, clock, monkeypatch):
    rawstore.write_rows("watchlist", [{"ticker": "NOK"}])
    _write_history()
    out, mirror = tmp_path / "out", tmp_path / "drive"
    mirror.mkdir()
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", str(mirror))
    for i in range(5):
        clock(_day(2026, 9, 28) + timedelta(days=i))
        assert bs.main(["--out", str(out), "--keep-mirror", "2"]) == 0
    assert len(_names(mirror / SUBDIR, "history_*.duckdb")) == 2
    assert _names(mirror / SUBDIR, "history_*.duckdb") == _names(out, "history_*.duckdb")[-2:]


# --- restore ----------------------------------------------------------------------------------------------------

def test_restore_round_trip_for_history(rawstore_tmp_db, tmp_path):
    hist = _write_history(3)
    snap = bs.snapshot_history(out_dir=tmp_path)
    before = bs._table_fingerprints(hist)
    conn = duckdb.connect(str(hist))
    conn.execute("DELETE FROM hist_probe")
    conn.close()
    assert rs.restore(snap) == hist
    assert bs._table_fingerprints(hist) == before
    assert hist.with_name("history.duckdb.pre-restore").exists()


def test_restore_latest_history_and_list(rawstore_tmp_db, tmp_path, capsys):
    hist = _write_history(2)
    bs.snapshot(out_dir=tmp_path)
    old = bs.snapshot_history(out_dir=tmp_path)
    hist = _write_history(5)
    new = bs.snapshot_history(out_dir=tmp_path)
    assert rs.snapshots(tmp_path, "history") == [old, new] and old in rs.snapshots(tmp_path)
    assert rs.main(["--from", str(tmp_path), "--latest", "history"]) == 0
    assert bs._table_counts(hist) == {"hist_probe": 5}
    capsys.readouterr()
    assert rs.main(["--from", str(tmp_path), "--list"]) == 0
    assert new.name in capsys.readouterr().out


def test_restore_from_the_weekly_folder_reads_history_too(rawstore_tmp_db, tmp_path, clock):
    _write_history(4)
    out = tmp_path / "out"
    bs.keep_weekly(bs.snapshot_history(out_dir=out), out_dir=out)
    hist = rawstore.history_db_path()
    hist.unlink()
    assert rs.main(["--from", str(out / "weekly"), "--latest", "history"]) == 0
    assert bs._table_counts(hist) == {"hist_probe": 4}


def test_restore_still_refuses_a_name_that_is_not_a_store_snapshot(rawstore_tmp_db, tmp_path):
    f = tmp_path / "journal_20260928_020005_123456.duckdb"
    f.write_bytes(b"x")
    with pytest.raises(bs.BackupError, match="not a store snapshot"):
        rs.restore(f)


# --- Automations row --------------------------------------------------------------------------------------------

def test_the_backup_row_names_history_and_keeps_its_arguments():
    row = next(e for e in automations.REGISTRY if e.name == "RC-Store-Backup")
    assert "(raw, edgar, discord, history, prices)" in row.description and "history snapshots" in row.description
    assert row.args == ("-m", "RevenueChaser.scripts.backup_store", "--keep", "5")
