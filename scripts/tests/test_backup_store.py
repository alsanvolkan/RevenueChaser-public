"""Tests for scripts.backup_store — verified snapshots of the durable store."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
from datetime import date, datetime, timezone

import duckdb
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.scripts import backup_store as bs
from RevenueChaser.scripts.tests.duckdb_damage import damage_data_block


def _seed():
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": "AAPL", "payload": {"x": 1}, "fetched_at": 1.0, "fetched_at_iso": ""}])
    rawstore.write_rows("watchlist", [{"ticker": "NOK"}])


def test_backup_writes_a_verified_snapshot(rawstore_tmp_db, tmp_path):
    _seed()
    dest = bs.snapshot(out_dir=tmp_path, keep=3)
    assert dest.is_file()
    assert dest.name.startswith("raw_") and dest.name.endswith(".duckdb")
    assert bs._table_counts(dest) == bs._table_counts(rawstore.DB_PATH)


def test_backup_snapshot_matches_live_tables_and_rows(rawstore_tmp_db, tmp_path):
    _seed()
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": "MSFT", "payload": {"x": 2}, "fetched_at": 2.0, "fetched_at_iso": ""}])
    dest = bs.snapshot(out_dir=tmp_path, keep=3)
    counts = bs._table_counts(dest)
    assert counts["ticker_payload_cache"] == 2
    assert counts["watchlist"] == 1


def test_keep_prunes_older_verified_snapshots(rawstore_tmp_db, tmp_path):
    _seed()
    first = bs.snapshot(out_dir=tmp_path, keep=2)
    assert first.is_file()
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": "MSFT", "payload": {"x": 2}, "fetched_at": 2.0, "fetched_at_iso": ""}])
    second = bs.snapshot(out_dir=tmp_path, keep=2)
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": "NVDA", "payload": {"x": 3}, "fetched_at": 3.0, "fetched_at_iso": ""}])
    third = bs.snapshot(out_dir=tmp_path, keep=2)
    assert second.is_file() and third.is_file()
    assert not first.exists(), "keep=2 must prune the oldest once a third snapshot exists"
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": "AMD", "payload": {"x": 4}, "fetched_at": 4.0, "fetched_at_iso": ""}])
    fourth = bs.snapshot(out_dir=tmp_path, keep=2)
    assert {p.name for p in tmp_path.glob("raw_*.duckdb")} == {third.name, fourth.name}


def test_backup_missing_store_raises(rawstore_tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "none.duckdb")
    with pytest.raises(bs.BackupError):
        bs.snapshot(out_dir=tmp_path / "out")


def test_failed_snapshot_preserves_existing_backups(rawstore_tmp_db, tmp_path, monkeypatch):
    _seed()
    good = bs.snapshot(out_dir=tmp_path, keep=5)

    def broken_verify(_db, _path):
        raise bs._VerificationError("boom")

    monkeypatch.setattr(bs, "_verify", broken_verify)
    with pytest.raises(bs.BackupError):
        bs.snapshot(out_dir=tmp_path, keep=5, attempts=1)
    # existing verified backup untouched, nothing half-written kept
    assert list(tmp_path.glob("raw_*.duckdb")) == [good]
    assert not list(tmp_path.glob("raw_*.tmp"))


def test_verification_detects_missing_table(rawstore_tmp_db, tmp_path):
    _seed()
    dest = bs.snapshot(out_dir=tmp_path, keep=3)
    # drop a table from the snapshot so it no longer matches the live store
    conn = duckdb.connect(str(dest))
    try:
        conn.execute("DROP TABLE watchlist")
    finally:
        conn.close()
    with pytest.raises(bs.BackupError):
        bs._verify(rawstore.DB_PATH, dest)


def test_main_cli_success_and_failure(rawstore_tmp_db, tmp_path, capsys, monkeypatch):
    _seed()
    assert bs.main(["--out", str(tmp_path), "--keep", "2"]) == 0
    assert "backup OK:" in capsys.readouterr().out
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "none.duckdb")
    assert bs.main(["--out", str(tmp_path / "out")]) == 1
    assert "backup FAILED:" in capsys.readouterr().err


def test_verification_detects_changed_content_with_equal_counts(rawstore_tmp_db, tmp_path):
    """Row counts alone would pass a snapshot whose cells differ."""
    _seed()
    dest = bs.snapshot(out_dir=tmp_path, keep=3)
    conn = duckdb.connect(str(dest))
    try:
        conn.execute("UPDATE watchlist SET ticker = 'XXX'")
    finally:
        conn.close()
    with pytest.raises(bs._VerificationError, match="content differs"):
        bs._verify(rawstore.DB_PATH, dest)


def test_a_busy_store_at_checkpoint_is_retried_not_fatal(rawstore_tmp_db, tmp_path, monkeypatch):
    _seed()
    real = rawstore._connect_with_retry
    calls = []

    def flaky(delays, path=None):
        calls.append(1)
        if len(calls) == 1:
            raise rawstore.RawStoreError("locked for a moment")
        return real(delays, path)

    monkeypatch.setattr(rawstore, "_connect_with_retry", flaky)
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)
    dest = bs.snapshot(out_dir=tmp_path, keep=3, attempts=3)
    assert dest.is_file() and len(calls) >= 2


def test_a_failed_copy_is_retried_then_reported_as_backup_error(rawstore_tmp_db, tmp_path,
                                                                 monkeypatch):
    _seed()

    def no_copy(*a, **k):
        raise PermissionError("sharing violation")

    monkeypatch.setattr(bs.shutil, "copy2", no_copy)
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)
    with pytest.raises(bs.BackupError, match="could not copy"):
        bs.snapshot(out_dir=tmp_path, keep=3, attempts=2)


def test_edgar_docs_archive_is_verified(tmp_path):
    docs = tmp_path / "docs" / "320193"
    docs.mkdir(parents=True)
    for i in range(3):
        (docs / f"000{i}.html.gz").write_bytes(b"x" * 100)
    out = tmp_path / "out"
    archive = bs.snapshot_edgar_docs(src=tmp_path / "docs", out_dir=out, keep=2)
    assert archive.name.startswith("edgar_docs_") and ".partial" not in archive.name
    bs._verify_archive(archive, 3)
    with pytest.raises(bs.BackupError, match="expected 4"):
        bs._verify_archive(archive, 4)


def test_restore_round_trip_keeps_the_previous_file(rawstore_tmp_db, tmp_path):
    from RevenueChaser.scripts import restore_store as rs
    _seed()
    snap = bs.snapshot(out_dir=tmp_path, keep=3)
    rawstore.write_rows("watchlist", [{"ticker": "LATER"}])
    assert rs.main(["--from", str(tmp_path), "--latest", "raw"]) == 0
    assert rawstore.load_watchlist() == {"NOK"}
    assert rawstore.DB_PATH.with_name("raw.duckdb.pre-restore").exists()
    assert snap.is_file()


def test_restore_refuses_while_a_peer_uses_the_store(rawstore_tmp_db, tmp_path):
    import subprocess
    import sys
    from RevenueChaser.scripts import restore_store as rs
    _seed()
    snap = bs.snapshot(out_dir=tmp_path, keep=3)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        rawstore._presence_file(rawstore.DB_PATH.parent, proc.pid).write_text(str(proc.pid))
        with pytest.raises(bs.BackupError, match="in use"):
            rs.restore(snap)
    finally:
        proc.kill()
        proc.wait()


def _bulk_rows(n: int = 3000) -> None:
    rawstore.write_rows("ticker_payload_cache", [
        {"ticker": f"T{i}", "payload": {"x": "y" * 40}, "fetched_at": 1.0, "fetched_at_iso": ""} for i in range(n)])


def test_a_damaged_data_block_is_a_verification_error_not_a_raw_duckdb_exception(tmp_path):
    """Review M3: the header and catalog open, the query over a table raises ``duckdb.IOException`` ("Corrupt
    database file: computed checksum ... does not match"), which is neither a ``BackupError`` nor an ``OSError``."""
    db = tmp_path / "x.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE a (i INTEGER, s VARCHAR)")
    conn.execute("INSERT INTO a SELECT i, md5(i::VARCHAR) FROM range(5000) t(i)")
    conn.execute("CHECKPOINT")
    conn.close()
    damage_data_block(db)
    with pytest.raises(bs._VerificationError, match="could not read x.duckdb"):
        bs._table_fingerprints(db)


