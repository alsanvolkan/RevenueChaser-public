"""#147 G9: the cleanup dry run has no delete in its code path at all.

``scripts/tests/test_backup_store.py`` already compares the folder before and after the scan. This file pins the
stronger promise avo asked for: while the scan runs, every removal call in the interpreter raises, so a future edit
that removes a file — inside the backup folder or anywhere else — fails here instead of in avo's backups.
"""
from __future__ import annotations

import os
import pathlib
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from RevenueChaser.scripts import backup_store as bs


NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def _aged(folder: pathlib.Path, name: str, size: int = 100, days_old: int = 0) -> pathlib.Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes(b"x" * size)
    when = (NOW - timedelta(days=days_old)).timestamp()
    os.utime(p, (when, when))
    return p


def test_no_removal_call_is_reachable_from_the_dry_run(tmp_path: pathlib.Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "local"
    for n in range(7):                                  # raw keeps 5, so two copies are expired
        _aged(out, f"raw_2026090{n + 1}_000000_000000.duckdb", 1000, 30 - n)
    _aged(out / bs.WEEKLY_DIRNAME, "raw_20260601_000000_000000.duckdb", 700, 60)
    (out / bs.MANIFEST_NAME).write_text("{}", encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("the cleanup dry run must not remove anything")

    monkeypatch.setattr(pathlib.Path, "unlink", forbidden)
    monkeypatch.setattr(pathlib.Path, "rmdir", forbidden)
    monkeypatch.setattr(pathlib.Path, "rename", forbidden)
    monkeypatch.setattr(pathlib.Path, "replace", forbidden)
    monkeypatch.setattr(os, "remove", forbidden)
    monkeypatch.setattr(os, "unlink", forbidden)
    monkeypatch.setattr(os, "rmdir", forbidden)
    monkeypatch.setattr(os, "truncate", forbidden)
    monkeypatch.setattr(shutil, "rmtree", forbidden)
    monkeypatch.setattr(shutil, "move", forbidden)

    found = bs.expired_backups(out, now=NOW)
    assert found.state == "ok" and found.count == 2
