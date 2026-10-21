"""#72 S1 (backlog-review condition F12): backup, restore and compact cover ``discord.duckdb``
and the ``discord_media/`` tree. #248: the media tree is backed up as an incremental, verified
mirror (not a nightly zip). Temp stores only."""
from __future__ import annotations

import os
import shutil
import time

import pytest

from RevenueChaser.core import automations, config, rawstore
from RevenueChaser.scripts import backup_store as bs
from RevenueChaser.scripts import compact_store as cs
from RevenueChaser.scripts import restore_store as rs


def _seed():
    rawstore.write_rows("discord_channels", [{"channel_id": "1", "name": "general"}])
    rawstore.write_rows("discord_messages", [
        {"message_id": "10", "channel_id": "1", "content": "hello"},
        {"message_id": "11", "channel_id": "1", "content": "world"}])


def test_snapshot_discord_writes_a_verified_snapshot(rawstore_tmp_db, tmp_path):
    _seed()
    dest = bs.snapshot_discord(out_dir=tmp_path)
    assert dest.name.startswith("discord_") and dest.suffix == ".duckdb"
    counts = bs._table_counts(dest)
    assert counts["discord_messages"] == 2 and counts["discord_channels"] == 1
    assert counts == bs._table_counts(rawstore.discord_db_path())


def test_snapshot_discord_is_none_when_the_store_does_not_exist(rawstore_tmp_db, tmp_path):
    rawstore.discord_db_path().unlink()
    assert bs.snapshot_discord(out_dir=tmp_path) is None


def test_snapshot_discord_keep_prunes_only_discord_snapshots(rawstore_tmp_db, tmp_path):
    _seed()
    raw = bs.snapshot(out_dir=tmp_path, keep=5)
    first = bs.snapshot_discord(out_dir=tmp_path, keep=1)
    second = bs.snapshot_discord(out_dir=tmp_path, keep=1)
    assert not first.exists() and second.is_file() and raw.is_file()


# --- #248: discord_media/ is mirrored incrementally, not zipped every night ------------------

_MIRROR = "discord_media_mirror"
_FILES = {"900/a.png": b"A" * 10, "900/b.png": b"B" * 20, "901/c.pdf": b"C" * 5}


def _media(tmp_path, files=None, age_s=3600):
    """A media tree whose files are ``age_s`` old, i.e. settled and safe to copy."""
    src = tmp_path / "media"
    for rel, data in (files if files is not None else _FILES).items():
        _put(src, rel, data, age_s)
    return src


def _put(src, rel, data, age_s=3600):
    f = src / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    t = time.time() - age_s
    os.utime(f, (t, t))
    return f


def _counts(res):
    return res.copied, res.unchanged, res.skipped, res.total


def _nightly_args():
    """The exact arguments the installed ``RC-Store-Backup`` task passes to the module."""
    args = automations.entry("RC-Store-Backup").args
    assert args[:2] == ("-m", "RevenueChaser.scripts.backup_store")
    return list(args[2:])


def test_mirror_first_run_copies_every_settled_file_byte_for_byte(tmp_path):
    src = _media(tmp_path)
    out = tmp_path / "out"
    res = bs.mirror_discord_media(src=src, out_dir=out)
    assert _counts(res) == (3, 0, 0, 3)
    assert res.dest == out / _MIRROR
    for rel, data in _FILES.items():
        assert (out / _MIRROR / rel).read_bytes() == data


def test_mirror_second_run_with_no_changes_copies_and_hashes_nothing(tmp_path, monkeypatch):
    src = _media(tmp_path)
    out = tmp_path / "out"
    bs.mirror_discord_media(src=src, out_dir=out)
    before = {rel: (out / _MIRROR / rel).stat().st_mtime_ns for rel in _FILES}

    def boom(*a, **k):
        raise AssertionError("an unchanged file must not be read")

    monkeypatch.setattr(bs, "_copy_file", boom)
    monkeypatch.setattr(bs, "_sha256", boom)
    res = bs.mirror_discord_media(src=src, out_dir=out)
    assert _counts(res) == (0, 3, 0, 3)
    assert {rel: (out / _MIRROR / rel).stat().st_mtime_ns for rel in _FILES} == before