def test_a_snapshot_copy_damaged_once_is_retried_and_leaves_no_temp_file(rawstore_tmp_db, tmp_path, monkeypatch):
    _seed()
    _bulk_rows()
    real, calls = shutil.copy2, []

    def damaging(src, dst, *a, **k):
        real(src, dst, *a, **k)
        calls.append(dst)
        if len(calls) == 1:
            damage_data_block(dst)                         # the first copy of the night is bad
        return dst

    monkeypatch.setattr(bs.shutil, "copy2", damaging)
    dest = bs.snapshot(out_dir=tmp_path, keep=3)
    assert len(calls) == 2                                 # the verify failed and the copy was tried again
    assert bs._table_counts(dest) == bs._table_counts(rawstore.DB_PATH)
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_snapshot_that_stays_damaged_fails_with_a_backup_error_and_no_temp_file(rawstore_tmp_db, tmp_path, monkeypatch):
    _seed()
    _bulk_rows()
    real = shutil.copy2

    def always_damaging(src, dst, *a, **k):
        real(src, dst, *a, **k)
        damage_data_block(dst)
        return dst

    monkeypatch.setattr(bs.shutil, "copy2", always_damaging)
    with pytest.raises(bs.BackupError, match="could not read"):
        bs.snapshot(out_dir=tmp_path, keep=3, attempts=2)
    assert list(tmp_path.glob("raw_*")) == []              # neither a snapshot nor a full-size .tmp


def test_snapshot_removes_tmp_when_rename_fails(rawstore_tmp_db, tmp_path, monkeypatch):
    """L5: an OSError from os.replace must not leave the ~430 MB .tmp behind."""
    _seed()

    def broken_replace(src, dst):
        raise OSError("rename refused")

    monkeypatch.setattr(bs.os, "replace", broken_replace)
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)
    with pytest.raises(bs.BackupError, match="could not finalize") as excinfo:
        bs.snapshot(out_dir=tmp_path, keep=3, attempts=1)
    assert "raw_" in str(excinfo.value)
    assert list(tmp_path.glob("*.duckdb.tmp")) == []


def test_snapshot_sweeps_stale_tmp(rawstore_tmp_db, tmp_path):
    """L5: a stale .duckdb.tmp from a killed run is removed by the next snapshot."""
    _seed()
    stale = tmp_path / "raw_20200101_000000_000000.duckdb.tmp"
    stale.write_bytes(b"junk")
    dest = bs.snapshot(out_dir=tmp_path, keep=3)
    assert not stale.exists()
    assert dest.is_file()


def test_edgar_docs_zlib_error_leaves_no_partial(tmp_path, monkeypatch):
    """L6: a zlib.error from the archive verification must not leave a .partial.zip."""
    import zipfile
    import zlib

    docs = tmp_path / "docs" / "320193"
    docs.mkdir(parents=True)
    (docs / "0000.html.gz").write_bytes(b"x" * 100)
    out = tmp_path / "out"

    def boom(self, *a, **k):
        raise zlib.error("bad crc")

    monkeypatch.setattr(zipfile.ZipFile, "testzip", boom)
    with pytest.raises(bs.BackupError, match="unreadable"):
        bs.snapshot_edgar_docs(src=tmp_path / "docs", out_dir=out, keep=2)
    assert list(out.glob("*.partial.zip")) == []


def test_edgar_docs_sweeps_stale_partial(tmp_path):
    """L6: a stale .partial.zip is removed, a completed archive is kept."""
    docs = tmp_path / "docs" / "320193"
    docs.mkdir(parents=True)
    (docs / "0000.html.gz").write_bytes(b"x" * 100)
    out = tmp_path / "out"
    out.mkdir()
    stale = out / "edgar_docs_20200101_000000_000000.partial.zip"
    stale.write_bytes(b"junk")
    completed = out / "edgar_docs_20190101_000000_000000.zip"
    shutil.make_archive(str(completed.with_suffix("")), "zip", root_dir=str(docs))   # an older tree's archive (#336)
    archive = bs.snapshot_edgar_docs(src=tmp_path / "docs", out_dir=out, keep=5)
    assert not stale.exists()
    assert completed.exists()
    assert archive.is_file()


def _unlink_raising_for(names: set[str]):
    real = pathlib.Path.unlink

    def guarded(self, *args, **kwargs):
        if self.name in names or self.name.endswith(".duckdb.tmp") and names == {".duckdb.tmp"}:
            raise PermissionError(self.name)
        return real(self, *args, **kwargs)

    return guarded


def test_snapshot_survives_an_undeletable_stale_tmp(rawstore_tmp_db, tmp_path, monkeypatch):
    """N1: a stale .duckdb.tmp that cannot be deleted must not stop the snapshot."""
    _seed()
    stale = tmp_path / "raw_20200101_000000_000000.duckdb.tmp"
    stale.write_bytes(b"junk")
    monkeypatch.setattr(pathlib.Path, "unlink", _unlink_raising_for({stale.name}))
    dest = bs.snapshot(out_dir=tmp_path, keep=3)
    assert dest.is_file()
    assert dest.name.startswith("raw_") and dest.name.endswith(".duckdb")


