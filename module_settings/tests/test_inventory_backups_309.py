"""#309: the Backups card of Settings / Data health reads the backup job's own records (sizes and the last
integrity scan), never the backup folders themselves and never the off-machine folder."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from RevenueChaser.core import config
from RevenueChaser.module_settings import inventory
from RevenueChaser.scripts import backup_store as bs

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
_SIZES = {"recorded_at": "2026-10-16T04:05:00+00:00", "local": 12_782_917_784, "offmachine": 7_676_019_838,
          "offmachine_state": "ok", "local_files": 3382, "duckdb_bytes": 9_589_997_568, "duckdb_files": 21,
          "docs_zip_bytes": 2_668_865_089, "docs_zip_files": 2, "offmachine_files": 3375}
_SCAN = {"last_day": "2026-10-16", "at": "2026-10-16T04:04:00+00:00", "ok": True, "checked": 3382, "failed": 0,
         "adopted": 0, "bytes": 12_782_000_000, "every_days": 7, "next_due": "2026-10-23"}


@pytest.fixture
def backups(monkeypatch, tmp_path):
    out = tmp_path / "backups"
    out.mkdir()
    monkeypatch.setenv("RAW_STORE_BACKUP_DIR", str(out))
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_WARN_GB", 20.0)
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR_WARN_GB", 320.0)
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", str(tmp_path / "drive"))

    def write(sizes=None, scan=None):
        if sizes is not None:
            (out / bs.SIZES_NAME).write_text(json.dumps(sizes), encoding="utf-8")
        if scan is not None:
            (out / bs.SCAN_MARKER_NAME).write_text(json.dumps(scan), encoding="utf-8")
        return inventory._backups(NOW)

    return write


def test_the_card_carries_both_folder_sizes_the_split_and_when_they_were_recorded(backups):
    b = backups(_SIZES, _SCAN)
    assert b["recorded_at"] == "2026-10-16T04:05:00+00:00"
    loc = b["local"]
    assert loc["state"] == "ok" and loc["bytes"] == 12_782_917_784 and loc["files"] == 3382
    assert (loc["duckdb_bytes"], loc["duckdb_files"], loc["docs_zip_bytes"], loc["docs_zip_files"]) == (
        9_589_997_568, 21, 2_668_865_089, 2)
    assert loc["limit_gb"] == 20.0
    off = b["offmachine"]
    assert off["state"] == "ok" and off["bytes"] == 7_676_019_838 and off["files"] == 3375 and off["limit_gb"] == 320.0


def test_the_checkpoints_folder_is_unknown_because_the_job_does_not_write_it(backups):
    cp = backups(_SIZES, _SCAN)["checkpoints"]
    assert cp["state"] == "unknown" and "not recorded" in cp["text"]


def test_the_last_scan_its_result_and_the_next_due_day(backups):
    s = backups(_SIZES, _SCAN)["scan"]
    assert (s["state"], s["last_day"], s["failed"], s["next_due"]) == ("ok", "2026-10-16", 0, "2026-10-23")
    bad = backups(_SIZES, dict(_SCAN, ok=False, failed=2, next_due="2026-10-17"))["scan"]
    assert bad["state"] == "bad" and bad["failed"] == 2 and bad["next_due"] == "2026-10-17"
    assert "2 files do not match" in bad["text"]


def test_a_folder_above_its_threshold_warns(backups):
    b = backups(dict(_SIZES, local=20_000_000_001, offmachine=320_000_000_001), _SCAN)
    assert b["local"]["state"] == "warn" and b["offmachine"]["state"] == "warn"
    at = backups(dict(_SIZES, local=20_000_000_000, offmachine=320_000_000_000), _SCAN)
    assert at["local"]["state"] == "ok" and at["offmachine"]["state"] == "ok"      # at the threshold: not above


def test_missing_records_are_unknown_not_zero(backups):
    b = backups()
    assert b["recorded_at"] is None
    assert b["local"]["state"] == "unknown" and b["local"]["bytes"] is None
    assert b["offmachine"]["state"] == "unknown" and b["offmachine"]["bytes"] is None
    assert b["scan"]["state"] == "unknown" and b["scan"]["next_due"] is None


def test_an_unset_or_unrecorded_offmachine_folder_says_so(backups, monkeypatch):
    assert backups(dict(_SIZES, offmachine=None, offmachine_state="unknown"), _SCAN)["offmachine"]["state"] == "unknown"
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", "")
    off = backups(dict(_SIZES, offmachine=None, offmachine_state="not set", offmachine_files=None), _SCAN)["offmachine"]
    assert off["state"] == "not set" and "RAW_STORE_BACKUP_MIRROR" in off["text"]


def test_an_unreadable_record_is_unknown(backups, tmp_path):
    (tmp_path / "backups" / bs.SIZES_NAME).write_text("{not json", encoding="utf-8")
    assert backups()["local"]["state"] == "unknown"


def test_a_failure_reading_the_records_is_recorded_and_never_fails_the_page(rawstore_tmp_db, backups, monkeypatch):
    def boom(*a, **k):
        raise OSError("backup folder unreadable")

    monkeypatch.setattr(bs, "backup_facts", boom)
    payload = inventory.collect(NOW)
    assert payload["backups"]["local"]["state"] == "unknown"
    assert any(e["where"] == "_backups" for e in payload["errors"])


def test_the_card_never_touches_the_offmachine_folder(backups, monkeypatch, tmp_path):
    drive = str(tmp_path / "drive")

    def guard(fn):
        def guarded(p=".", *a, **k):
            if not isinstance(p, int) and os.fspath(p).startswith(drive):
                raise AssertionError(f"the Settings page touched the off-machine folder: {p}")
            return fn(p, *a, **k)
        return guarded

    (tmp_path / "backups" / bs.SIZES_NAME).write_text(json.dumps(_SIZES), encoding="utf-8")
    for name in ("scandir", "listdir", "stat", "lstat", "walk"):
        monkeypatch.setattr(os, name, guard(getattr(os, name)))
    b = inventory._backups(NOW)
    monkeypatch.undo()
    assert b["offmachine"]["bytes"] == 7_676_019_838


def test_collect_carries_the_card(rawstore_tmp_db, backups):
    backups(_SIZES, _SCAN)
    assert inventory.collect(NOW)["backups"]["local"]["bytes"] == 12_782_917_784


def test_the_card_carries_the_offmachine_location_the_job_copies_to(backups, monkeypatch, tmp_path):
    """G8 (#124): the job's own record of where the off-machine copy goes, read as text and never walked."""
    assert backups(_SIZES, _SCAN)["offmachine_path"] == str(tmp_path / "drive")
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_MIRROR", "")
    assert backups(_SIZES, _SCAN)["offmachine_path"] is None