def test_mirror_treats_an_mtime_within_two_seconds_as_unchanged(tmp_path):
    src = _media(tmp_path, {"1/a.png": b"x" * 4})
    out = tmp_path / "out"
    bs.mirror_discord_media(src=src, out_dir=out)
    m = out / _MIRROR / "1" / "a.png"
    st = m.stat()
    os.utime(m, ns=(st.st_atime_ns, st.st_mtime_ns + 1_500_000_000))   # FAT/NTFS rounding
    assert _counts(bs.mirror_discord_media(src=src, out_dir=out)) == (0, 1, 0, 1)
    os.utime(m, ns=(st.st_atime_ns, st.st_mtime_ns + 3_000_000_000))
    assert _counts(bs.mirror_discord_media(src=src, out_dir=out)) == (1, 0, 0, 1)


def test_mirror_copies_only_the_new_file(tmp_path, monkeypatch):
    src = _media(tmp_path)
    out = tmp_path / "out"
    bs.mirror_discord_media(src=src, out_dir=out)
    _put(src, "900/new.png", b"N" * 7)
    copied = []
    real = bs._copy_file
    monkeypatch.setattr(bs, "_copy_file", lambda s, d: (copied.append(os.fspath(s)), real(s, d))[1])
    res = bs.mirror_discord_media(src=src, out_dir=out)
    assert _counts(res) == (1, 3, 0, 4)
    assert [os.path.basename(c) for c in copied] == ["new.png"]
    assert (out / _MIRROR / "900" / "new.png").read_bytes() == b"N" * 7


def test_mirror_recopies_a_changed_file_and_keeps_one_copy(tmp_path):
    src = _media(tmp_path)
    out = tmp_path / "out"
    bs.mirror_discord_media(src=src, out_dir=out)
    _put(src, "900/a.png", b"A2" * 40, age_s=1800)             # different size and content
    assert _counts(bs.mirror_discord_media(src=src, out_dir=out)) == (1, 2, 0, 3)
    assert (out / _MIRROR / "900" / "a.png").read_bytes() == b"A2" * 40
    # same size, later mtime: still detected
    _put(src, "900/b.png", b"Z" * 20, age_s=900)
    assert _counts(bs.mirror_discord_media(src=src, out_dir=out)) == (1, 2, 0, 3)
    assert (out / _MIRROR / "900" / "b.png").read_bytes() == b"Z" * 20
    assert sorted(p.name for p in (out / _MIRROR / "900").iterdir()) == ["a.png", "b.png"]


def test_mirror_skips_a_file_inside_the_settle_window_and_picks_it_up_later(tmp_path):
    assert bs.MEDIA_SETTLE_SECONDS == 600
    src = _media(tmp_path)
    fresh = _put(src, "900/downloading.mp4", b"half", age_s=30)
    out = tmp_path / "out"
    res = bs.mirror_discord_media(src=src, out_dir=out)
    assert _counts(res) == (3, 0, 1, 4)
    assert not (out / _MIRROR / "900" / "downloading.mp4").exists()
    t = time.time() - 3600                                       # the download finished long ago
    os.utime(fresh, (t, t))
    res = bs.mirror_discord_media(src=src, out_dir=out)
    assert _counts(res) == (1, 3, 0, 4)
    assert (out / _MIRROR / "900" / "downloading.mp4").read_bytes() == b"half"


def test_mirror_settle_window_is_a_parameter(tmp_path):
    src = _media(tmp_path, {"1/a.png": b"x"}, age_s=30)
    assert _counts(bs.mirror_discord_media(src=src, out_dir=tmp_path / "o", settle_s=0)) == (1, 0, 0, 1)


def test_mirror_keeps_a_file_that_vanished_from_the_source(tmp_path):
    src = _media(tmp_path)
    out = tmp_path / "out"
    bs.mirror_discord_media(src=src, out_dir=out)
    (src / "900" / "a.png").unlink()                              # e.g. the message was deleted
    res = bs.mirror_discord_media(src=src, out_dir=out)
    assert _counts(res) == (0, 2, 0, 2)
    assert (out / _MIRROR / "900" / "a.png").read_bytes() == b"A" * 10


def test_mirror_hash_mismatch_raises_and_leaves_the_previous_copy_untouched(tmp_path, monkeypatch):
    src = _media(tmp_path)
    out = tmp_path / "out"
    bs.mirror_discord_media(src=src, out_dir=out)
    _put(src, "900/a.png", b"A2" * 40, age_s=1800)

    def corrupting_copy(s, d):
        shutil.copy2(s, d)
        data = bytearray(open(d, "rb").read())
        data[0] ^= 0xFF                                           # same size, one flipped byte
        open(d, "wb").write(bytes(data))

    monkeypatch.setattr(bs, "_copy_file", corrupting_copy)
    with pytest.raises(bs.BackupError, match="a.png"):
        bs.mirror_discord_media(src=src, out_dir=out)
    assert (out / _MIRROR / "900" / "a.png").read_bytes() == b"A" * 10
    assert list((out / _MIRROR / ".tmp").glob("*")) == []


