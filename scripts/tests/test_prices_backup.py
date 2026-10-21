"""#156 (avo, 2026-10-09): ``.store/prices.duckdb`` is a further store file for the nightly backup, with its own rule.

The price tables are append-only (a bar is never rewritten), so the newest verified snapshot holds every row any
older one held: two nightly copies stay, there is no weekly copy (raw's weekly copies are kept forever, #277, which
at ~2.4 GiB a copy would grow by ~123 GiB a year). Off this machine (review 2026-10-09): prices goes last, so a failed
upload of the biggest file costs no other copy, once a week, and one copy stays there (avo's card, 12:06Z 2026-10-09:
``MIRROR_PRICES_EVERY_DAYS`` 7, ``KEEP_MIRROR_PRICES`` 1). Restore reads ``--latest prices``. Temp stores only; the clock is
driven through ``backup_store._utcnow``.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from RevenueChaser.core import automations, config, rawstore
from RevenueChaser.module_spx_gamma import config as spx_config
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


def _bars(n: int = 3, ticker: str = "AAA") -> Path:
    rawstore.write_rows("price_bars_daily", [
        {"ticker": ticker, "session_date": f"2026-09-{i + 1:02d}", "close": float(i), "source": "yfinance"}
        for i in range(n)])
    return rawstore.prices_db_path()


def _names(folder: Path, pattern: str) -> list[str]:
    return sorted(p.name for p in folder.glob(pattern))


# --- snapshot ---------------------------------------------------------------------------------------------------

def test_snapshot_prices_writes_a_verified_snapshot(rawstore_tmp_db, tmp_path):
    prices = _bars(3)
    dest = bs.snapshot_prices(out_dir=tmp_path)
    assert dest.name.startswith("prices_") and dest.suffix == ".duckdb"
    assert bs._table_counts(dest)["price_bars_daily"] == 3
    assert bs._table_fingerprints(dest) == bs._table_fingerprints(prices)


def test_snapshot_prices_is_none_when_the_file_does_not_exist(isolate_rawstore, tmp_path):
    assert not rawstore.prices_db_path().exists()
    assert bs.snapshot_prices(out_dir=tmp_path) is None
    assert not list(tmp_path.glob("prices_*"))


def test_snapshot_prices_keeps_two_by_default_and_prunes_only_its_own(rawstore_tmp_db, tmp_path):
    _bars()
    raw = bs.snapshot(out_dir=tmp_path, keep=5)
    made = [bs.snapshot_prices(out_dir=tmp_path) for _ in range(4)]
    assert _names(tmp_path, "prices_*.duckdb") == sorted(p.name for p in made[-2:])
    assert raw.is_file()


# --- the nightly run --------------------------------------------------------------------------------------------

def test_main_backs_up_prices_with_no_weekly_copy_and_logs_it(rawstore_tmp_db, tmp_path, clock, capsys):
    _bars()
    out = tmp_path / "out"
    assert bs.main(["--out", str(out)]) == 0
    text = capsys.readouterr().out
    snap = _names(out, "prices_*.duckdb")
    assert len(snap) == 1 and f"backup OK: {out / snap[0]}" in text
    assert _names(out / "weekly", "prices_*") == []
    assert _names(out / "weekly", "raw_*.duckdb")                       # raw keeps its weekly copy


def test_main_keeps_two_prices_snapshots_by_default_and_keep_prices_overrides(rawstore_tmp_db, tmp_path, clock):
    _bars()
    out = tmp_path / "out"
    for i in range(4):
        clock(_day(2026, 9, 28) + timedelta(days=i))
        assert bs.main(["--out", str(out)]) == 0
    assert len(_names(out, "prices_*.duckdb")) == 2
    for i in range(4, 7):
        clock(_day(2026, 9, 28) + timedelta(days=i))
        assert bs.main(["--out", str(out), "--keep-prices", "3"]) == 0
    assert len(_names(out, "prices_*.duckdb")) == 3
    assert _names(out / "weekly", "prices_*") == []


def test_keep_prices_must_be_at_least_one_so_a_run_never_deletes_the_snapshot_it_wrote(rawstore_tmp_db, tmp_path,
                                                                                       clock):
    _bars()
    out = tmp_path / "out"
    assert bs.main(["--out", str(out)]) == 0
    snap = _names(out, "prices_*.duckdb")
    clock(_day(2026, 9, 29))
    with pytest.raises(SystemExit) as stop:
        bs.main(["--out", str(out), "--keep-prices", "0"])
    assert stop.value.code == 2
    assert _names(out, "prices_*.duckdb") == snap


def test_a_failed_prices_backup_is_logged_and_the_rest_of_the_night_still_runs(
        rawstore_tmp_db, tmp_path, clock, capsys, monkeypatch):
    rawstore.write_rows("watchlist", [{"ticker": "NOK"}])
    out, mirror = tmp_path / "out", tmp_path / "drive"
    mirror.mkdir()
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", str(mirror))

    def boom(*a, **k):
        raise bs.BackupError("disk full")

    monkeypatch.setattr(bs, "snapshot_prices", boom)
    assert bs.main(["--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "prices backup FAILED: disk full" in err and "skipped, 1 backup step(s) failed" in err
    assert _names(out, "raw_*.duckdb") and _names(out, "discord_*.duckdb")
    assert list(mirror.iterdir()) == []


# --- off-machine copy -------------------------------------------------------------------------------------------

def test_the_off_machine_copy_carries_the_newest_prices_snapshot(rawstore_tmp_db, tmp_path, clock, monkeypatch):
    _bars()
    out, mirror = tmp_path / "out", tmp_path / "drive"
    mirror.mkdir()
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", str(mirror))
    assert bs.main(["--out", str(out)]) == 0
    drive = mirror / SUBDIR
    name = _names(out, "prices_*.duckdb")[-1]
    assert _names(drive, "prices_*.duckdb") == [name]
    assert bs._table_fingerprints(drive / name) == bs._table_fingerprints(out / name)
    assert _names(drive / "weekly", "prices_*") == []


def test_one_prices_copy_stays_off_machine_whatever_the_other_keep_flags_say(rawstore_tmp_db, tmp_path, clock,
                                                                            monkeypatch):
    _bars()
    out, mirror = tmp_path / "out", tmp_path / "drive"
    mirror.mkdir()
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", str(mirror))
    assert bs.KEEP_MIRROR_PRICES == 1                                     # avo's card, 12:06Z 2026-10-09
    for i in range(3):
        clock(_day(2026, 9, 28) + timedelta(days=i))
        assert bs.main(["--out", str(out), "--keep-prices", "5", "--keep-mirror", "5", "--keep-mirror-big", "5",
                        "--mirror-big-now"]) == 0
    assert _names(mirror / SUBDIR, "prices_*.duckdb") == _names(out, "prices_*.duckdb")[-1:]


def test_prices_go_off_machine_once_a_week_whatever_the_big_file_schedule_says(rawstore_tmp_db, tmp_path, clock,
                                                                                monkeypatch, capsys):
    """Drive keeps every pruned copy in its quota-counting trash for 30 days (#349), so the ~2.4 GiB file goes once a
    week (avo's card), even when the raw and edgar copies go every run."""
    _bars()
    out, mirror = tmp_path / "out", tmp_path / "drive"
    mirror.mkdir()
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", str(mirror))
    assert bs.MIRROR_PRICES_EVERY_DAYS == 7
    there, made = [], {}
    for d in (0, 1, 6, 7, 14):
        clock(_day(2026, 9, 28) + timedelta(days=d))
        capsys.readouterr()
        assert bs.main(["--out", str(out), "--mirror-big-every-days", "0"]) == 0
        made[d] = _names(out, "prices_*.duckdb")[-1]
        there.append(_names(mirror / SUBDIR, "prices_*.duckdb"))
        if d == 1:
            assert "big files skipped until 2026-10-05" in capsys.readouterr().out
    assert there == [[made[0]], [made[0]], [made[0]], [made[7]], [made[14]]]
    assert len(_names(mirror / SUBDIR, "raw_*.duckdb")) == 2              # raw went every run (keep 2)


def _aged(root: Path, rel: str, data: bytes) -> None:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    t = time.time() - 3600                                              # settled: the mirrors take it this run
    os.utime(f, (t, t))


def test_a_failed_prices_upload_comes_last_and_costs_no_other_off_machine_copy(
        rawstore_tmp_db, tmp_path, clock, monkeypatch, capsys):
    """Review F1: after the fill the prices snapshot is ~2.4 GiB. A failed upload of it (Drive quota, a timeout) must
    not cost that night's copies of the SPX and Discord mirrors, which cannot be fetched again; prices can."""
    _bars()
    spx, media = tmp_path / "spx", tmp_path / "media"
    _aged(spx, "brier.json", b'{"records": []}')
    _aged(media, "900/a.png", b"A" * 10)
    monkeypatch.setattr(spx_config, "DATA_ROOT", spx)
    monkeypatch.setattr(config, "DISCORD_MEDIA_DIR", media)
    out, mirror = tmp_path / "out", tmp_path / "drive"
    mirror.mkdir()
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", str(mirror))
    real = bs._copy_verified

    def quota(src, dest):
        if src.name.startswith("prices_"):
            raise bs.BackupError("Drive quota exceeded")
        return real(src, dest)

    monkeypatch.setattr(bs, "_copy_verified", quota)
    assert bs.main(["--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "off-machine copy FAILED" in err and "Drive quota exceeded" in err
    drive = mirror / SUBDIR
    assert (drive / "spx_gamma_mirror" / "brier.json").read_bytes() == b'{"records": []}'
    assert (drive / "discord_media_mirror" / "900" / "a.png").read_bytes() == b"A" * 10
    for pattern in ("raw_*.duckdb", "edgar_*.duckdb", "discord_*.duckdb"):
        assert _names(drive, pattern) == _names(out, pattern)[-1:] and _names(drive, pattern), pattern
    assert _names(drive, "prices_*") == []


# --- restore ----------------------------------------------------------------------------------------------------

def test_restore_round_trip_for_prices(rawstore_tmp_db, tmp_path):
    prices = _bars(3)
    snap = bs.snapshot_prices(out_dir=tmp_path)
    before = bs._table_fingerprints(prices)
    conn = duckdb.connect(str(prices))
    conn.execute("DELETE FROM price_bars_daily")
    conn.close()
    assert rs.restore(snap) == prices
    assert bs._table_fingerprints(prices) == before
    assert prices.with_name("prices.duckdb.pre-restore").exists()


def test_restore_latest_prices_and_list(rawstore_tmp_db, tmp_path, capsys):
    prices = _bars(2)
    bs.snapshot(out_dir=tmp_path)
    old = bs.snapshot_prices(out_dir=tmp_path)
    _bars(2, ticker="BBB")
    new = bs.snapshot_prices(out_dir=tmp_path)
    assert rs.snapshots(tmp_path, "prices") == [old, new] and new in rs.snapshots(tmp_path)
    assert rs.main(["--from", str(tmp_path), "--latest", "prices"]) == 0
    assert bs._table_counts(prices)["price_bars_daily"] == 4
    capsys.readouterr()
    assert rs.main(["--from", str(tmp_path), "--list"]) == 0
    assert new.name in capsys.readouterr().out


# --- Automations row --------------------------------------------------------------------------------------------

def test_the_backup_row_names_prices_and_its_rule_and_keeps_its_arguments():
    row = next(e for e in automations.REGISTRY if e.name == "RC-Store-Backup")
    assert "(raw, edgar, discord, history, prices)" in row.description
    assert "2 daily prices snapshots" in row.description
    assert row.args == ("-m", "RevenueChaser.scripts.backup_store", "--keep", "5")
