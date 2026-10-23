"""scripts/clean_test_scratch.py: the broom for scratch a killed pytest run left behind.

The one thing that must never happen is removing the scratch of a run in progress, so every case here
is about which directories the script is willing to name.
"""
from __future__ import annotations

import os
import time

import pytest

from RevenueChaser.scripts import clean_test_scratch as mod


def _aged(path, hours):
    """Backdate a directory and everything in it, so the age rule has something to see."""
    when = time.time() - hours * 3600
    for p in [path, *path.rglob("*")]:
        os.utime(p, (when, when))


def test_a_directory_older_than_the_cutoff_is_named(tmp_path):
    old = tmp_path / "rc-pytest-abc123"
    (old / "sub").mkdir(parents=True)
    (old / "sub" / "raw.duckdb").write_bytes(b"x")
    _aged(old, 5)
    assert mod.stale_dirs(tmp_path, min_age_hours=2) == [old]


def test_a_run_in_progress_is_left_alone(tmp_path):
    """The whole point: a fresh directory is never a candidate, however it is named."""
    fresh = tmp_path / "rc-pytest-running"
    fresh.mkdir()
    (fresh / "raw.duckdb").write_bytes(b"x")
    assert mod.stale_dirs(tmp_path, min_age_hours=2) == []


def test_a_recent_write_deep_in_an_old_tree_protects_it(tmp_path):
    """The directory's own mtime does not change when a test writes a file inside it."""
    d = tmp_path / "rc-pytest-deep"
    (d / "sub").mkdir(parents=True)
    _aged(d, 5)
    (d / "sub" / "fresh.duckdb").write_bytes(b"x")  # a live test writing now
    assert mod.stale_dirs(tmp_path, min_age_hours=2) == []


def test_only_the_two_known_prefixes_are_touched(tmp_path):
    keep = []
    for name in ("important-data", "rc-bench-xyz", ".store", "rcpytest-nodash"):
        p = tmp_path / name
        p.mkdir()
        keep.append(p)
    for name in ("rc-pytest-a", "pytest-of-avozp"):
        (tmp_path / name).mkdir()
    for p in [*keep, tmp_path / "rc-pytest-a", tmp_path / "pytest-of-avozp"]:
        _aged(p, 5)
    assert sorted(p.name for p in mod.stale_dirs(tmp_path, min_age_hours=2)) == [
        "pytest-of-avozp", "rc-pytest-a"]


def test_a_file_that_merely_shares_the_prefix_is_not_a_directory(tmp_path):
    f = tmp_path / "rc-pytest-notadir"
    f.write_bytes(b"x")
    _aged(tmp_path, 5)
    assert mod.stale_dirs(tmp_path, min_age_hours=2) == []


def test_the_default_run_reports_and_removes_nothing(tmp_path, capsys):
    d = tmp_path / "rc-pytest-old"
    d.mkdir()
    (d / "f").write_bytes(b"x" * 2048)
    _aged(d, 5)
    assert mod.main(["--temp-dir", str(tmp_path)]) == 0
    assert d.exists(), "a run without --apply must not delete anything"
    assert "report only" in capsys.readouterr().out


def test_apply_removes_the_named_directories(tmp_path, capsys):
    old, fresh = tmp_path / "rc-pytest-old", tmp_path / "rc-pytest-new"
    old.mkdir()
    (old / "f").write_bytes(b"x")
    _aged(old, 5)
    fresh.mkdir()
    assert mod.main(["--temp-dir", str(tmp_path), "--apply"]) == 0
    assert not old.exists() and fresh.exists()
    assert "removed 1" in capsys.readouterr().out


def test_an_empty_temp_dir_is_not_an_error(tmp_path, capsys):
    assert mod.main(["--temp-dir", str(tmp_path)]) == 0
    assert "nothing to clean" in capsys.readouterr().out


def test_a_missing_temp_dir_is_not_an_error(tmp_path):
    assert mod.stale_dirs(tmp_path / "gone", min_age_hours=2) == []
