"""#276 (review B3): the SPX Gamma data tree is mirrored by the nightly backup, incrementally and verified
(the #248 mechanism). Temp trees only: no test here reads or writes the real tree."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from RevenueChaser.core import automations
from RevenueChaser.module_spx_gamma import config as spx_config
from RevenueChaser.scripts import backup_store as bs

_MIRROR = "spx_gamma_mirror"
_VERSIONS = "spx_gamma_versions"
_REAL_TREE = Path(bs.__file__).resolve().parents[1] / "module_spx_gamma" / "data"
_FILES = {
    "raw/2026-10-01/gex_levels.json": b'{"data": {"call_wall": "7900"}}',
    "raw/2026-10-01/greek_exposure.json": b'{"data": [1, 2, 3]}',
    "raw/2026-10-02/gex_levels.json": b'{"data": {"call_wall": "7950"}}',
    "brier.json": b'{"records": [{"date": "2026-10-01"}], "average": null, "n": 0}',
    "spx_gamma_gex_history.json": b'{"2026-10-01": {"spot": 7700.0}}',
}


def _put(src: Path, rel: str, data: bytes, age_s: int = 3600) -> Path:
    f = src / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    t = time.time() - age_s
    os.utime(f, (t, t))
    return f


def _tree(tmp_path: Path, files: dict | None = None) -> Path:
    src = tmp_path / "spx"
    for rel, data in (files if files is not None else _FILES).items():
        _put(src, rel, data)
    return src


def _counts(res):
    return res.copied, res.unchanged, res.skipped, res.total


def test_the_unit_suite_never_points_at_the_real_spx_tree():
    """G1 (2026-09-30 SPX review): a test once wrote into the real ``brier.json``. The root conftest
    redirects the tree, as it does ``.store`` and ``edgar_docs``, so a test that forgets to pass a
    root still cannot reach the real one."""
    root = Path(spx_config.DATA_ROOT).resolve()
    assert root != _REAL_TREE.resolve()
    assert _REAL_TREE.resolve() not in root.parents


def test_mirror_first_run_copies_every_file_byte_for_byte(tmp_path):
    src = _tree(tmp_path)
    out = tmp_path / "out"
    res = bs.mirror_spx_gamma(src=src, out_dir=out)
    assert _counts(res) == (5, 0, 0, 5)
    assert res.dest == out / _MIRROR
    for rel, data in _FILES.items():
        assert (out / _MIRROR / rel).read_bytes() == data


def test_mirror_incremental_run_copies_only_the_new_day(tmp_path, monkeypatch):
    src = _tree(tmp_path)
    out = tmp_path / "out"
    bs.mirror_spx_gamma(src=src, out_dir=out)
    _put(src, "raw/2026-10-03/gex_levels.json", b'{"data": {"call_wall": "8000"}}')
    copied = []
    real = bs._copy_file
    monkeypatch.setattr(bs, "_copy_file", lambda s, d: (copied.append(Path(s).parent.name), real(s, d))[1])
    res = bs.mirror_spx_gamma(src=src, out_dir=out)
    assert _counts(res) == (1, 5, 0, 6)
    assert copied == ["2026-10-03"]
    assert (out / _MIRROR / "raw" / "2026-10-03" / "gex_levels.json").read_bytes() == b'{"data": {"call_wall": "8000"}}'


def test_mirror_never_deletes_from_the_mirror(tmp_path):
    src = _tree(tmp_path)
    out = tmp_path / "out"
    bs.mirror_spx_gamma(src=src, out_dir=out)
    shutil.rmtree(src / "raw" / "2026-10-01")              # a day folder lost from the live tree
    res = bs.mirror_spx_gamma(src=src, out_dir=out)
    assert _counts(res) == (0, 3, 0, 3)
    assert (out / _MIRROR / "raw" / "2026-10-01" / "gex_levels.json").read_bytes() == _FILES["raw/2026-10-01/gex_levels.json"]


def test_mirror_detects_a_copy_that_differs_from_its_source_and_keeps_the_previous_one(tmp_path, monkeypatch):
    src = _tree(tmp_path)
    out = tmp_path / "out"
    bs.mirror_spx_gamma(src=src, out_dir=out)
    _put(src, "raw/2026-10-02/gex_levels.json", b'{"data": {"call_wall": "8123"}}', age_s=1800)

    def corrupting_copy(s, d):
        shutil.copy2(s, d)
        data = bytearray(Path(d).read_bytes())
        data[3] ^= 0x01                                    # same size, one flipped bit
        Path(d).write_bytes(bytes(data))

    monkeypatch.setattr(bs, "_copy_file", corrupting_copy)
    with pytest.raises(bs.BackupError, match="gex_levels.json"):
        bs.mirror_spx_gamma(src=src, out_dir=out)
    assert (out / _MIRROR / "raw" / "2026-10-02" / "gex_levels.json").read_bytes() == _FILES["raw/2026-10-02/gex_levels.json"]
    assert list((out / _MIRROR / ".tmp").glob("*")) == []


def test_mirror_leaves_a_file_still_being_written_for_the_next_run(tmp_path):
    src = _tree(tmp_path)
    _put(src, "raw/2026-10-03/gex_levels.json", b"{", age_s=30)
    out = tmp_path / "out"
    res = bs.mirror_spx_gamma(src=src, out_dir=out)
    assert _counts(res) == (5, 0, 1, 6)
    assert not (out / _MIRROR / "raw" / "2026-10-03").exists()


def test_mirror_is_none_for_a_missing_or_empty_tree_and_creates_nothing(tmp_path):
    out = tmp_path / "out"
    assert bs.mirror_spx_gamma(src=tmp_path / "nope", out_dir=out) is None
    (tmp_path / "empty" / "raw").mkdir(parents=True)
    assert bs.mirror_spx_gamma(src=tmp_path / "empty", out_dir=out) is None
    assert not (out / _MIRROR).exists()


def test_mirror_defaults_to_the_spx_module_data_root(tmp_path, monkeypatch):
    src = _tree(tmp_path)
    monkeypatch.setattr(spx_config, "DATA_ROOT", src)
    res = bs.mirror_spx_gamma(out_dir=tmp_path / "out")
    assert res.total == 5


def test_spx_mirror_name_does_not_collide_with_a_snapshot(rawstore_tmp_db, tmp_path):
    from RevenueChaser.scripts import restore_store as rs
    out = tmp_path / "out"
    bs.mirror_spx_gamma(src=_tree(tmp_path), out_dir=out)
    assert (out / _MIRROR).is_dir()
    assert rs.snapshots(out) == []


def test_main_mirrors_the_spx_tree_and_logs_one_line(rawstore_tmp_db, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(spx_config, "DATA_ROOT", _tree(tmp_path))
    out = tmp_path / "out"
    args = list(automations.entry("RC-Store-Backup").args[2:])      # what the installed task passes
    assert bs.main(args + ["--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "spx mirror OK: 5 copied, 0 unchanged, 0 in flight, 5 files" in text
    assert str(out / _MIRROR) in text
    assert (out / _MIRROR / "brier.json").read_bytes() == _FILES["brier.json"]
    capsys.readouterr()
    assert bs.main(args + ["--out", str(out)]) == 0
    assert "spx mirror OK: 0 copied, 5 unchanged, 0 in flight, 5 files" in capsys.readouterr().out


def test_main_with_no_spx_tree_returns_0_and_creates_no_mirror(rawstore_tmp_db, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(spx_config, "DATA_ROOT", tmp_path / "no-such-dir")
    out = tmp_path / "out"
    assert bs.main(["--out", str(out)]) == 0
    assert "spx mirror" not in capsys.readouterr().out
    assert not (out / _MIRROR).exists()


def test_main_reports_an_spx_mirror_failure_after_the_db_snapshots(rawstore_tmp_db, tmp_path, capsys, monkeypatch):
    def boom(*a, **k):
        raise bs.BackupError("hash mismatch for raw/2026-10-02/gex_levels.json")

    monkeypatch.setattr(bs, "mirror_spx_gamma", boom)
    out = tmp_path / "out"
    assert bs.main(["--out", str(out)]) == 1
    assert "spx backup FAILED: hash mismatch for raw/2026-10-02/gex_levels.json" in capsys.readouterr().err
    assert list(out.glob("raw_*.duckdb"))                  # the DB snapshots were already written


# --- Q5 (avo, 2026-10-04): a corrupt in-place file never replaces a good mirror copy; the two files that are
# --- rewritten in place keep a dated copy of every version ---------------------------------------------------

_BRIER_V2 = b'{"records": [{"date": "2026-10-01"}, {"date": "2026-10-02"}], "average": null, "n": 0}'
_BRIER_TORN = b'{"records": [{"date": "2026-10-01"}, {"da'          # a torn write (the S-1 shape)


def _versions(out: Path, stem: str) -> list[Path]:
    return sorted((out / _VERSIONS).glob(f"{stem}.*.json"))


def test_a_corrupt_brier_json_never_replaces_a_good_mirror_copy(tmp_path):
    src = _tree(tmp_path)
    out = tmp_path / "out"
    bs.mirror_spx_gamma(src=src, out_dir=out)
    _put(src, "brier.json", _BRIER_TORN, age_s=1800)
    res = bs.mirror_spx_gamma(src=src, out_dir=out)
    assert res.rejected == ("brier.json",) and res.copied == 0
    assert (out / _MIRROR / "brier.json").read_bytes() == _FILES["brier.json"]
    assert list((out / _MIRROR / ".tmp").glob("*")) == []
    assert [p.read_bytes() for p in _versions(out, "brier")] == [_FILES["brier.json"]]      # nothing dated for the torn file
    _put(src, "brier.json", _BRIER_V2, age_s=900)                        # the source is repaired
    res = bs.mirror_spx_gamma(src=src, out_dir=out)
    assert res.rejected == () and res.copied == 1
    assert (out / _MIRROR / "brier.json").read_bytes() == _BRIER_V2


def test_the_guard_rejects_bytes_that_are_not_utf8_json_too(tmp_path):
    src = _tree(tmp_path)
    out = tmp_path / "out"
    bs.mirror_spx_gamma(src=src, out_dir=out)
    _put(src, "spx_gamma_gex_history.json", b"\xff\xfe\x00garbage", age_s=1800)
    assert bs.mirror_spx_gamma(src=src, out_dir=out).rejected == ("spx_gamma_gex_history.json",)
    assert (out / _MIRROR / "spx_gamma_gex_history.json").read_bytes() == _FILES["spx_gamma_gex_history.json"]


def test_a_corrupt_json_with_no_good_copy_yet_is_still_backed_up(tmp_path):
    src = _tree(tmp_path, {"brier.json": _BRIER_TORN})
    out = tmp_path / "out"
    res = bs.mirror_spx_gamma(src=src, out_dir=out)
    assert res.rejected == () and res.copied == 1                         # nothing good to protect: keep the bytes
    assert (out / _MIRROR / "brier.json").read_bytes() == _BRIER_TORN
    assert _versions(out, "brier") == []                                  # but it is not a version worth dating
    _put(src, "brier.json", _BRIER_V2, age_s=900)
    assert bs.mirror_spx_gamma(src=src, out_dir=out).copied == 1          # a repaired file replaces it


def test_each_version_of_the_two_in_place_files_keeps_a_dated_copy(tmp_path):
    src = _tree(tmp_path)
    out = tmp_path / "out"
    bs.mirror_spx_gamma(src=src, out_dir=out)
    assert [p.read_bytes() for p in _versions(out, "brier")] == [_FILES["brier.json"]]
    assert len(_versions(out, "spx_gamma_gex_history")) == 1
    bs.mirror_spx_gamma(src=src, out_dir=out)                             # unchanged: no new dated copy
    assert len(_versions(out, "brier")) == 1
    _put(src, "brier.json", _BRIER_V2, age_s=1800)
    bs.mirror_spx_gamma(src=src, out_dir=out)
    versions = _versions(out, "brier")
    assert [p.read_bytes() for p in versions] == [_FILES["brier.json"], _BRIER_V2]    # oldest first (UTC stamp in the name)
    assert (out / _MIRROR / "brier.json").read_bytes() == _BRIER_V2
    assert len(_versions(out, "spx_gamma_gex_history")) == 1              # the other file did not change


def test_the_dated_copy_is_named_by_the_source_mtime_in_utc(tmp_path):
    src = _tree(tmp_path, {"brier.json": _FILES["brier.json"]})
    f = src / "brier.json"
    t = 1_790_000_000                                                     # 2026-09-21T14:13:20Z
    os.utime(f, (t, t))
    out = tmp_path / "out"
    bs.mirror_spx_gamma(src=src, out_dir=out)
    assert [p.name for p in _versions(out, "brier")] == ["brier.20260921T141320_000000Z.json"]


def test_only_the_two_in_place_files_are_versioned(tmp_path):
    src = _tree(tmp_path)
    out = tmp_path / "out"
    bs.mirror_spx_gamma(src=src, out_dir=out)
    _put(src, "raw/2026-10-02/gex_levels.json", b'{"data": {"call_wall": "9000"}}', age_s=1800)
    bs.mirror_spx_gamma(src=src, out_dir=out)
    assert sorted(p.name.split(".")[0] for p in (out / _VERSIONS).glob("*")) == ["brier", "spx_gamma_gex_history"]
    assert bs.SPX_VERSIONED_FILES == ("brier.json", "spx_gamma_gex_history.json")


def test_dated_copies_sit_beside_the_mirror_so_a_restore_of_the_mirror_does_not_carry_them(tmp_path):
    src = _tree(tmp_path)
    out = tmp_path / "out"
    bs.mirror_spx_gamma(src=src, out_dir=out)
    assert (out / _VERSIONS).is_dir() and _VERSIONS != _MIRROR
    assert sorted(p.relative_to(out / _MIRROR).as_posix() for p in (out / _MIRROR).rglob("*") if p.is_file()) == sorted(_FILES)


def test_dated_copies_stay_when_the_source_is_lost(tmp_path):
    src = _tree(tmp_path)
    out = tmp_path / "out"
    bs.mirror_spx_gamma(src=src, out_dir=out)
    (src / "brier.json").unlink()
    bs.mirror_spx_gamma(src=src, out_dir=out)
    assert [p.read_bytes() for p in _versions(out, "brier")] == [_FILES["brier.json"]]
    assert (out / _MIRROR / "brier.json").read_bytes() == _FILES["brier.json"]


def test_the_json_guard_belongs_to_the_spx_tree_not_to_every_mirror(tmp_path):
    """``json_guard`` is an argument of ``_mirror_tree``, off by default: only the SPX tree passes it.
    Another tree's ``.json`` file that stops being valid JSON is mirrored like any other file."""
    src, dest = tmp_path / "other", tmp_path / "out" / "other_mirror"
    _put(src, "900/notes.json", b'{"valid": true}')
    assert bs._mirror_tree(src, dest, 600.0, "other mirror").copied == 1
    _put(src, "900/notes.json", b"this file is just text", age_s=1800)
    res = bs._mirror_tree(src, dest, 600.0, "other mirror")
    assert res.copied == 1 and res.rejected == ()
    assert (dest / "900" / "notes.json").read_bytes() == b"this file is just text"


