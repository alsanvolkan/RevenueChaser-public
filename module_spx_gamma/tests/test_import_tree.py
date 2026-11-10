"""``--import-tree`` (SPX Gamma review §7): the old tree goes into ``boards/spx_gamma.duckdb`` byte-exact, a dry run
writes nothing, a second run writes nothing, and the tree itself is never touched."""
from __future__ import annotations

import hashlib
import json

from RevenueChaser.module_spx_gamma import import_tree as it, store
from RevenueChaser.module_spx_gamma.config import BRIER_FILENAME, HISTORY_FILENAME

GEX = '{"data": {"call_wall": "9999"}}'


def _tree(root):
    day = root / "raw" / "2026-08-13"
    day.mkdir(parents=True)
    (day / "gex_levels.json").write_text(GEX, encoding="utf-8")
    (day / "manifest.json").write_text(json.dumps({
        "asof_date": "2026-08-13", "sha256": {"gex_levels.json": hashlib.sha256(GEX.encode()).hexdigest()}}))
    (root / "raw" / "2026-08-14").mkdir()                                   # no manifest: not a day
    (root / "raw" / ".partial-1-x-2026-08-15").mkdir()
    (root / BRIER_FILENAME).write_text(json.dumps({"records": [
        {"date": "2026-08-13", "scenario_probs": {"Upside": 1.0}, "outcome": "Upside", "brier": 0.0}],
        "average": 0.0, "n": 1}))
    # 12 days: the old file kept 10, the extra file still holds the two it pruned
    days = [f"2026-07-{d:02d}" for d in range(1, 13)]
    (root / HISTORY_FILENAME).write_text(json.dumps({d: {"spot": 7000.0} for d in days[2:]}))
    extra = root / "versions_copy.json"
    extra.write_text(json.dumps({d: {"spot": 6000.0} for d in days}))
    (root / "latest.json").write_text(json.dumps({"asof_date": "2026-08-13"}))
    (root / "brier.json.corrupt-20261101").write_text("{")
    return extra, days


def _snapshot(root):
    return sorted((p.relative_to(root).as_posix(), p.read_bytes()) for p in root.rglob("*") if p.is_file())


def test_a_dry_run_reports_and_writes_nothing(tmp_path):
    src = tmp_path / "tree"
    extra, _ = _tree(src)
    out = it.import_tree(src, extra_gex=[extra])
    assert out["status"] == "dry_run"
    raw = out["spx_raw_days"]
    assert (raw["days_read"], raw["written"], raw["files"]) == (1, 1, 1)
    assert raw["not_days"] == ["2026-08-14"] and raw["partial"] == [".partial-1-x-2026-08-15"]
    assert raw["sha_mismatch"] == []
    assert out["spx_brier"]["written"] == 1 and out["spx_brier"]["recomputed_n"] == 1
    assert out["spx_gex_daily"]["written"] == 12
    assert out["corrupt"] == ["brier.json.corrupt-20261101"]
    assert store.stored_days() == [] and store.brier_records() == [] and store.gex_days() == {}
    assert store.latest() is None


def test_apply_is_byte_exact_keeps_every_gex_day_and_leaves_the_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "rebuild_cache", lambda: [])
    src = tmp_path / "tree"
    extra, days = _tree(src)
    before = _snapshot(src)
    it.import_tree(src, apply=True, extra_gex=[extra])
    assert store.raw_files("2026-08-13") == {"gex_levels.json": GEX}
    assert store.raw_manifest("2026-08-13")["asof_date"] == "2026-08-13"
    assert [r["date"] for r in store.brier_records()] == ["2026-08-13"]
    gex = store.gex_days()
    assert sorted(gex) == days
    assert gex[days[0]]["spot"] == 6000.0 and gex[days[5]]["spot"] == 7000.0     # the history file wins
    assert store.latest() == {"asof_date": "2026-08-13"}
    assert _snapshot(src) == before, "the import changed the tree"


def test_a_second_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "rebuild_cache", lambda: [])
    src = tmp_path / "tree"
    extra, _ = _tree(src)
    it.import_tree(src, apply=True, extra_gex=[extra])
    again = it.import_tree(src, apply=True, extra_gex=[extra])
    assert again["spx_raw_days"]["written"] == 0 and again["spx_raw_days"]["already"] == 1
    assert again["spx_brier"]["written"] == 0 and again["spx_gex_daily"]["written"] == 0
    assert again["spx_meta"]["written"] is False


def test_a_hash_that_does_not_match_the_manifest_is_reported(tmp_path):
    src = tmp_path / "tree"
    _tree(src)
    (src / "raw" / "2026-08-13" / "gex_levels.json").write_text("{}", encoding="utf-8")
    assert it.import_tree(src)["spx_raw_days"]["sha_mismatch"] == ["2026-08-13/gex_levels.json"]


def test_no_tree_is_reported(tmp_path):
    assert it.import_tree(tmp_path / "none")["status"] == "no_tree"
