"""#275 (review B2): after a verified nightly backup the app copies the newest snapshot set and the file mirrors to a
second folder (``RAW_STORE_BACKUP_MIRROR``: a Google Drive for desktop folder or an external disk), verifies each file
there again and keeps the newest N. #349: the big kinds go there once a week, the weekly copies stay local. Temp
folders only."""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from RevenueChaser.core import config, rawstore
from RevenueChaser.module_spx_gamma import config as spx_config
from RevenueChaser.scripts import backup_store as bs
from RevenueChaser.scripts.tests.duckdb_damage import damage_data_block

SUBDIR = "RevenueChaser-backup"        # what the off-machine copy writes into, inside RAW_STORE_BACKUP_MIRROR
_FILES = {"raw/2026-10-01/gex_levels.json": b'{"data": 1}', "brier.json": b'{"records": []}'}


def _day(y, m, d, hh=2):
    return datetime(y, m, d, hh, 0, 5, 123456, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    state = {"now": _day(2026, 9, 28)}
    monkeypatch.setattr(bs, "_utcnow", lambda: state["now"])
    return lambda dt: state.update(now=dt)


@pytest.fixture
def world(rawstore_tmp_db, tmp_path, monkeypatch):
    """A seeded store with a history file, a backup folder, a mirror folder and an SPX tree, all under tmp_path."""
    out, mirror = tmp_path / "out", tmp_path / "drive"
    mirror.mkdir()
    spx = tmp_path / "spx"
    for rel, data in _FILES.items():
        _put(spx, rel, data)
    docs = tmp_path / "docs"
    _put(docs, "320193/0000320193-24-000001.html.gz", b"gz-bytes")
    monkeypatch.setattr(spx_config, "DATA_ROOT", spx)
    monkeypatch.setattr(config, "EDGAR_DOCS_DIR", docs)
    rawstore.write_rows("watchlist", [{"ticker": "NOK"}])
    _write_history()
    return {"out": out, "mirror": mirror, "dest": mirror / SUBDIR, "spx": spx, "docs": docs}


def _write_history(rows: int = 3) -> Path:
    """``history.duckdb`` with one hand-made table: the small mirror kind, as in ``test_history_backup``."""
    path = rawstore.history_db_path()
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


def _put(root: Path, rel: str, data: bytes, age_s: int = 3600) -> Path:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    t = time.time() - age_s
    os.utime(f, (t, t))
    return f


def _run(world, monkeypatch, *extra, mirror=True):
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", str(world["mirror"]) if mirror else "")
    return bs.main(["--out", str(world["out"]), *extra])


def _new_filing(world, tag) -> None:
    """An EDGAR sync stored one more document: the next run zips the tree again (#336 keeps an unchanged one)."""
    _put(world["docs"], f"320193/0000320193-26-{tag}.html.gz", b"gz-bytes")


def _names(folder: Path, pattern: str) -> list[str]:
    return sorted(p.name for p in folder.glob(pattern))


def test_an_unset_key_is_a_no_op(world, monkeypatch, capsys):
    assert _run(world, monkeypatch, mirror=False) == 0
    text = capsys.readouterr()
    assert "off-machine" not in text.out + text.err
    assert list(world["mirror"].iterdir()) == []


def test_the_newest_set_and_the_mirrors_reach_the_second_folder(world, monkeypatch, capsys, clock):
    assert _run(world, monkeypatch) == 0
    out, drive = world["out"], world["dest"]
    for pattern in ("raw_*.duckdb", "edgar_*.duckdb", "history_*.duckdb", "edgar_docs_*.zip"):
        assert _names(drive, pattern) == _names(out, pattern) and _names(drive, pattern), pattern
    raw = _names(out, "raw_*.duckdb")[-1]
    assert bs._table_fingerprints(drive / raw) == bs._table_fingerprints(out / raw)
    assert _names(out / "weekly", "raw_*.duckdb") and not (drive / "weekly").exists()     # #349: weekly stays local
    for rel, data in _FILES.items():
        assert (drive / "spx_gamma_mirror" / rel).read_bytes() == data
    assert _names(drive / "spx_gamma_versions", "brier.*.json")
    assert not [p for p in drive.rglob("*") if p.is_file() and p.suffix in (".tmp", ".part")]
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("off-machine copy OK "))
    assert line.startswith("off-machine copy OK 2026-09-28T02:00:05+00:00")
    assert str(drive) in line


