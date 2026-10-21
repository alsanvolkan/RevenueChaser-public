"""#277: one verified raw snapshot per ISO week is kept in ``weekly/``; #342 bounds it at ``KEEP_WEEKLY`` (12) and
drops history's weekly copy (the owner's decision of 2026-10-21: history grows ~6.55 GB/yr against raw's 603 MB).
Temp stores only; the clock is driven through ``backup_store._utcnow``."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from RevenueChaser.core import config, rawstore
from RevenueChaser.scripts import backup_store as bs
from RevenueChaser.scripts import restore_store as rs


def _day(y, m, d, hh=2):
    return datetime(y, m, d, hh, 0, 5, 123456, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    """``clock(datetime)`` fixes what ``backup_store`` thinks "now" is."""
    state = {"now": _day(2026, 9, 28)}
    monkeypatch.setattr(bs, "_utcnow", lambda: state["now"])
    return lambda dt: state.update(now=dt)


def _seed(n: int):
    rawstore.write_rows("watchlist", [{"ticker": f"T{n}"}])


def _run(out: Path, clock, when: datetime, n: int, keep: int = 3):
    """One nightly run at ``when``: write a row, take the daily snapshot, then the weekly copy."""
    clock(when)
    _seed(n)
    daily = bs.snapshot(out_dir=out, keep=keep)
    weekly = bs.keep_weekly(daily, out_dir=out)
    return daily, weekly


def test_the_first_snapshot_of_a_week_is_copied_to_weekly_and_the_rest_are_not(rawstore_tmp_db, tmp_path, clock):
    out = tmp_path / "out"
    d1, w1 = _run(out, clock, _day(2026, 9, 28), 1)          # Monday
    d2, w2 = _run(out, clock, _day(2026, 9, 29), 2)          # Tuesday, same ISO week
    assert w1 == out / "weekly" / d1.name and w1.is_file()
    assert w2 is None
    assert sorted(p.name for p in (out / "weekly").glob("*")) == [d1.name]
    assert bs._table_fingerprints(w1) == bs._table_fingerprints(d1)      # the weekly copy IS the first snapshot


def test_several_weeks_keep_the_newest_dailies_plus_exactly_one_weekly_each(rawstore_tmp_db, tmp_path, clock):
    out = tmp_path / "out"
    firsts, n = [], 0
    for week in range(5):                                    # Mondays 2026-09-07 .. 2026-10-05
        for dow in range(7):
            when = _day(2026, 9, 7) + timedelta(days=7 * week + dow)
            daily, weekly = _run(out, clock, when, n)
            n += 1
            if dow == 0:
                firsts.append(daily.name)
                assert weekly is not None
            else:
                assert weekly is None
    weeklies = sorted(p.name for p in (out / "weekly").glob("raw_*.duckdb"))
    assert weeklies == sorted(firsts) and len(weeklies) == 5
    dailies = sorted(p.name for p in out.glob("raw_*.duckdb"))
    assert len(dailies) == 3                                 # keep=3: only the newest three
    assert not set(dailies) & set(firsts[:4])                # older weeks' firsts are gone from the top folder, kept in weekly/


def test_a_weekly_copy_is_never_deleted_by_prune(rawstore_tmp_db, tmp_path, clock):
    """The top-folder keep counts are scoped to the top folder: no ``_prune(out, ...)`` reaches into ``weekly/``."""
    out = tmp_path / "out"
    d1, w1 = _run(out, clock, _day(2026, 9, 28), 1)
    _run(out, clock, _day(2026, 10, 5), 2)
    assert bs._prune(out, "raw", 1) == [d1]                  # keep=1 prunes the old daily...
    assert not d1.exists() and w1.is_file()                  # ...and only that
    assert bs._prune(out, "raw", 0) != []                    # even "prune everything" stays in the top folder
    assert w1.is_file()


def test_the_week_is_the_iso_week_of_the_snapshot_not_a_rolling_seven_days(rawstore_tmp_db, tmp_path, clock):
    out = tmp_path / "out"
    _, sunday = _run(out, clock, _day(2026, 10, 4), 1)       # Sunday, ISO week 40
    _, monday = _run(out, clock, _day(2026, 10, 5), 2)       # Monday, ISO week 41: one day later, a new week
    assert sunday is not None and monday is not None
    assert len(list((out / "weekly").glob("raw_*.duckdb"))) == 2


def test_the_week_key_carries_the_iso_year(rawstore_tmp_db, tmp_path, clock):
    out = tmp_path / "out"
    _, a = _run(out, clock, _day(2025, 10, 1), 1)            # 2025-W40
    _, b = _run(out, clock, _day(2026, 10, 1), 2)            # 2026-W40: same week number, other year
    assert a is not None and b is not None
    # ISO year, not calendar year: 2026-12-31 and 2027-01-01 are both in ISO week 2026-W53
    _, c = _run(out, clock, _day(2026, 12, 31), 3)
    _, d = _run(out, clock, _day(2027, 1, 1), 4)
    assert c is not None and d is None
    assert len(list((out / "weekly").glob("raw_*.duckdb"))) == 3


def test_a_week_with_no_monday_run_is_kept_from_its_first_good_snapshot(rawstore_tmp_db, tmp_path, clock):
    out = tmp_path / "out"
    d, w = _run(out, clock, _day(2026, 9, 29), 1)            # Tuesday: the Monday run never produced a snapshot
    assert w is not None and w.name == d.name
    _, later = _run(out, clock, _day(2026, 9, 30), 2)
    assert later is None


def test_weekly_copy_is_verified_and_a_corrupt_copy_leaves_nothing_under_the_final_name(rawstore_tmp_db, tmp_path, clock, monkeypatch):
    out = tmp_path / "out"
    clock(_day(2026, 9, 28))
    _seed(1)
    daily = bs.snapshot(out_dir=out, keep=3)

    def bad_copy(src, dst):
        Path(dst).write_bytes(b"not a duckdb file")

    monkeypatch.setattr(bs.shutil, "copy2", bad_copy)
    with pytest.raises(bs.BackupError):
        bs.keep_weekly(daily, out_dir=out)
    assert list((out / "weekly").glob("*")) == []            # neither the final name nor a leftover temp file
    assert daily.is_file()


def test_weekly_can_be_switched_off_in_the_function(rawstore_tmp_db, tmp_path, clock):
    out = tmp_path / "out"
    clock(_day(2026, 9, 28))
    _seed(1)
    daily = bs.snapshot(out_dir=out, keep=3)
    assert bs.keep_weekly(daily, out_dir=out, enabled=False) is None
    assert not (out / "weekly").exists()


def test_main_writes_the_weekly_copy_and_logs_it(rawstore_tmp_db, tmp_path, clock, capsys):
    out = tmp_path / "out"
    clock(_day(2026, 9, 28))
    _seed(1)
    assert bs.main(["--out", str(out)]) == 0
    text = capsys.readouterr().out
    weekly = list((out / "weekly").glob("raw_*.duckdb"))
    assert len(weekly) == 1 and f"weekly backup OK: {weekly[0]}" in text
    clock(_day(2026, 9, 29))
    assert bs.main(["--out", str(out)]) == 0
    assert "weekly backup OK" not in capsys.readouterr().out
    assert len(list((out / "weekly").glob("raw_*.duckdb"))) == 1


def test_main_no_weekly_flag_turns_the_weekly_copy_off(rawstore_tmp_db, tmp_path, clock, capsys):
    out = tmp_path / "out"
    clock(_day(2026, 9, 28))
    _seed(1)
    assert bs.main(["--out", str(out), "--no-weekly"]) == 0
    assert "weekly backup" not in capsys.readouterr().out
    assert not (out / "weekly").exists()


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_main_config_turns_the_weekly_copy_off(rawstore_tmp_db, tmp_path, clock, monkeypatch, value):
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_WEEKLY", value)
    out = tmp_path / "out"
    clock(_day(2026, 9, 28))
    _seed(1)
    assert bs.main(["--out", str(out)]) == 0
    assert not (out / "weekly").exists()


def test_main_reports_a_weekly_copy_failure(rawstore_tmp_db, tmp_path, clock, monkeypatch, capsys):
    def boom(*a, **k):
        raise bs.BackupError("disk full")

    monkeypatch.setattr(bs, "keep_weekly", boom)
    clock(_day(2026, 9, 28))
    _seed(1)
    out = tmp_path / "out"
    assert bs.main(["--out", str(out)]) == 1
    assert "weekly backup FAILED: disk full" in capsys.readouterr().err
    assert list(out.glob("raw_*.duckdb"))                    # the daily snapshot was already written


def test_restore_ignores_weekly_copies_by_default_and_reads_them_through_from(rawstore_tmp_db, tmp_path, clock):
    out = tmp_path / "out"
    daily, weekly = _run(out, clock, _day(2026, 9, 28), 1)
    assert rs.snapshots(out, "raw") == [daily]               # --latest raw never picks a weekly copy by accident
    assert rs.snapshots(out / "weekly", "raw") == [weekly]
    _seed(99)
    assert rs.main(["--from", str(out / "weekly"), "--latest", "raw"]) == 0
    assert rawstore.count("watchlist") == 1                  # restored to the weekly state


def test_the_nightly_task_text_says_what_is_kept():
    from RevenueChaser.core import automations
    desc = automations.entry("RC-Store-Backup").description
    assert "keeps 5" in desc and "week" in desc


def test_register_tasks_comment_matches_the_retention():
    ps1 = (Path(__file__).resolve().parents[2] / "register_tasks.ps1").read_text(encoding="utf-8")
    assert "keeps the 5 newest raw snapshots" in ps1 and "one per week" in ps1


# ── #342: the weekly raw copies are bounded, and history's weekly copy is gone ────────────────────
# The owner decided on 2026-10-21: history's weekly copy goes away (it grows ~6.55 GB/yr against raw's 603 MB, and
# its switch could not be turned off at all), and the raw weekly copies are kept at KEEP_WEEKLY = 12.

def _thirteen_weeks(out: Path, clock) -> list[Path]:
    """Thirteen nights' raw snapshots, one per ISO week (2026-01-05 .. 2026-03-30), each with its weekly copy."""
    _seed(1)
    made = []
    for week in range(13):
        clock(_day(2026, 1, 5) + timedelta(weeks=week))
        made.append(bs.snapshot(out_dir=out, keep=20))
        assert bs.keep_weekly(made[-1], out_dir=out) is not None, week
    return made