def test_mirror_a_failed_copy_is_a_backup_error_and_leaves_no_temp_file(tmp_path, monkeypatch):
    src = _media(tmp_path, {"1/a.png": b"x" * 3})
    out = tmp_path / "out"

    def full_disk(s, d):
        open(d, "wb").write(b"x")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(bs, "_copy_file", full_disk)
    with pytest.raises(bs.BackupError, match="a.png"):
        bs.mirror_discord_media(src=src, out_dir=out)
    assert list((out / _MIRROR / ".tmp").glob("*")) == []
    assert not (out / _MIRROR / "1" / "a.png").exists()


def test_mirror_skips_a_file_whose_source_changed_during_the_copy(tmp_path, monkeypatch):
    src = _media(tmp_path, {"1/a.png": b"x" * 100})
    out = tmp_path / "out"
    real = bs._copy_file

    def copy_then_the_download_continues(s, d):
        real(s, d)
        with open(s, "ab") as fh:
            fh.write(b"more bytes arrived")
        t = time.time() - 3500
        os.utime(s, (t, t))

    monkeypatch.setattr(bs, "_copy_file", copy_then_the_download_continues)
    res = bs.mirror_discord_media(src=src, out_dir=out)                # in flight, not an error
    assert _counts(res) == (0, 0, 1, 1)
    assert not (out / _MIRROR / "1" / "a.png").exists()
    assert list((out / _MIRROR / ".tmp").glob("*")) == []


def test_mirror_removes_a_leftover_temp_file_from_a_killed_run(tmp_path):
    src = _media(tmp_path)
    out = tmp_path / "out"
    stale = out / _MIRROR / ".tmp" / "stale.part"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"half a file")
    bs.mirror_discord_media(src=src, out_dir=out)
    assert not stale.exists()
    assert not any(p.name == "stale.part" for p in (out / _MIRROR).rglob("*"))


def test_mirror_does_not_back_up_a_source_entry_named_like_its_temp_dir(tmp_path):
    src = _media(tmp_path, {"900/a.png": b"A", ".tmp/odd.bin": b"O"})
    out = tmp_path / "out"
    res = bs.mirror_discord_media(src=src, out_dir=out)
    assert _counts(res) == (1, 0, 1, 2)                          # counted, never silently dropped
    assert (out / _MIRROR / "900" / "a.png").is_file()
    assert not (out / _MIRROR / ".tmp" / "odd.bin").exists()


def test_mirror_is_none_for_a_missing_or_empty_tree_and_creates_nothing(tmp_path):
    out = tmp_path / "out"
    assert bs.mirror_discord_media(src=tmp_path / "nope", out_dir=out) is None
    (tmp_path / "empty" / "900").mkdir(parents=True)
    assert bs.mirror_discord_media(src=tmp_path / "empty", out_dir=out) is None
    assert not (out / _MIRROR).exists()