def test_edgar_docs_survives_an_undeletable_stale_partial(tmp_path, monkeypatch):
    """N1: a stale .partial.zip that cannot be deleted must not stop the archive."""
    docs = tmp_path / "docs" / "320193"
    docs.mkdir(parents=True)
    (docs / "0000.html.gz").write_bytes(b"x" * 100)
    out = tmp_path / "out"
    out.mkdir()
    stale = out / "edgar_docs_20200101_000000_000000.partial.zip"
    stale.write_bytes(b"junk")
    monkeypatch.setattr(pathlib.Path, "unlink", _unlink_raising_for({stale.name}))
    archive = bs.snapshot_edgar_docs(src=tmp_path / "docs", out_dir=out, keep=2)
    assert archive.is_file()
    assert archive.name.startswith("edgar_docs_") and archive.name.endswith(".zip")
    assert ".partial" not in archive.name


def test_rename_failure_reports_backup_error_when_tmp_cannot_be_deleted(
        rawstore_tmp_db, tmp_path, monkeypatch):
    """N2: a failed rename must still raise BackupError if the .tmp cannot be deleted."""
    _seed()

    def broken_replace(src, dst):
        raise OSError("rename refused")

    monkeypatch.setattr(bs.os, "replace", broken_replace)
    monkeypatch.setattr(pathlib.Path, "unlink", _unlink_raising_for({".duckdb.tmp"}))
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)
    with pytest.raises(bs.BackupError, match="could not finalize"):
        bs.snapshot(out_dir=tmp_path, keep=3, attempts=1)


def _edgar_docs_src(tmp_path):
    docs = tmp_path / "docs" / "320193"
    docs.mkdir(parents=True)
    (docs / "0000.html.gz").write_bytes(b"x" * 100)
    return tmp_path / "docs", tmp_path / "out"


def test_edgar_docs_verify_failure_reports_backup_error_when_partial_cannot_be_deleted(tmp_path, monkeypatch):
    """N2 for edgar_docs: a locked .partial.zip must not hide the "unreadable" BackupError."""
    import zipfile
    import zlib

    src, out = _edgar_docs_src(tmp_path)

    def boom(self, *a, **k):
        raise zlib.error("bad crc")

    monkeypatch.setattr(zipfile.ZipFile, "testzip", boom)
    real = pathlib.Path.unlink

    def locked(self, *a, **k):
        if self.name.endswith(".partial.zip") and "2020" not in self.name:
            raise PermissionError(self.name)
        return real(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "unlink", locked)
    with pytest.raises(bs.BackupError, match="unreadable"):
        bs.snapshot_edgar_docs(src=src, out_dir=out, keep=2)


def test_edgar_docs_zip_failure_reports_backup_error_when_partial_cannot_be_deleted(tmp_path, monkeypatch):
    """N2 for edgar_docs: a failed zip whose half-written file is locked still raises "could not zip"."""
    src, out = _edgar_docs_src(tmp_path)

    def disk_full(*a, **k):
        raise OSError("No space left on device")

    monkeypatch.setattr(bs.shutil, "make_archive", disk_full)
    real = pathlib.Path.unlink

    def locked(self, *a, **k):
        if self.name.endswith(".partial.zip"):
            raise PermissionError(self.name)
        return real(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "unlink", locked)
    with pytest.raises(bs.BackupError, match="could not zip"):
        bs.snapshot_edgar_docs(src=src, out_dir=out, keep=2)


# ── #336: an unchanged edgar_docs/ tree reuses the newest archive instead of zipping ~1 GB again ──────────────

def _docs_tree(tmp_path, n: int = 3):
    docs = tmp_path / "docs"
    for i in range(n):
        f = docs / "320193" / f"000{i}.html.gz"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * 100)
    return docs, tmp_path / "out"


def _zips(out):
    return sorted(p.name for p in out.glob("edgar_docs_*.zip"))


def test_an_unchanged_docs_tree_reuses_the_newest_archive(tmp_path):
    docs, out = _docs_tree(tmp_path)
    first = bs.archive_edgar_docs(src=docs, out_dir=out, keep=2)
    second = bs.archive_edgar_docs(src=docs, out_dir=out, keep=2)
    assert not first.reused and second.reused
    assert second.path == first.path
    assert _zips(out) == [first.path.name]
    assert bs.snapshot_edgar_docs(src=docs, out_dir=out, keep=2) == first.path


def _add_file(docs):
    (docs / "320193" / "0009.html.gz").write_bytes(b"y" * 100)


def _resize_file(docs):
    f = docs / "320193" / "0000.html.gz"
    st = f.stat()
    f.write_bytes(b"x" * 101)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))          # same name and mtime, another size


def _touch_file(docs):
    f = docs / "320193" / "0000.html.gz"
    st = f.stat()
    f.write_bytes(b"z" * 100)                                 # same size
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


@pytest.mark.parametrize("change", [_add_file, _resize_file, _touch_file],
                         ids=["new-file", "same-name-other-size", "same-size-other-mtime"])
def test_a_changed_docs_tree_gets_a_new_archive(tmp_path, change):
    docs, out = _docs_tree(tmp_path)
    first = bs.archive_edgar_docs(src=docs, out_dir=out, keep=2)
    change(docs)
    second = bs.archive_edgar_docs(src=docs, out_dir=out, keep=2)
    assert not second.reused and second.path != first.path
    assert _zips(out) == sorted([first.path.name, second.path.name])
    third = bs.archive_edgar_docs(src=docs, out_dir=out, keep=2)
    assert third.reused and third.path == second.path


def test_keep_docs_still_prunes_the_oldest_archive(tmp_path):
    docs, out = _docs_tree(tmp_path)
    made = []
    for i in range(3):
        (docs / "320193" / f"10{i}.html.gz").write_bytes(b"n" * 10)
        made.append(bs.archive_edgar_docs(src=docs, out_dir=out, keep=2).path)
    assert _zips(out) == sorted(p.name for p in made[1:])