def test_the_raw_weekly_copies_keep_the_newest_twelve(rawstore_tmp_db, tmp_path, clock):
    """W2: a thirteenth weekly copy is followed by a night that adds none — that night still prunes, to 12."""
    out = tmp_path / "out"
    made = _thirteen_weeks(out, clock)
    assert len(list((out / "weekly").glob("raw_*.duckdb"))) == 13
    clock(_day(2026, 3, 31))                                  # later in the same ISO week: no new weekly copy
    assert bs.main(["--out", str(out), "--keep", "20"]) == 0
    kept = sorted(p.name for p in (out / "weekly").glob("raw_*.duckdb"))
    assert len(kept) == 12                                    # the owner's number, not whatever the code says
    assert kept == sorted(p.name for p in made[-12:])
    assert not (out / "weekly" / made[0].name).exists()


def test_the_two_prunes_stay_in_their_own_folders(rawstore_tmp_db, tmp_path, clock):
    """W3: the top-folder keep counts never see ``weekly/``, and the weekly prune never sees the top folder."""
    out = tmp_path / "out"
    made = _thirteen_weeks(out, clock)
    weekly = sorted(p.name for p in (out / "weekly").glob("raw_*.duckdb"))
    assert bs._prune(out, "raw", 1) == made[:-1]              # the nightly top-folder prune...
    assert sorted(p.name for p in (out / "weekly").glob("raw_*.duckdb")) == weekly      # ...leaves weekly/ alone
    removed = bs._prune_weekly(out, bs.KEEP_WEEKLY)
    assert [p.name for p in removed] == [made[0].name]        # and the weekly prune leaves the top folder alone
    assert made[-1].is_file()
    assert len(list((out / "weekly").glob("raw_*.duckdb"))) == bs.KEEP_WEEKLY          # 12 stay