def test_main_keeps_going_after_a_rejected_file_and_exits_red(rawstore_tmp_db, tmp_path, capsys, monkeypatch):
    src = _tree(tmp_path)
    monkeypatch.setattr(spx_config, "DATA_ROOT", src)
    out = tmp_path / "out"
    assert bs.main(["--out", str(out)]) == 0
    capsys.readouterr()
    _put(src, "brier.json", _BRIER_TORN, age_s=1800)
    assert bs.main(["--out", str(out)]) == 1
    cap = capsys.readouterr()
    assert "spx mirror OK:" in cap.out
    assert "spx mirror WARNING: kept the previous copy of 1 file(s) whose source is not valid JSON: brier.json" in cap.err
    assert (out / _MIRROR / "brier.json").read_bytes() == _FILES["brier.json"]


def test_a_damaged_source_replaces_a_mirror_copy_that_is_already_damaged(tmp_path):
    src = _tree(tmp_path, {"brier.json": _BRIER_TORN})
    out = tmp_path / "out"
    bs.mirror_spx_gamma(src=src, out_dir=out)
    worse = b'{"records": [{"da'
    _put(src, "brier.json", worse, age_s=1800)
    res = bs.mirror_spx_gamma(src=src, out_dir=out)
    assert res.rejected == () and res.copied == 1                         # nothing good is lost, so nothing is held back
    assert (out / _MIRROR / "brier.json").read_bytes() == worse