def test_a_corrupt_newest_archive_is_not_reused_and_is_removed(tmp_path):
    docs, out = _docs_tree(tmp_path)
    older = bs.archive_edgar_docs(src=docs, out_dir=out, keep=2).path
    _add_file(docs)
    newest = bs.archive_edgar_docs(src=docs, out_dir=out, keep=2).path
    data = newest.read_bytes()
    newest.write_bytes(data[:len(data) // 2])                # a cut copy: no central directory left
    again = bs.archive_edgar_docs(src=docs, out_dir=out, keep=2)
    assert not again.reused
    assert not newest.exists()                               # never kept beside the good ones
    assert _zips(out) == sorted([older.name, again.path.name])
    bs._verify_archive(again.path, 4)


def test_an_archive_whose_members_went_missing_is_not_reused(tmp_path):
    """The fingerprint matches but a document is not in the archive: zip again, drop the short one."""
    import zipfile
    docs, out = _docs_tree(tmp_path)
    first = bs.archive_edgar_docs(src=docs, out_dir=out, keep=5).path
    with zipfile.ZipFile(first) as zf:
        comment, names = zf.comment, [n for n in zf.namelist() if n.endswith(".html.gz")]
    first.unlink()
    with zipfile.ZipFile(first, "w") as zf:
        for name in names[:-1]:
            zf.write(docs / name, name)
        zf.comment = comment
    again = bs.archive_edgar_docs(src=docs, out_dir=out, keep=5)
    assert not again.reused and again.path != first and not first.exists()
    bs._verify_archive(again.path, 3)


def test_an_archive_without_a_fingerprint_is_zipped_again_once(tmp_path):
    """An archive from before #336 carries no fingerprint: the first run after the change zips again."""
    import zipfile
    docs, out = _docs_tree(tmp_path)
    old = bs.archive_edgar_docs(src=docs, out_dir=out, keep=5).path
    with zipfile.ZipFile(old, "a") as zf:
        zf.comment = b""
    again = bs.archive_edgar_docs(src=docs, out_dir=out, keep=5)
    assert not again.reused and again.path != old and old.exists()
    assert bs.archive_edgar_docs(src=docs, out_dir=out, keep=5).reused


def test_the_log_line_says_the_archive_was_kept(rawstore_tmp_db, tmp_path, capsys, monkeypatch):
    from RevenueChaser.core import config
    docs, out = _docs_tree(tmp_path)
    monkeypatch.setattr(config, "EDGAR_DOCS_DIR", docs)
    _seed()
    assert bs.main(["--out", str(out)]) == 0
    (archive,) = out.glob("edgar_docs_*.zip")
    first = capsys.readouterr().out
    assert f"backup OK: {archive}\n" in first and "(unchanged, kept)" not in first
    assert bs.main(["--out", str(out)]) == 0
    assert f"backup OK: {archive} (unchanged, kept)\n" in capsys.readouterr().out


def test_the_offmachine_copy_finds_a_reused_archive_already_there(tmp_path):
    docs, out = _docs_tree(tmp_path)
    mirror = tmp_path / "drive"
    mirror.mkdir()
    bs.archive_edgar_docs(src=docs, out_dir=out, keep=2)
    assert bs.offmachine_copy(out, mirror, big_every_days=0).copied == 1
    assert bs.archive_edgar_docs(src=docs, out_dir=out, keep=2).reused
    res = bs.offmachine_copy(out, mirror, big_every_days=0)
    assert (res.copied, res.present) == (0, 1)


# ── N3: the sweeps spare files that only look like leftovers; N4: two more failures surface as BackupError ─────

def _corrupt_first_member(zip_path) -> None:
    """A real corrupt deflate stream (block type 11) in the first compressed member; the size stays the same."""
    import struct
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        info = next(i for i in zf.infolist() if i.compress_type == zipfile.ZIP_DEFLATED and i.file_size)
    data = bytearray(pathlib.Path(zip_path).read_bytes())
    name_len, extra_len = struct.unpack("<HH", data[info.header_offset + 26:info.header_offset + 30])
    data[info.header_offset + 30 + name_len + extra_len] = 0x07
    pathlib.Path(zip_path).write_bytes(bytes(data))


def test_snapshot_sweep_spares_foreign_tmp_files_and_finished_snapshots(rawstore_tmp_db, tmp_path):
    """N3: only ``raw_*.duckdb.tmp`` is a raw leftover; another kind's temp file, a foreign one and a snapshot stay."""
    _seed()
    keep = [tmp_path / "x.duckdb.tmp", tmp_path / "edgar_20200101_000000_000000.duckdb.tmp",
            tmp_path / "raw_20200101_000000_000000.duckdb"]
    for p in keep:
        p.write_bytes(b"junk")
    bs.snapshot(out_dir=tmp_path, keep=5)
    assert all(p.exists() for p in keep)


def test_edgar_docs_sweep_spares_a_foreign_partial_zip(tmp_path):
    """N3: only ``edgar_docs_*.partial.zip`` is a leftover of the docs archive."""
    src, out = _edgar_docs_src(tmp_path)
    out.mkdir()
    foreign = out / "other.partial.zip"
    foreign.write_bytes(b"junk")
    bs.snapshot_edgar_docs(src=src, out_dir=out, keep=2)
    assert foreign.exists()


def test_edgar_docs_real_corrupt_member_is_a_backup_error_and_leaves_no_partial(tmp_path, monkeypatch):
    """N3: a really corrupt deflate stream, not a patched ``testzip``."""
    src, out = _edgar_docs_src(tmp_path)
    real = shutil.make_archive

    def corrupting(*a, **k):
        made = real(*a, **k)
        _corrupt_first_member(made)
        return made

    monkeypatch.setattr(bs.shutil, "make_archive", corrupting)
    with pytest.raises(bs.BackupError, match="unreadable"):
        bs.snapshot_edgar_docs(src=src, out_dir=out, keep=2)
    assert list(out.glob("*.zip")) == []


@pytest.mark.parametrize("exc", [EOFError("cut"), OSError("read error")], ids=["EOFError", "OSError"])
def test_edgar_docs_eof_and_os_errors_in_the_verify_are_backup_errors(tmp_path, monkeypatch, exc):
    """N3: ``EOFError`` and ``OSError`` from the CRC pass are caught like ``zlib.error``."""
    import zipfile
    src, out = _edgar_docs_src(tmp_path)

    def boom(self, *a, **k):
        raise exc

    monkeypatch.setattr(zipfile.ZipFile, "testzip", boom)
    with pytest.raises(bs.BackupError, match="unreadable"):
        bs.snapshot_edgar_docs(src=src, out_dir=out, keep=2)
    assert list(out.glob("*.partial.zip")) == []


def test_offmachine_zlib_error_is_a_backup_error_and_leaves_no_tmp(tmp_path):
    """N4: a ``zlib.error`` from the CRC pass over the off-machine zip was neither a BackupError nor an OSError."""
    docs, out = _docs_tree(tmp_path)
    archive = bs.archive_edgar_docs(src=docs, out_dir=out, keep=2).path
    _corrupt_first_member(archive)
    mirror = tmp_path / "drive"
    mirror.mkdir()
    with pytest.raises(bs.BackupError, match="not a readable zip|corrupt"):
        bs.offmachine_copy(out, mirror, big_every_days=0)
    assert [p.name for p in (mirror / bs.OFFMACHINE_SUBDIR).glob("*.tmp")] == []
    assert not (mirror / bs.OFFMACHINE_SUBDIR / archive.name).exists()


def test_edgar_docs_final_rename_failure_is_a_backup_error_and_leaves_no_partial(tmp_path, monkeypatch):
    """N4: ``os.replace(archive, final)`` was unguarded."""
    src, out = _edgar_docs_src(tmp_path)
    real = os.replace

    def refuse_the_archive(a, b):
        if str(b).endswith(".zip") and ".partial" not in str(b):
            raise OSError("rename refused")
        return real(a, b)

    monkeypatch.setattr(bs.os, "replace", refuse_the_archive)
    with pytest.raises(bs.BackupError, match="could not finalize"):
        bs.snapshot_edgar_docs(src=src, out_dir=out, keep=2)
    assert list(out.glob("*.zip")) == []


# ── #309 L1: a manifest written at copy time, and a periodic, read-only integrity scan ──────────────────────────

def _day(y, m, d, hh=2):
    return datetime(y, m, d, hh, 0, 5, 123456, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    state = {"now": _day(2026, 10, 1)}
    monkeypatch.setattr(bs, "_utcnow", lambda: state["now"])
    return lambda dt: state.update(now=dt)


def _manifest(out) -> dict:
    return json.loads((out / bs.MANIFEST_NAME).read_text(encoding="utf-8"))["files"]


def _sha(p) -> str:
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def _old_file(f, data: bytes) -> pathlib.Path:
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    os.utime(f, (1_700_000_000, 1_700_000_000))
    return f


def _flip_byte_keeping_size_and_mtime(p) -> None:
    st = p.stat()
    data = bytearray(p.read_bytes())
    data[len(data) // 2] ^= 0xFF
    p.write_bytes(bytes(data))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_a_snapshot_is_recorded_in_the_manifest_with_the_hash_of_the_snapshot_file(rawstore_tmp_db, tmp_path):
    _seed()
    out = tmp_path / "out"
    first = bs.snapshot(out_dir=out, keep=1)
    assert _manifest(out)[first.name] == {"size": first.stat().st_size, "sha256": _sha(first)}
    rawstore.write_rows("watchlist", [{"ticker": "MSFT"}])
    second = bs.snapshot(out_dir=out, keep=1)
    assert [k for k in _manifest(out) if k.startswith("raw_")] == [second.name]     # the pruned one left it


def test_the_weekly_copy_and_the_docs_archive_are_recorded_and_a_pruned_archive_leaves(rawstore_tmp_db, tmp_path):
    _seed()
    docs, out = _docs_tree(tmp_path)
    weekly = bs.keep_weekly(bs.snapshot(out_dir=out, keep=3), out_dir=out, enabled=True)
    first = bs.archive_edgar_docs(src=docs, out_dir=out, keep=1).path
    m = _manifest(out)
    assert m[f"weekly/{weekly.name}"] == {"size": weekly.stat().st_size, "sha256": _sha(weekly)}
    assert m[first.name] == {"size": first.stat().st_size, "sha256": _sha(first)}
    _add_file(docs)
    second = bs.archive_edgar_docs(src=docs, out_dir=out, keep=1).path
    m = _manifest(out)
    assert first.name not in m and m[second.name]["sha256"] == _sha(second)


def test_the_mirrors_record_the_hash_of_each_copied_file(tmp_path):
    out = tmp_path / "out"
    _old_file(tmp_path / "media" / "900" / "a.png", b"A" * 64)
    _old_file(tmp_path / "spx" / "brier.json", b'{"records": []}')
    bs.mirror_discord_media(src=tmp_path / "media", out_dir=out, settle_s=0)
    bs.mirror_spx_gamma(src=tmp_path / "spx", out_dir=out, settle_s=0)
    m = _manifest(out)
    media = out / bs.MEDIA_MIRROR_DIRNAME / "900" / "a.png"
    assert m[f"{bs.MEDIA_MIRROR_DIRNAME}/900/a.png"] == {"size": 64, "sha256": _sha(media)}
    assert m[f"{bs.SPX_MIRROR_DIRNAME}/brier.json"]["sha256"] == _sha(out / bs.SPX_MIRROR_DIRNAME / "brier.json")
    (dated,) = (out / bs.SPX_VERSIONS_DIRNAME).glob("brier.*.json")
    assert m[f"{bs.SPX_VERSIONS_DIRNAME}/{dated.name}"]["sha256"] == _sha(dated)


@pytest.fixture
def backed_up(rawstore_tmp_db, tmp_path):
    """A backup folder with a raw snapshot, its weekly copy, a docs archive and a media mirror, all recorded."""
    _seed()
    _bulk_rows()
    docs, out = _docs_tree(tmp_path)
    snap = bs.snapshot(out_dir=out, keep=3)
    weekly = bs.keep_weekly(snap, out_dir=out, enabled=True)
    archive = bs.archive_edgar_docs(src=docs, out_dir=out, keep=2).path
    _old_file(tmp_path / "media" / "900" / "a.png", b"A" * 64)
    bs.mirror_discord_media(src=tmp_path / "media", out_dir=out, settle_s=0)
    return {"out": out, "snap": snap, "weekly": weekly, "zip": archive,
            "media": out / bs.MEDIA_MIRROR_DIRNAME / "900" / "a.png"}


def _failed(res) -> list[str]:
    return [rel for rel, _ in res.failures]


def test_the_scan_passes_an_intact_backup_folder(backed_up, clock):
    res = bs.integrity_scan(backed_up["out"], now=True)
    assert res.ran and res.failures == () and res.checked == 4 and res.adopted == 0


def test_the_scan_detects_a_flipped_byte_with_the_same_size_and_mtime(backed_up, clock):
    """The mirror's own ``stat`` check would call this file current; only the copy-time hash sees the change."""
    f = backed_up["media"]
    _flip_byte_keeping_size_and_mtime(f)
    res = bs.integrity_scan(backed_up["out"], now=True)
    assert _failed(res) == [f"{bs.MEDIA_MIRROR_DIRNAME}/900/a.png"]
    assert "SHA-256" in res.failures[0][1]


def test_the_scan_detects_a_damaged_weekly_duckdb(backed_up, clock):
    damage_data_block(backed_up["weekly"])
    assert _failed(bs.integrity_scan(backed_up["out"], now=True)) == [f"weekly/{backed_up['weekly'].name}"]


def test_the_scan_detects_a_corrupt_edgar_docs_zip(backed_up, clock):
    _corrupt_first_member(backed_up["zip"])
    assert _failed(bs.integrity_scan(backed_up["out"], now=True)) == [backed_up["zip"].name]


def test_the_scan_reports_a_size_change_and_a_missing_file(backed_up, clock):
    with open(backed_up["media"], "ab") as fh:
        fh.write(b"more")
    backed_up["snap"].unlink()
    res = bs.integrity_scan(backed_up["out"], now=True)
    assert sorted(_failed(res)) == sorted([f"{bs.MEDIA_MIRROR_DIRNAME}/900/a.png", backed_up["snap"].name])
    assert dict(res.failures)[backed_up["snap"].name] == "missing"


def test_the_scan_adopts_a_file_from_before_the_manifest_only_after_a_self_check(backed_up, clock):
    out, snap = backed_up["out"], backed_up["snap"]
    good = out / "weekly" / "raw_20250106_020005_123456.duckdb"
    bad = out / "weekly" / "raw_20250113_020005_123456.duckdb"
    shutil.copy2(snap, good)
    shutil.copy2(snap, bad)
    damage_data_block(bad)
    res = bs.integrity_scan(out, now=True)
    assert _failed(res) == [f"weekly/{bad.name}"] and res.adopted == 1
    m = _manifest(out)
    assert m[f"weekly/{good.name}"] == {"size": good.stat().st_size, "sha256": _sha(good)}
    assert f"weekly/{bad.name}" not in m


def test_the_scan_runs_only_every_n_days(backed_up, clock):
    out = backed_up["out"]
    clock(_day(2026, 10, 1))
    assert bs.integrity_scan(out).ran                          # no marker yet: due
    clock(_day(2026, 10, 7))
    res = bs.integrity_scan(out)
    assert not res.ran and res.next_due == date(2026, 10, 8)
    clock(_day(2026, 10, 8))
    assert bs.integrity_scan(out).ran
    clock(_day(2026, 10, 10))
    assert not bs.integrity_scan(out, every_days=3).ran       # the interval is a setting
    clock(_day(2026, 10, 11))
    assert bs.integrity_scan(out, every_days=3).ran


def test_scan_now_forces_it(backed_up, clock):
    out = backed_up["out"]
    assert bs.integrity_scan(out).ran
    assert not bs.integrity_scan(out).ran
    assert bs.integrity_scan(out, now=True).ran


def test_a_failed_scan_is_due_again_the_next_night(backed_up, clock):
    out = backed_up["out"]
    _flip_byte_keeping_size_and_mtime(backed_up["media"])
    assert bs.integrity_scan(out).failures
    clock(_day(2026, 10, 2))
    assert bs.integrity_scan(out).ran                          # the row stays red until a scan passes


def _tree_state(out) -> dict:
    skip = {bs.MANIFEST_NAME, bs.SCAN_MARKER_NAME}
    return {p.relative_to(out).as_posix(): (_sha(p), p.stat().st_mtime_ns)
            for p in out.rglob("*") if p.is_file() and p.name not in skip}


def test_the_scan_never_deletes_or_rewrites_a_backup_file(backed_up, clock):
    _corrupt_first_member(backed_up["zip"])
    _flip_byte_keeping_size_and_mtime(backed_up["media"])
    damage_data_block(backed_up["weekly"])
    before = _tree_state(backed_up["out"])
    assert len(bs.integrity_scan(backed_up["out"], now=True).failures) == 3
    assert _tree_state(backed_up["out"]) == before


def test_a_scan_failure_is_exit_1_and_the_backups_still_ran(rawstore_tmp_db, tmp_path, capsys, clock):
    _seed()
    out = tmp_path / "out"
    assert bs.main(["--out", str(out)]) == 0
    assert "integrity scan OK:" in capsys.readouterr().out
    (snap,) = out.glob("raw_*.duckdb")
    _flip_byte_keeping_size_and_mtime(snap)
    clock(_day(2026, 10, 2))
    assert bs.main(["--out", str(out)]) == 0                   # not due for 7 days: the damage is not seen yet
    assert "integrity scan not due" in capsys.readouterr().out
    clock(_day(2026, 10, 3))
    assert bs.main(["--out", str(out), "--scan-now"]) == 1
    text = capsys.readouterr()
    assert f"integrity scan FAILED: {snap.name}: " in text.err
    assert "backup OK:" in text.out and len(list(out.glob("raw_*.duckdb"))) == 3     # tonight's snapshot was written
    assert snap.exists()


def test_scan_every_days_is_a_command_line_setting(rawstore_tmp_db, tmp_path, capsys, clock):
    _seed()
    out = tmp_path / "out"
    assert bs.main(["--out", str(out), "--scan-every-days", "2"]) == 0
    clock(_day(2026, 10, 3))
    assert bs.main(["--out", str(out), "--scan-every-days", "2"]) == 0
    assert capsys.readouterr().out.count("integrity scan OK:") == 2


# ── #309 part 3: the sizes of both backup folders, never by walking the off-machine one ──────────────────────────

def _local_bytes(out) -> int:
    return sum(p.stat().st_size for p in out.rglob("*") if p.is_file() and p.name != bs.SIZES_NAME)


def _drive_files(dest) -> dict:
    return {p.relative_to(dest).as_posix(): p.stat().st_size for p in dest.rglob("*") if p.is_file()}


def test_the_offmachine_manifest_holds_what_the_copy_wrote_and_kept(tmp_path, clock):
    docs, out = _docs_tree(tmp_path)
    _old_file(tmp_path / "media" / "900" / "a.png", b"A" * 64)
    mirror = tmp_path / "drive"
    dest = mirror / bs.OFFMACHINE_SUBDIR
    dest.mkdir(parents=True)
    (dest / "edgar_docs_20190101_000000_000000.zip").write_bytes(b"an older copy, from before the manifest")
    for i in range(4):
        clock(_day(2026, 10, 1 + i))
        (docs / "320193" / f"20{i}.html.gz").write_bytes(b"n" * (10 + i))
        bs.archive_edgar_docs(src=docs, out_dir=out, keep=5)
        bs.mirror_discord_media(src=tmp_path / "media", out_dir=out, settle_s=0)
        bs.offmachine_copy(out, mirror, big_every_days=0, keep_big=2)
    files = json.loads((out / bs.OFFMACHINE_MANIFEST_NAME).read_text(encoding="utf-8"))["files"]
    assert {k: v["size"] for k, v in files.items()} == _drive_files(dest)
    assert bs.backup_sizes(out, mirror).offmachine == sum(_drive_files(dest).values())


def test_files_already_in_the_offmachine_folder_are_counted_once_the_copy_keeps_them(tmp_path, clock):
    docs, out = _docs_tree(tmp_path)
    mirror = tmp_path / "drive"
    dest = mirror / bs.OFFMACHINE_SUBDIR
    dest.mkdir(parents=True)
    older = dest / "edgar_docs_20190101_000000_000000.zip"
    older.write_bytes(b"an older copy, from before the manifest")
    bs.archive_edgar_docs(src=docs, out_dir=out, keep=2)
    bs.offmachine_copy(out, mirror, big_every_days=0, keep_big=2)
    assert bs.backup_sizes(out, mirror).offmachine == sum(_drive_files(dest).values())


def test_the_size_step_never_lists_or_stats_the_offmachine_folder(tmp_path, monkeypatch):
    docs, out = _docs_tree(tmp_path)
    mirror = tmp_path / "drive"
    mirror.mkdir()
    bs.archive_edgar_docs(src=docs, out_dir=out, keep=2)
    bs.offmachine_copy(out, mirror, big_every_days=0)
    drive = str(mirror)

    def guard(fn):
        def guarded(p=".", *a, **k):
            if not isinstance(p, int) and os.fspath(p).startswith(drive):
                raise AssertionError(f"the size step touched the off-machine folder: {p}")
            return fn(p, *a, **k)
        return guarded

    for name in ("scandir", "listdir", "stat", "lstat", "walk"):
        monkeypatch.setattr(os, name, guard(getattr(os, name)))
    sizes = bs.backup_sizes(out, mirror)
    monkeypatch.undo()
    assert sizes.local == _local_bytes(out)
    assert sizes.offmachine == sum(_drive_files(mirror / bs.OFFMACHINE_SUBDIR).values())


def test_the_run_logs_both_folder_sizes(rawstore_tmp_db, tmp_path, capsys, monkeypatch, clock):
    from RevenueChaser.core import config
    _seed()
    out, mirror = tmp_path / "out", tmp_path / "drive"
    mirror.mkdir()
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", str(mirror))
    assert bs.main(["--out", str(out)]) == 0
    m = re.search(r"^backup sizes (\S+): local (\d+) bytes, off-machine (\d+) bytes$", capsys.readouterr().out, re.M)
    assert m, "no sizes line"
    assert int(m.group(2)) == _local_bytes(out)
    assert int(m.group(3)) == sum(_drive_files(mirror / bs.OFFMACHINE_SUBDIR).values()) > 0
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", "")
    assert bs.main(["--out", str(out)]) == 0
    out_text = capsys.readouterr().out
    assert re.search(r"^backup sizes \S+: local \d+ bytes$", out_text, re.M) and "off-machine" not in out_text
    assert json.loads((out / bs.SIZES_NAME).read_text(encoding="utf-8"))["offmachine_state"] == "not set"


# ── #309 review C (2026-10-17): a lost manifest is never a silent pass, bookkeeping never fails a backup ─────────

def _scan_twice_after(backed_up, clock, damage):
    out = backed_up["out"]
    assert bs.integrity_scan(out, now=True).failures == ()
    _flip_byte_keeping_size_and_mtime(backed_up["media"])
    damage(out / bs.MANIFEST_NAME)
    res = bs.integrity_scan(out, now=True)
    marker = json.loads((out / bs.SCAN_MARKER_NAME).read_text(encoding="utf-8"))
    clock(_day(2026, 10, 2))
    again = bs.integrity_scan(out)                       # due again the next night: passes, the warning is cleared
    return res, marker, again


def test_a_corrupt_manifest_is_reported_once(backed_up, clock):
    res, marker, again = _scan_twice_after(backed_up, clock, lambda f: f.write_text("{x", encoding="utf-8"))
    assert [r for r, _ in res.failures] == [bs.MANIFEST_NAME] and "unreadable" in res.failures[0][1]
    assert marker["ok"] is False
    assert again.ran and again.failures == ()


def test_a_missing_manifest_after_a_scan_is_reported_once(backed_up, clock):
    res, marker, again = _scan_twice_after(backed_up, clock, lambda f: f.unlink())
    assert [r for r, _ in res.failures] == [bs.MANIFEST_NAME] and "missing" in res.failures[0][1]
    assert again.ran and again.failures == ()


def test_the_first_scan_without_a_manifest_is_not_a_failure(backed_up, clock):
    out = backed_up["out"]
    (out / bs.MANIFEST_NAME).unlink()                    # no marker yet: the first run after the merge
    assert bs.integrity_scan(out, now=True).failures == ()


def test_the_first_scan_hashes_noted_mirror_files_and_then_watches_them(tmp_path, clock):
    out = tmp_path / "out"
    _old_file(tmp_path / "media" / "900" / "a.png", b"A" * 64)
    bs.mirror_discord_media(src=tmp_path / "media", out_dir=out, settle_s=0)
    (out / bs.MANIFEST_NAME).unlink()                    # a mirror made before #309
    bs.mirror_discord_media(src=tmp_path / "media", out_dir=out, settle_s=0)
    key, f = f"{bs.MEDIA_MIRROR_DIRNAME}/900/a.png", out / bs.MEDIA_MIRROR_DIRNAME / "900" / "a.png"
    assert _manifest(out)[key] == {"size": 64, "sha256": None}
    res = bs.integrity_scan(out, now=True)
    assert res.failures == () and res.adopted == 1 and _manifest(out)[key]["sha256"] == _sha(f)
    _flip_byte_keeping_size_and_mtime(f)
    assert _failed(bs.integrity_scan(out, now=True)) == [key]


def test_the_scan_never_deletes_a_file_it_cannot_adopt(backed_up, clock):
    out = backed_up["out"]
    bad = out / "weekly" / "raw_20250113_020005_123456.duckdb"
    shutil.copy2(backed_up["snap"], bad)
    damage_data_block(bad)
    junk = out / "discord_media_20250101_000000_000000.zip"
    junk.write_bytes(b"not a zip")
    before = _tree_state(out)
    assert sorted(_failed(bs.integrity_scan(out, now=True))) == sorted([f"weekly/{bad.name}", junk.name])
    assert _tree_state(out) == before


@pytest.mark.parametrize("damage, why", [
    (lambda f: f.write_text('{"version": 1, "files": {', encoding="utf-8"), "unreadable"),
    (lambda f: f.unlink(), "missing"),
])
def test_a_lost_manifest_is_that_nights_scan_failure_and_every_step_still_runs(rawstore_tmp_db, tmp_path, capsys,
                                                                               clock, damage, why):
    _seed()
    out = tmp_path / "out"
    assert bs.main(["--out", str(out)]) == 0
    damage(out / bs.MANIFEST_NAME)
    clock(_day(2026, 10, 2))
    capsys.readouterr()
    assert bs.main(["--out", str(out)]) == 1                   # not due for 7 days, but a lost manifest is due now
    text = capsys.readouterr()
    assert f"backup manifest WARNING: {out / bs.MANIFEST_NAME} is {why}" in text.err      # the first step says so
    assert f"integrity scan FAILED: {bs.MANIFEST_NAME}: the manifest was {why}" in text.err
    assert text.err.count(" FAILED") == 1 and "backup OK:" in text.out
    assert json.loads((out / bs.MANIFEST_NAME).read_text(encoding="utf-8")).get("lost") is None
    clock(_day(2026, 10, 3))                                   # due again after a failure: passes, reported once
    assert bs.main(["--out", str(out)]) == 0
    assert "integrity scan OK:" in capsys.readouterr().out


def test_a_failed_manifest_save_leaves_the_old_manifest(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    m = bs._Manifest(out / bs.MANIFEST_NAME, out, {"a": {"size": 1, "sha256": "x"}})
    m.save()
    before = (out / bs.MANIFEST_NAME).read_bytes()
    m.files["b"] = {"size": 2, "sha256": "y"}

    def refuse(a, b):
        raise OSError("disk")

    monkeypatch.setattr(bs.os, "replace", refuse)
    with pytest.raises(bs.BackupError):
        m.save()
    assert (out / bs.MANIFEST_NAME).read_bytes() == before
    assert not (out / (bs.MANIFEST_NAME + ".tmp")).exists()


def test_a_run_killed_while_hashing_the_new_snapshot_leaves_no_missing_file(rawstore_tmp_db, tmp_path, monkeypatch,
                                                                             clock):
    _seed()
    out = tmp_path / "out"
    bs.snapshot(out_dir=out, keep=1)
    clock(_day(2026, 10, 2))
    rawstore.write_rows("watchlist", [{"ticker": "MSFT"}])

    class Killed(BaseException):                               # a Windows Update restart, the 2 h task limit
        pass

    def killed(p):
        raise Killed()

    sha = bs._sha256
    monkeypatch.setattr(bs, "_sha256", killed)
    with pytest.raises(Killed):
        bs.snapshot(out_dir=out, keep=1)
    monkeypatch.setattr(bs, "_sha256", sha)
    assert bs.integrity_scan(out, now=True).failures == ()     # nothing was pruned before the record


def test_a_refused_manifest_write_is_a_warning_not_a_failed_backup(rawstore_tmp_db, tmp_path, monkeypatch, capsys):
    _seed()
    out = tmp_path / "out"
    real = os.replace

    def refuse(a, b):
        if pathlib.Path(b).name == bs.MANIFEST_NAME:
            raise PermissionError("the manifest is locked")
        return real(a, b)

    monkeypatch.setattr(bs.os, "replace", refuse)
    snap = bs.snapshot(out_dir=out, keep=1)
    assert snap.is_file() and "backup manifest WARNING" in capsys.readouterr().err


def test_an_offmachine_failure_still_runs_the_scan_and_the_sizes(rawstore_tmp_db, tmp_path, capsys, monkeypatch,
                                                                  clock):
    from RevenueChaser.core import config
    _seed()
    out, mirror = tmp_path / "out", tmp_path / "drive"
    mirror.mkdir()
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", str(mirror))

    def offline(*a, **k):
        raise bs.BackupError("the Drive folder is offline")

    monkeypatch.setattr(bs, "offmachine_copy", offline)
    assert bs.main(["--out", str(out)]) == 1
    text = capsys.readouterr()
    assert "off-machine copy FAILED" in text.err and "integrity scan OK:" in text.out
    assert json.loads((out / bs.SCAN_MARKER_NAME).read_text(encoding="utf-8"))["ok"] is True
    assert (out / bs.SIZES_NAME).is_file() and "backup sizes " in text.out


def test_a_new_offmachine_folder_starts_its_own_manifest(tmp_path, clock):
    docs, out = _docs_tree(tmp_path)
    _old_file(tmp_path / "media" / "900" / "a.png", b"A" * 64)
    bs.archive_edgar_docs(src=docs, out_dir=out, keep=2)
    bs.mirror_discord_media(src=tmp_path / "media", out_dir=out, settle_s=0)
    old, new = tmp_path / "drive_old", tmp_path / "drive_new"
    old.mkdir()
    new.mkdir()
    bs.offmachine_copy(out, old, big_every_days=0)
    shutil.rmtree(out / bs.MEDIA_MIRROR_DIRNAME)               # so the new folder gets no media
    bs.offmachine_copy(out, new, big_every_days=0)             # RAW_STORE_BACKUP_MIRROR was changed
    files = json.loads((out / bs.OFFMACHINE_MANIFEST_NAME).read_text(encoding="utf-8"))["files"]
    assert {k: v["size"] for k, v in files.items()} == _drive_files(new / bs.OFFMACHINE_SUBDIR)
    assert bs.backup_sizes(out, new).offmachine == sum(_drive_files(new / bs.OFFMACHINE_SUBDIR).values())


def test_the_offmachine_manifest_never_carries_a_lost_flag(tmp_path, clock):
    docs, out = _docs_tree(tmp_path)
    bs.archive_edgar_docs(src=docs, out_dir=out, keep=2)
    assert bs.integrity_scan(out, now=True).ran                # a scan marker exists
    mirror = tmp_path / "drive"
    mirror.mkdir()
    bs.offmachine_copy(out, mirror, big_every_days=0)          # its manifest did not exist yet
    assert "lost" not in json.loads((out / bs.OFFMACHINE_MANIFEST_NAME).read_text(encoding="utf-8"))


def _lock_while(monkeypatch, name, *steps):
    """``os.replace`` onto ``name`` raises PermissionError while one of the module functions ``steps`` runs."""
    real, on = os.replace, []

    def refuse(a, b):
        if on and pathlib.Path(b).name == name:
            raise PermissionError(f"{name} is locked")
        return real(a, b)

    def during(fn):
        def run(*a, **k):
            on.append(1)
            try:
                return fn(*a, **k)
            finally:
                on.pop()
        return run

    monkeypatch.setattr(bs.os, "replace", refuse)
    for step in steps:
        monkeypatch.setattr(bs, step, during(getattr(bs, step)))


def test_a_refused_manifest_write_in_the_mirror_steps_is_a_warning_and_the_offmachine_copy_runs(
        rawstore_tmp_db, tmp_path, capsys, monkeypatch, clock):
    from RevenueChaser.core import config
    _seed()
    out, mirror = tmp_path / "out", tmp_path / "drive"
    mirror.mkdir()
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", str(mirror))
    _old_file(tmp_path / "media" / "900" / "a.png", b"A" * 64)
    monkeypatch.setattr(config, "DISCORD_MEDIA_DIR", tmp_path / "media")
    _lock_while(monkeypatch, bs.MANIFEST_NAME, "mirror_discord_media", "mirror_spx_gamma")
    assert bs.main(["--out", str(out)]) == 0
    text = capsys.readouterr()
    assert "backup manifest WARNING" in text.err and " FAILED" not in text.err
    assert "discord_media mirror OK:" in text.out and "off-machine copy OK" in text.out
    assert (out / bs.MEDIA_MIRROR_DIRNAME / "900" / "a.png").is_file()


def test_a_refused_offmachine_manifest_write_is_a_warning_not_a_failed_copy(rawstore_tmp_db, tmp_path, capsys,
                                                                             monkeypatch, clock):
    from RevenueChaser.core import config
    _seed()
    out, mirror = tmp_path / "out", tmp_path / "drive"
    mirror.mkdir()
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", str(mirror))
    _lock_while(monkeypatch, bs.OFFMACHINE_MANIFEST_NAME, "offmachine_copy")
    assert bs.main(["--out", str(out)]) == 0
    text = capsys.readouterr()
    assert "off-machine manifest WARNING" in text.err and " FAILED" not in text.err
    assert "off-machine copy OK" in text.out
    assert any(n.startswith("raw_") for n in _drive_files(mirror / bs.OFFMACHINE_SUBDIR))