def test_a_pruned_weekly_copy_leaves_the_manifest_so_the_scan_stays_green(rawstore_tmp_db, tmp_path, clock):
    """W4: the removed copy leaves the #309 manifest, so the integrity scan does not report it missing."""
    out = tmp_path / "out"
    made = _thirteen_weeks(out, clock)
    key = f"weekly/{made[0].name}"
    assert key in bs._local_manifest(out).files
    bs._prune_weekly(out, bs.KEEP_WEEKLY)
    assert not (out / "weekly" / made[0].name).exists()
    assert key not in bs._local_manifest(out).files
    scan = bs.integrity_scan(out, every_days=7, now=True)
    assert scan.ran and not scan.failures


def test_restore_reads_the_newest_weekly_copy_after_the_prune(rawstore_tmp_db, tmp_path, clock):
    """W5: bounding the weekly copies keeps ``restore_store --from <dir>/weekly --latest raw`` working."""
    out = tmp_path / "out"
    made = _thirteen_weeks(out, clock)
    clock(_day(2026, 3, 31))
    assert bs.main(["--out", str(out), "--keep", "20"]) == 0
    weekly = rs.snapshots(out / "weekly", "raw")
    assert weekly and weekly[-1].name == made[-1].name
    assert len(weekly) == 12                                             # bounded, and still what --latest reads
    _seed(2)                                                  # the live store moves on
    assert rs.main(["--from", str(out / "weekly"), "--latest", "raw"]) == 0
    assert [r["ticker"] for r in rawstore.query("SELECT ticker FROM watchlist")] == ["T1"]