def test_a_dated_copy_is_never_overwritten(tmp_path):
    src = _tree(tmp_path, {"brier.json": _FILES["brier.json"]})
    f = src / "brier.json"
    t = time.time() - 3600
    os.utime(f, (t, t))
    out = tmp_path / "out"
    bs.mirror_spx_gamma(src=src, out_dir=out)
    f.write_bytes(_BRIER_V2)                                              # a different version with the same mtime
    os.utime(f, (t, t))
    assert bs.mirror_spx_gamma(src=src, out_dir=out).copied == 1
    assert (out / _MIRROR / "brier.json").read_bytes() == _BRIER_V2
    assert [p.read_bytes() for p in _versions(out, "brier")] == [_FILES["brier.json"]]


def test_a_dated_copy_that_does_not_match_the_verified_copy_is_discarded_and_the_run_fails(tmp_path, monkeypatch):
    """Review L2: the dated copy is a second write; a bad write there must not be kept under a dated name."""
    src = _tree(tmp_path, {"brier.json": _FILES["brier.json"]})
    out = tmp_path / "out"
    real = shutil.copyfile

    def damaged(s, d, *a, **k):                            # copy2 calls copyfile too: damage only the dated copy
        real(s, d, *a, **k)
        if Path(d).parent.name == _VERSIONS:
            data = bytearray(Path(d).read_bytes())
            data[3] ^= 0x01                                # same size, one flipped bit
            Path(d).write_bytes(bytes(data))
        return d

    monkeypatch.setattr(bs.shutil, "copyfile", damaged)
    with pytest.raises(bs.BackupError, match="dated copy"):
        bs.mirror_spx_gamma(src=src, out_dir=out)
    assert list((out / _VERSIONS).glob("*")) == []         # neither the dated name nor a leftover .part
    assert not (out / _MIRROR / "brier.json").exists()     # the file waits for the next run, nothing half-done