def test_the_second_run_copies_nothing_it_already_has(world, monkeypatch, capsys, clock):
    assert _run(world, monkeypatch) == 0
    capsys.readouterr()
    clock(_day(2026, 9, 29))
    assert _run(world, monkeypatch) == 0                       # new daily snapshots are copied, the old files are not
    res = bs.offmachine_copy(world["out"], world["mirror"], keep=3)
    assert res.copied == 0 and res.present > 0 and res.dest == world["dest"]


def test_a_failed_copy_leaves_nothing_under_the_final_name_and_exits_red(world, monkeypatch, capsys, clock):
    real = shutil.copy2

    def half_then_die(src, dst, *a, **k):
        if Path(dst).parent == world["dest"] and Path(src).name.startswith("raw_"):
            Path(dst).write_bytes(b"half a fi")
            raise OSError(28, "No space left on device")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(bs.shutil, "copy2", half_then_die)
    assert _run(world, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "off-machine copy FAILED 2026-09-28T02:00:05" in err and "raw_" in err
    assert _names(world["dest"], "raw_*") == []                  # neither the final name nor a leftover .tmp
    assert _names(world["out"], "raw_*.duckdb")                    # the local backup is intact


def test_a_copy_that_does_not_verify_is_discarded(world, monkeypatch, capsys, clock):
    real = shutil.copy2

    def garbage(src, dst, *a, **k):
        if Path(dst).parent == world["dest"] and Path(src).name.startswith("raw_"):
            Path(dst).write_bytes(b"not a duckdb file")
            return dst
        return real(src, dst, *a, **k)

    monkeypatch.setattr(bs.shutil, "copy2", garbage)
    assert _run(world, monkeypatch) == 1
    assert "off-machine copy FAILED" in capsys.readouterr().err
    assert _names(world["dest"], "raw_*") == []


def test_a_zip_with_a_flipped_byte_is_discarded(world, monkeypatch, capsys, clock):
    real = shutil.copy2

    def flip(src, dst, *a, **k):
        real(src, dst, *a, **k)
        if Path(dst).parent == world["dest"] and Path(src).name.startswith("edgar_docs_"):
            data = bytearray(Path(dst).read_bytes())
            data[len(data) // 2] ^= 0xFF
            Path(dst).write_bytes(bytes(data))
        return dst

    monkeypatch.setattr(bs.shutil, "copy2", flip)
    assert _run(world, monkeypatch) == 1
    assert "off-machine copy FAILED" in capsys.readouterr().err
    assert _names(world["dest"], "edgar_docs_*") == []


def test_the_newest_n_copies_are_kept_per_kind(world, monkeypatch, clock):
    for i in range(4):                                         # four Mondays: the big kinds go every time
        clock(_day(2026, 9, 28) + timedelta(days=7 * i))
        rawstore.write_rows("watchlist", [{"ticker": f"T{i}"}])
        _new_filing(world, f"T{i}")
        assert _run(world, monkeypatch, "--keep-mirror", "2", "--keep-mirror-big", "2") == 0
    drive, out = world["dest"], world["out"]
    assert _names(drive, "raw_*.duckdb") == _names(out, "raw_*.duckdb")[-2:]
    assert len(_names(drive, "edgar_*.duckdb")) == 2 and len(_names(drive, "history_*.duckdb")) == 2
    assert len(_names(drive, "edgar_docs_*.zip")) == 2


def test_the_default_keeps_three_small_copies_and_sends_the_big_ones_once_in_five_days(world, monkeypatch, clock):
    for i in range(5):
        clock(_day(2026, 9, 28) + timedelta(days=i))
        assert _run(world, monkeypatch) == 0
    assert len(_names(world["dest"], "history_*.duckdb")) == 3
    assert len(_names(world["dest"], "raw_*.duckdb")) == 1


def test_pruning_never_runs_before_the_new_copy_is_verified(world, monkeypatch, clock):
    assert _run(world, monkeypatch, "--keep-mirror", "1", "--keep-mirror-big", "1") == 0
    before = _names(world["dest"], "raw_*.duckdb")
    clock(_day(2026, 10, 5))                                   # a week later: the raw copy is due again

    def verify_fails(src, copy):
        raise bs._VerificationError("boom")

    monkeypatch.setattr(bs, "_verify_copy", verify_fails)
    assert _run(world, monkeypatch, "--keep-mirror", "1", "--keep-mirror-big", "1") == 1
    assert _names(world["dest"], "raw_*.duckdb") == before   # the older good copy is still there


def test_keep_mirror_must_be_at_least_one(world, monkeypatch):
    with pytest.raises(SystemExit):
        _run(world, monkeypatch, "--keep-mirror", "0")


def test_a_folder_that_is_not_there_is_an_error_not_a_new_local_folder(world, monkeypatch, capsys, clock):
    world["mirror"] = world["mirror"] / "E_unplugged" / "backups"      # the disk is not mounted: its parent does not exist
    assert _run(world, monkeypatch) == 1
    assert "off-machine copy FAILED" in capsys.readouterr().err
    assert not world["mirror"].exists() and not world["mirror"].parent.exists()


@pytest.mark.parametrize("where", ["same", "inside", "around"])
def test_the_second_folder_must_be_a_different_folder(world, monkeypatch, capsys, clock, where):
    out = world["out"]
    out.mkdir(parents=True, exist_ok=True)
    world["mirror"] = {"same": out, "inside": out / "drive", "around": out.parent}[where]
    world["mirror"].mkdir(exist_ok=True)
    assert _run(world, monkeypatch) == 1
    assert "different folder" in capsys.readouterr().err


def test_a_leftover_temp_file_from_a_killed_run_is_removed(world, monkeypatch, clock):
    assert _run(world, monkeypatch) == 0
    raw = _names(world["out"], "raw_*.duckdb")[-1]
    (world["dest"] / (raw + ".tmp")).write_bytes(b"half")
    clock(_day(2026, 9, 29))
    assert _run(world, monkeypatch) == 0
    assert not list(world["dest"].glob("*.tmp"))


def test_a_mirror_folder_failure_does_not_touch_the_local_backup_result(world, monkeypatch, capsys, clock):
    world["mirror"].rmdir()
    world["mirror"].write_text("a file, not a folder")
    assert _run(world, monkeypatch) == 1
    assert _names(world["out"], "raw_*.duckdb") and "backup OK:" in capsys.readouterr().out


def test_the_copy_is_skipped_after_a_local_failure(world, monkeypatch, capsys, clock):
    def boom(*a, **k):
        raise bs.BackupError("disk full")

    monkeypatch.setattr(bs, "snapshot_history", boom)
    assert _run(world, monkeypatch) == 1
    assert "off-machine" not in capsys.readouterr().out
    assert list(world["mirror"].iterdir()) == []               # "after a verified backup run"


def _made(world) -> dict[str, bool]:
    """Which parts of the nightly backup exist in the backup folder."""
    out = world["out"]
    return {
        "raw": bool(_names(out, "raw_*.duckdb")),
        "weekly": bool(_names(out / "weekly", "raw_*.duckdb")),
        "edgar": bool(_names(out, "edgar_*.duckdb")),
        "edgar_docs": bool(_names(out, "edgar_docs_*.zip")),
        "history": bool(_names(out, "history_*.duckdb")),
        "spx": (out / "spx_gamma_mirror" / "brier.json").is_file(),
    }


# step -> (function the run calls, label of its FAILED line)
_STEPS = {
    "raw": ("snapshot", "backup"),
    "weekly": ("keep_weekly", "weekly backup"),
    "edgar": ("snapshot_edgar", "edgar backup"),
    "edgar_docs": ("archive_edgar_docs", "edgar_docs backup"),
    "history": ("snapshot_history", "history backup"),
    "spx": ("mirror_spx_gamma", "spx backup"),
}


def _boom(*a, **k):
    raise bs.BackupError("disk full")


@pytest.mark.parametrize("step", list(_STEPS))
def test_every_other_step_still_runs_after_one_fails_and_the_run_is_red(world, monkeypatch, capsys, clock, step):
    """Review M1: the run used to return at the first failure, so one failed step (a busy edgar store, a full
    disk for the weekly copy) also cost that night's SPX mirror."""
    func, label = _STEPS[step]
    monkeypatch.setattr(bs, func, _boom)
    assert _run(world, monkeypatch) == 1
    assert f"{label} FAILED: disk full" in capsys.readouterr().err
    expected = {k: True for k in _STEPS}
    expected[step] = False
    if step == "raw":
        expected["weekly"] = False                             # the weekly copy is cut from the raw snapshot
    assert _made(world) == expected
    assert list(world["mirror"].iterdir()) == []               # only the off-machine copy is skipped


def test_two_failed_steps_are_both_reported_and_the_rest_still_runs(world, monkeypatch, capsys, clock):
    monkeypatch.setattr(bs, "snapshot_edgar", _boom)
    monkeypatch.setattr(bs, "mirror_spx_gamma", _boom)
    assert _run(world, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "edgar backup FAILED: disk full" in err and "spx backup FAILED: disk full" in err
    assert _made(world) == {**{k: True for k in _STEPS}, "edgar": False, "spx": False}


def test_a_night_with_a_failed_step_logs_the_skipped_copy_so_the_row_turns_red_that_night(
        world, monkeypatch, capsys, clock, tmp_path):
    """Review L3: with no line the Automations row kept showing the last good copy for up to 48 h."""
    from RevenueChaser.core import automations
    monkeypatch.setattr(bs, "snapshot_history", _boom)
    assert _run(world, monkeypatch) == 1
    err = capsys.readouterr().err
    line = next(ln for ln in err.splitlines() if ln.startswith("off-machine copy FAILED "))
    assert line.startswith("off-machine copy FAILED 2026-09-28T02:00:05+00:00: skipped")
    log = tmp_path / "task.log"
    log.write_text("off-machine copy OK 2026-09-27T02:00:05+00:00: 9 copied, 0 already there -> x\n" + line + "\n",
                   encoding="utf-8")
    assert automations.last_offmachine(log) == ("2026-09-27T02:00:05+00:00", "2026-09-28T02:00:05+00:00", False)
    assert list(world["mirror"].iterdir()) == []


def test_no_skipped_line_when_no_folder_is_set(world, monkeypatch, capsys, clock):
    monkeypatch.setattr(bs, "snapshot_history", _boom)
    assert _run(world, monkeypatch, mirror=False) == 1
    assert "off-machine" not in capsys.readouterr().err


def _bulk_rows(n: int = 3000) -> None:
    rawstore.write_rows("ticker_payload_cache", [
        {"ticker": f"T{i}", "payload": {"x": "y" * 40}, "fetched_at": 1.0, "fetched_at_iso": ""} for i in range(n)])


def test_a_weekly_copy_with_a_damaged_data_block_fails_that_step_only(world, monkeypatch, capsys, clock):
    """Review M3: ``duckdb.IOException`` from the weekly copy's verify used to end the night with a traceback,
    skip every later step and print no line, so the off-machine row stayed green."""
    _bulk_rows()
    real = shutil.copy2

    def damaging(src, dst, *a, **k):
        real(src, dst, *a, **k)
        if Path(dst).parent.name == "weekly":
            damage_data_block(dst)
        return dst

    monkeypatch.setattr(bs.shutil, "copy2", damaging)
    assert _run(world, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "weekly backup FAILED: could not read" in err and "Traceback" not in err
    assert "off-machine copy FAILED 2026-09-28T02:00:05+00:00: skipped" in err
    assert list((world["out"] / "weekly").glob("*")) == []     # neither the final name nor a leftover .tmp
    assert _made(world) == {**{k: True for k in _STEPS}, "weekly": False}
    assert list(world["mirror"].iterdir()) == []


def test_an_off_machine_copy_with_a_damaged_data_block_is_reported_and_leaves_no_temp_file(
        world, monkeypatch, capsys, clock):
    """Review M3: the third place the connect-only wrapping did not reach: the verify of the second-folder copy."""
    _bulk_rows()
    real = shutil.copy2

    def damaging(src, dst, *a, **k):
        real(src, dst, *a, **k)
        if Path(dst).parent == world["dest"] and Path(src).name.startswith("raw_"):
            damage_data_block(dst)
        return dst

    monkeypatch.setattr(bs.shutil, "copy2", damaging)
    assert _run(world, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "off-machine copy FAILED 2026-09-28T02:00:05+00:00: could not read" in err and "Traceback" not in err
    assert _names(world["dest"], "raw_*") == []                # neither the final name nor a leftover .tmp
    assert _names(world["out"], "raw_*.duckdb")                # the local backup is intact


@pytest.mark.parametrize("step", list(_STEPS))
def test_a_step_that_raises_something_other_than_a_backup_error_does_not_end_the_night(
        world, monkeypatch, capsys, clock, step):
    """Review L4: an ``OSError`` outside the spots that wrap it (``make_archive`` on a full disk, a prune that
    meets a file another process holds open) used to abort the run before the later steps."""
    func, label = _STEPS[step]

    def disk_full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(bs, func, disk_full)
    assert _run(world, monkeypatch) == 1
    err = capsys.readouterr().err
    assert f"{label} FAILED: OSError: [Errno 28] No space left on device" in err
    assert "Traceback" in err                                  # an unexpected error keeps its stack for the log
    expected = {k: True for k in _STEPS}
    expected[step] = False
    if step == "raw":
        expected["weekly"] = False
    assert _made(world) == expected
    assert "off-machine copy FAILED 2026-09-28T02:00:05+00:00: skipped" in err


def test_a_backup_error_keeps_its_plain_message_without_a_traceback(world, monkeypatch, capsys, clock):
    monkeypatch.setattr(bs, "snapshot_edgar", _boom)
    assert _run(world, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "edgar backup FAILED: disk full" in err and "Traceback" not in err and "BackupError" not in err


def test_an_unexpected_error_in_the_off_machine_copy_is_reported_and_the_run_is_red(world, monkeypatch, capsys, clock):
    def denied(*a, **k):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(bs, "offmachine_copy", denied)
    assert _run(world, monkeypatch) == 1
    assert "off-machine copy FAILED 2026-09-28T02:00:05+00:00: PermissionError: [Errno 13] Permission denied" in capsys.readouterr().err
    assert all(_made(world).values())                          # everything local was made


def test_a_full_disk_while_zipping_the_filings_leaves_no_partial_archive(world, monkeypatch, capsys, clock):
    import zipfile

    def full(self, *a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(zipfile.ZipFile, "write", full)
    assert _run(world, monkeypatch) == 1
    assert "edgar_docs backup FAILED" in capsys.readouterr().err
    assert list(world["out"].glob("edgar_docs_*")) == []       # the half-written .partial.zip is removed
    expected = {k: True for k in _STEPS}
    expected["edgar_docs"] = False
    assert _made(world) == expected


def test_a_rejected_spx_file_still_lets_the_off_machine_copy_run(world, monkeypatch, capsys, clock):
    assert _run(world, monkeypatch) == 0
    _put(world["spx"], "brier.json", b'{"records": [{"da', age_s=1800)
    clock(_day(2026, 9, 29))
    assert _run(world, monkeypatch) == 1                       # red because of the damaged source...
    cap = capsys.readouterr()
    assert "off-machine copy OK" in cap.out                    # ...but the off-machine copy still ran
    assert (world["dest"] / "spx_gamma_mirror" / "brier.json").read_bytes() == _FILES["brier.json"]


def test_main_reports_a_file_the_second_folder_kept_and_exits_red(world, monkeypatch, capsys, clock):
    """``offmachine_copy`` can keep an older copy there (damaged local source): main prints it and the run is red."""
    real = bs.offmachine_copy

    def with_a_rejection(out_dir, mirror, **kw):
        res = real(out_dir, mirror, **kw)
        return bs.OffMachineResult(res.copied, res.present, ("spx_gamma_mirror/brier.json",), res.dest)

    monkeypatch.setattr(bs, "offmachine_copy", with_a_rejection)
    assert _run(world, monkeypatch) == 1
    cap = capsys.readouterr()
    assert "off-machine copy OK" in cap.out
    assert "off-machine copy WARNING" in cap.err and "spx_gamma_mirror/brier.json" in cap.err


def test_the_spx_guard_also_holds_on_the_second_folder(world, monkeypatch, clock):
    assert _run(world, monkeypatch) == 0
    local = world["out"] / "spx_gamma_mirror" / "brier.json"
    local.write_bytes(b'{"records": [{"da')                    # the local mirror copy itself gets damaged
    os.utime(local, (time.time() - 100, time.time() - 100))
    res = bs.offmachine_copy(world["out"], world["mirror"], keep=3)
    assert res.rejected == ("spx_gamma_mirror/brier.json",)
    assert (world["dest"] / "spx_gamma_mirror" / "brier.json").read_bytes() == _FILES["brier.json"]


def test_the_sub_folder_name_is_the_one_the_docs_and_the_row_use():
    assert bs.OFFMACHINE_SUBDIR == SUBDIR


_FOREIGN = {
    "raw_20250101_020000_000001.duckdb": b"a schema checkpoint avo saved by hand",
    "raw_20250102_020000_000001.duckdb": b"another one",
    "raw_20250103_020000_000001.duckdb": b"and another",
    "raw_20250104_020000_000001.duckdb": b"and a fourth",
    "edgar_20250101_020000_000001.duckdb": b"an old edgar copy",
    "edgar_docs_20250101_020000_000001.zip": b"an old zip",
    "notes.tmp": b"a file that only looks like a leftover",
    "weekly/raw_20250101_020000_000001.duckdb": b"his own weekly folder",
    "weekly/draft.tmp": b"another lookalike",
    # The mirror a pre-October-2026 run wrote for the Discord attachments: nothing prunes or deletes it now.
    "discord_media_mirror/keep.me": b"a folder an earlier version of this job wrote",
}


def test_an_existing_drive_folder_keeps_every_file_that_is_not_ours(world, monkeypatch, capsys, clock):
    """Review M2: RAW_STORE_BACKUP_MIRROR may be an existing Drive folder. Pruning to the newest N and deleting
    ``*.tmp`` used to run in that folder itself and ate files the app never wrote."""
    drive = world["mirror"]
    for rel, data in _FOREIGN.items():
        (drive / rel).parent.mkdir(parents=True, exist_ok=True)
        (drive / rel).write_bytes(data)
    for day in range(4):                                       # four nights, keep 1: every run prunes
        clock(_day(2026, 9, 28) + timedelta(days=day))
        assert _run(world, monkeypatch, "--keep-mirror", "1", "--keep-mirror-big", "1",
                    "--mirror-big-every-days", "0") == 0
    for rel, data in _FOREIGN.items():
        assert (drive / rel).read_bytes() == data, f"{rel} was changed or deleted"
    assert sorted(p.name for p in drive.iterdir() if p.is_file()) == sorted(n for n in _FOREIGN if "/" not in n)
    assert len(_names(world["dest"], "raw_*.duckdb")) == 1     # the pruning did run, inside the sub-folder


def test_the_retired_discord_leftovers_are_never_pruned_in_either_folder(world, monkeypatch, capsys, clock):
    """#174/#175: the ``discord_*`` snapshots and ``discord_media_mirror/`` an earlier run wrote, in the backup
    folder and in the off-machine sub-folder, stay until the owner deletes them by hand: no prune touches them."""
    assert _run(world, monkeypatch) == 0
    out, dest = world["out"], world["dest"]
    (raw_snap,) = _names(out, "raw_*.duckdb")
    leftovers = {}
    for folder, count in ((out, 4), (dest, 3)):                # more than any --keep below, so a prune would bite
        for i in range(count):
            p = folder / f"discord_2025010{i + 1}_020000_000001.duckdb"
            shutil.copy2(out / raw_snap, p)                   # a real DuckDB file, so the local scan adopts it
            leftovers[p] = p.read_bytes()
        media = folder / "discord_media_mirror" / "900" / "a.png"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"an attachment an earlier version of this job mirrored")
        leftovers[media] = media.read_bytes()
    for day in range(1, 5):
        clock(_day(2026, 9, 28) + timedelta(days=day))
        assert _run(world, monkeypatch, "--keep", "1", "--keep-mirror", "1", "--keep-mirror-big", "1",
                    "--mirror-big-every-days", "0", "--scan-every-days", "0") == 0, capsys.readouterr().err
    for p, data in leftovers.items():
        assert p.is_file() and p.read_bytes() == data, f"{p} was changed or deleted"
    assert len(_names(out, "raw_*.duckdb")) == 1 and len(_names(dest, "raw_*.duckdb")) == 1  # the prunes did run


def test_everything_the_app_writes_goes_into_the_sub_folder(world, monkeypatch, capsys, clock):
    assert _run(world, monkeypatch) == 0
    drive = world["mirror"]
    assert [p.name for p in drive.iterdir()] == [SUBDIR]
    assert _names(world["dest"], "raw_*.duckdb") and _names(world["dest"], "history_*.duckdb")
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("off-machine copy OK "))
    assert line.endswith(f"-> {world['dest']}")


def test_a_leftover_temp_file_in_the_drive_folder_itself_is_not_ours_to_remove(world, monkeypatch, clock):
    (world["mirror"] / "half.tmp").write_bytes(b"x")
    assert _run(world, monkeypatch) == 0
    assert (world["mirror"] / "half.tmp").is_file()


def test_a_file_where_the_sub_folder_should_be_is_an_error_and_nothing_else_is_touched(world, monkeypatch, capsys, clock):
    world["dest"].write_bytes(b"not a folder")
    (world["mirror"] / "mine.txt").write_bytes(b"mine")
    assert _run(world, monkeypatch) == 1
    assert "off-machine copy FAILED" in capsys.readouterr().err
    assert sorted(p.name for p in world["mirror"].iterdir()) == sorted(["mine.txt", SUBDIR])
    assert world["dest"].read_bytes() == b"not a folder"


def test_the_unit_suite_forces_the_backup_switches_to_their_defaults():
    """Run by the test below with hostile values in the environment. ``load_dotenv`` never overrides a variable
    that is already set, so the root conftest sets both before anything imports ``core.config``."""
    assert config.RAW_STORE_BACKUP_MIRROR == ""
    assert config.RAW_STORE_BACKUP_WEEKLY == "1"


def test_a_real_off_machine_folder_or_weekly_switch_in_the_environment_cannot_reach_the_unit_suite():
    """Once avo sets RAW_STORE_BACKUP_MIRROR in .env, every test that calls ``backup_store.main`` would otherwise
    copy a test store into his real Drive folder, and RAW_STORE_BACKUP_WEEKLY=0 would break the weekly tests."""
    import subprocess
    import sys
    env = dict(os.environ, RAW_STORE_BACKUP_MIRROR=str(Path(__file__).parent / "never-written"),
               RAW_STORE_BACKUP_WEEKLY="0")
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"{__file__}::test_the_unit_suite_forces_the_backup_switches_to_their_defaults"],
        env=env, capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stdout[-800:] + res.stderr[-400:]
    assert not (Path(__file__).parent / "never-written").exists()


def test_restore_reads_the_second_folder(world, monkeypatch, clock):
    """The Backups section tells avo to restore with ``--from <RAW_STORE_BACKUP_MIRROR folder>/RevenueChaser-backup``.
    The weekly copies stay on this machine since #349 (``--from <backup dir>/weekly``, covered by the weekly tests)."""
    from RevenueChaser.scripts import restore_store as rs
    assert _run(world, monkeypatch) == 0
    rawstore.write_rows("watchlist", [{"ticker": "LATER"}])
    assert rs.main(["--from", str(world["dest"]), "--latest", "raw"]) == 0
    assert rawstore.count("watchlist") == 1


# --- #349: the big files go to the second folder once a week ----------------------------------------------------
# A Drive for desktop folder moves every pruned copy into the Drive trash, which counts toward the Google quota
# for 30 days: a daily 1.9 GB set held about 60 GB of avo's 200 GB account, and it filled up on 2026-10-08.

_BIG = ("raw_*.duckdb", "edgar_*.duckdb", "edgar_docs_*.zip")


def _nights(world, monkeypatch, clock, days, *extra, hh=2):
    """One backup run on each of ``days`` (days after Monday 2026-09-28), each after a store write and a new filing."""
    for d in days:
        clock(_day(2026, 9, 28, hh) + timedelta(days=d))
        rawstore.write_rows("watchlist", [{"ticker": f"D{d}"}])
        _new_filing(world, f"D{d}")
        assert _run(world, monkeypatch, *extra) == 0


def test_a_run_within_a_week_sends_the_small_files_but_no_big_file(world, monkeypatch, capsys, clock):
    _nights(world, monkeypatch, clock, [0])
    first = {p: _names(world["dest"], p) for p in _BIG}
    assert all(first.values())
    _nights(world, monkeypatch, clock, [1, 6])
    for p in _BIG:
        assert _names(world["dest"], p) == first[p], p
    drive_history = _names(world["dest"], "history_*.duckdb")
    assert len(drive_history) == 3 and drive_history[-1] == _names(world["out"], "history_*.duckdb")[-1]
    line = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("off-machine copy OK ")][-1]
    assert "big files skipped until 2026-10-05" in line and line.endswith(f"-> {world['dest']}")


def test_a_run_a_week_after_the_newest_big_copy_sends_them_again(world, monkeypatch, capsys, clock):
    _nights(world, monkeypatch, clock, [0, 7])
    for p in _BIG:
        assert len(_names(world["dest"], p)) == 2, p
    assert _names(world["dest"], "raw_*.duckdb")[-1] == _names(world["out"], "raw_*.duckdb")[-1]
    line = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("off-machine copy OK ")][-1]
    assert "skipped" not in line


def test_the_week_is_counted_in_days_so_an_earlier_hour_a_week_later_still_sends_them(world, monkeypatch, clock):
    _nights(world, monkeypatch, clock, [0], hh=23)            # a manual run late in the evening
    _nights(world, monkeypatch, clock, [7], hh=2)             # the 04:00 task a week later, earlier in the day
    for p in _BIG:
        assert len(_names(world["dest"], p)) == 2, p


def test_big_kinds_keep_two_and_small_kinds_keep_three(world, monkeypatch, clock):
    _nights(world, monkeypatch, clock, [0, 1, 7, 14])
    for p in _BIG:
        assert len(_names(world["dest"], p)) == 2, p
    assert _names(world["dest"], "raw_*.duckdb")[-1] == _names(world["out"], "raw_*.duckdb")[-1]
    assert len(_names(world["dest"], "history_*.duckdb")) == 3


def test_keep_mirror_big_sets_how_many_big_copies_stay(world, monkeypatch, clock):
    _nights(world, monkeypatch, clock, [0, 7, 14, 21], "--keep-mirror-big", "3")
    for p in _BIG:
        assert len(_names(world["dest"], p)) == 3, p


def test_every_days_zero_sends_the_big_files_on_every_run(world, monkeypatch, clock):
    _nights(world, monkeypatch, clock, [0, 1], "--mirror-big-every-days", "0")
    for p in _BIG:
        assert len(_names(world["dest"], p)) == 2, p


def test_big_now_sends_them_inside_the_week(world, monkeypatch, clock):
    _nights(world, monkeypatch, clock, [0])
    _nights(world, monkeypatch, clock, [2], "--mirror-big-now")
    for p in _BIG:
        assert len(_names(world["dest"], p)) == 2, p


@pytest.mark.parametrize("flag,value", [("--keep-mirror-big", "0"), ("--mirror-big-every-days", "-1")])
def test_the_big_file_switches_refuse_values_that_make_no_sense(world, monkeypatch, capsys, flag, value):
    with pytest.raises(SystemExit):
        _run(world, monkeypatch, flag, value)
    assert "must be" in capsys.readouterr().err               # refused for its value, not as an unknown flag


def test_the_weekly_copies_stay_on_this_machine_and_old_ones_on_the_second_folder_are_kept(world, monkeypatch, clock):
    old = world["dest"] / "weekly" / "raw_20260901_020000_000001.duckdb"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"a weekly copy an older version sent")
    _nights(world, monkeypatch, clock, [0, 7])
    assert len(_names(world["out"] / "weekly", "raw_*.duckdb")) == 2      # the local weekly copies are still made
    assert _names(world["dest"] / "weekly", "*") == [old.name]
    assert old.read_bytes() == b"a weekly copy an older version sent"


def test_a_big_copy_without_a_stamp_in_its_name_is_aged_by_its_modified_time(world, monkeypatch, clock):
    hand = [world["dest"] / p.replace("*", "by_hand") for p in _BIG]    # one unstamped copy of each big kind
    world["dest"].mkdir()
    for f in hand:
        f.write_bytes(b"x")
        t = _day(2026, 9, 28).timestamp()                     # made today
        os.utime(f, (t, t))
    assert _run(world, monkeypatch) == 0
    assert _names(world["dest"], "raw_2*.duckdb") == []       # the week has not passed: no big file is sent
    for f in hand:
        t = _day(2026, 9, 20).timestamp()                     # eight days old
        os.utime(f, (t, t))
    assert _run(world, monkeypatch) == 0
    for p in _BIG:
        assert _names(world["dest"], p.replace("*", "2*")), p


def test_the_skipped_line_still_reads_as_a_good_copy_on_the_automations_row(world, monkeypatch, capsys, clock, tmp_path):
    from RevenueChaser.core import automations
    _nights(world, monkeypatch, clock, [0, 1])
    line = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("off-machine copy OK ")][-1]
    assert "big files skipped until" in line
    log = tmp_path / "task.log"
    log.write_text(line + "\n", encoding="utf-8")
    assert automations.last_offmachine(log) == ("2026-09-29T02:00:05+00:00", "2026-09-29T02:00:05+00:00", True)