def test_main_with_the_nightly_task_args_mirrors_media_and_writes_no_zip(
        rawstore_tmp_db, tmp_path, capsys, monkeypatch):
    _seed()
    monkeypatch.setattr(config, "DISCORD_MEDIA_DIR", _media(tmp_path))
    out = tmp_path / "out"
    assert bs.main(_nightly_args() + ["--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "discord_media mirror OK: 3 copied, 0 unchanged, 0 in flight, 3 files" in text
    assert str(out / _MIRROR) in text
    assert (out / _MIRROR / "900" / "a.png").read_bytes() == b"A" * 10
    assert list(out.glob("discord_media_*.zip")) == []
    assert list(out.glob("discord_*.duckdb")) and list(out.glob("raw_*.duckdb"))   # DBs unchanged


def test_main_second_nightly_run_reports_everything_unchanged(rawstore_tmp_db, tmp_path, capsys, monkeypatch):
    _seed()
    monkeypatch.setattr(config, "DISCORD_MEDIA_DIR", _media(tmp_path))
    out = tmp_path / "out"
    assert bs.main(_nightly_args() + ["--out", str(out)]) == 0
    capsys.readouterr()
    assert bs.main(_nightly_args() + ["--out", str(out)]) == 0
    assert "0 copied, 3 unchanged, 0 in flight, 3 files" in capsys.readouterr().out


def test_main_leaves_a_pre_existing_media_zip_untouched(rawstore_tmp_db, tmp_path, monkeypatch):
    _seed()
    monkeypatch.setattr(config, "DISCORD_MEDIA_DIR", _media(tmp_path))
    out = tmp_path / "out"
    out.mkdir()
    old = out / "discord_media_20261003_000000_000000.zip"
    import zipfile                                        # a real zip: the #309 scan self-checks a file it adopts
    with zipfile.ZipFile(old, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("900/a.png", b"old-archive")
    before = old.read_bytes()
    assert bs.main(["--out", str(out)]) == 0
    assert old.read_bytes() == before
    assert list(out.glob("discord_media_*.zip")) == [old]


def test_main_with_no_media_dir_returns_0_and_creates_no_mirror(rawstore_tmp_db, tmp_path, capsys, monkeypatch):
    _seed()
    monkeypatch.setattr(config, "DISCORD_MEDIA_DIR", tmp_path / "no-such-dir")
    out = tmp_path / "out"
    assert bs.main(["--out", str(out)]) == 0
    assert "discord_media mirror" not in capsys.readouterr().out
    assert not (out / _MIRROR).exists()


def test_main_reports_a_media_mirror_failure_after_the_db_snapshots(rawstore_tmp_db, tmp_path, capsys, monkeypatch):
    _seed()

    def boom(*a, **k):
        raise bs.BackupError("hash mismatch for 900/a.png")

    monkeypatch.setattr(bs, "mirror_discord_media", boom)
    out = tmp_path / "out"
    assert bs.main(["--out", str(out)]) == 1
    assert "discord_media backup FAILED: hash mismatch for 900/a.png" in capsys.readouterr().err
    assert list(out.glob("discord_*.duckdb"))                    # the DB snapshots were already written


def test_main_no_longer_accepts_the_removed_media_zip_flag():
    with pytest.raises(SystemExit):
        bs.main(["--keep-discord-media", "1"])


def test_main_reports_a_discord_failure(rawstore_tmp_db, tmp_path, capsys, monkeypatch):
    _seed()

    def boom(*a, **k):
        raise bs.BackupError("disk full")

    monkeypatch.setattr(bs, "snapshot_discord", boom)
    assert bs.main(["--out", str(tmp_path)]) == 1
    assert "discord backup FAILED: disk full" in capsys.readouterr().err


def test_restore_round_trip_for_discord(rawstore_tmp_db, tmp_path):
    _seed()
    snap = bs.snapshot_discord(out_dir=tmp_path)
    rawstore.write_rows("discord_messages", [{"message_id": "12", "channel_id": "1", "content": "later"}])
    assert rs.main(["--from", str(tmp_path), "--latest", "discord"]) == 0
    assert rawstore.count("discord_messages") == 2
    assert rawstore.discord_db_path().with_name("discord.duckdb.pre-restore").exists()
    assert snap.is_file()


def test_restore_lists_discord_snapshots(rawstore_tmp_db, tmp_path):
    _seed()
    snap = bs.snapshot_discord(out_dir=tmp_path)
    assert snap in rs.snapshots(tmp_path, "discord")
    assert snap in rs.snapshots(tmp_path)
    assert rs._kind_of(snap) == "discord"


def test_restore_does_not_confuse_media_archives_with_snapshots(rawstore_tmp_db, tmp_path):
    (tmp_path / "discord_media_20260101_000000_000000.zip").write_bytes(b"PK")
    assert rs.snapshots(tmp_path, "discord") == []


def test_restore_does_not_list_the_media_mirror_as_a_snapshot(rawstore_tmp_db, tmp_path):
    _seed()
    out = tmp_path / "out"
    snap = bs.snapshot_discord(out_dir=out)
    bs.mirror_discord_media(src=_media(tmp_path), out_dir=out)
    assert (out / _MIRROR).is_dir()
    assert rs.snapshots(out) == [snap]
    assert rs.snapshots(out, "discord") == [snap]


def test_compact_discord_shrinks_and_keeps_rows(rawstore_tmp_db, capsys):
    _seed()
    assert cs.main(["--db", "discord"]) == 0
    assert "discord:" in capsys.readouterr().out
    assert rawstore.count("discord_messages") == 2
    assert rawstore.discord_db_path().with_name("discord.duckdb.pre-compact").exists()


def test_compact_both_includes_discord(rawstore_tmp_db, capsys):
    assert cs.main([]) == 0
    out = capsys.readouterr().out
    assert "raw:" in out and "edgar:" in out and "discord:" in out
