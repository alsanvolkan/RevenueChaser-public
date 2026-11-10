"""``rcdata/build/dirty.py``: what changed in Parquet since the live file was built (#446 step 2).

Nothing builds on this yet: a dataset that declares no ``dirty`` is wholly dirty, so every kind is still built
whole. These tests pin the contract the incremental steps (3 onwards) will read.
"""
from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path

import pytest

from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base, dirty


class _Ds:
    """A dataset as ``dirty`` sees one: a name, and optionally its own declarations."""

    def __init__(self, name: str, **declared):
        self.name = name
        for key, value in declared.items():
            setattr(self, key, value)


def _build(kind: str = "market", rows: int = 3) -> None:
    with serving.build(kind) as b:
        b.con.execute("CREATE TABLE t (i INTEGER)")
        b.con.execute(f"INSERT INTO t SELECT range FROM range({rows})")
        b.swap({"t": rows})


def _partition(name: str, mtime: float | None = None) -> Path:
    path = base.parquet_root() / "src" / "ds" / "year=2026" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not really parquet")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ── a dataset's dirty set ─────────────────────────────────────────────────────────────────────────────────────

def test_a_dataset_that_declares_nothing_is_wholly_dirty():
    assert dirty.of(_Ds("sec/ftd"), None) is dirty.ALL


def test_a_dataset_that_declares_dirty_is_asked():
    ds = _Ds("sec/ftd", dirty=lambda ctx: dirty.Dirty(keys=frozenset({"AAPL"})))
    answer = dirty.of(ds, None)
    assert answer.keys == {"AAPL"} and answer.all is False


def test_a_dirty_set_narrows_by_day_range_too():
    """A cumulative table's dirty set is a range of days; a restated table's is a key (the two axes)."""
    days = ((date(2026, 11, 1), date(2026, 11, 5)),)
    ds = _Ds("alpaca/bars", dirty=lambda ctx: dirty.Dirty(ranges=days))
    answer = dirty.of(ds, None)
    assert answer.ranges == days and answer.keys == frozenset() and answer.all is False


def test_a_dataset_that_answers_none_is_wholly_dirty():
    assert dirty.of(_Ds("sec/ftd", dirty=lambda ctx: None), None) is dirty.ALL


def test_a_dataset_that_answers_the_wrong_shape_raises():
    """A bare set read as a narrowing would serve a partial table, so it is a mistake, not a dirty set."""
    with pytest.raises(TypeError, match="not a Dirty"):
        dirty.of(_Ds("sec/ftd", dirty=lambda ctx: {"AAPL"}), None)


def test_everything_is_dirty_when_neither_axis_is_narrowed():
    assert dirty.ALL.all is True and dirty.Dirty().all is True


# ── changed partitions ────────────────────────────────────────────────────────────────────────────────────────

def test_changed_partitions_names_only_the_files_written_after_the_cutoff():
    now = time.time()
    old = _partition("old.parquet", now - 600)
    new = _partition("new.parquet", now + 600)
    assert dirty.changed_partitions(now) == [new]
    assert sorted(dirty.changed_partitions(None)) == sorted([new, old]), "no cutoff: every partition is dirty"


def test_changed_partitions_can_be_narrowed_to_one_subtree():
    now = time.time()
    mine = _partition("mine.parquet", now + 600)
    other = base.parquet_root() / "elsewhere" / "part-0.parquet"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"x")
    os.utime(other, (now + 600, now + 600))
    assert dirty.changed_partitions(now, under=mine.parent.parent.parent) == [mine]


def test_changed_partitions_of_a_missing_parquet_root_is_empty():
    assert dirty.changed_partitions(None, under=base.parquet_root() / "never-written") == []


# ── the live file's build stamp ────────────────────────────────────────────────────────────────────────────────

def test_live_built_at_is_none_until_a_build_has_been_swapped_in():
    assert dirty.live_built_at("market") is None
    _build()
    assert dirty.live_built_at("market") == pytest.approx(time.time(), abs=120)


def test_the_build_stamp_is_the_builds_start_not_its_swap():
    """A partition written while a build runs must come out dirty next time (the long build's race)."""
    with serving.build("market") as b:
        b.con.execute("CREATE TABLE t (i INTEGER)")
        b.con.execute("INSERT INTO t VALUES (1)")
        written = _partition("during-the-build.parquet")
        b.swap({"t": 1})
    built_at = dirty.live_built_at("market")
    assert built_at is not None
    assert built_at <= os.path.getmtime(written), "the stamp must precede what the build read"
    assert dirty.changed_partitions(built_at) == [written]


# ── when a whole rebuild is the only safe build ────────────────────────────────────────────────────────────────

def test_a_kind_with_no_live_file_needs_a_whole_rebuild():
    why = dirty.full_rebuild_reason("market", datasets=())
    assert why and "serving schema version" in why


def test_a_live_file_of_another_serving_version_needs_a_whole_rebuild(monkeypatch):
    _build()
    monkeypatch.setitem(serving.SERVING_SCHEMA_VERSION, "market",
                        serving.SERVING_SCHEMA_VERSION["market"] + 1)
    assert dirty.full_rebuild_reason("market", datasets=())


# ── #649: a version bump rebuilds only its own kind ─────────────────────────────────────────────────────────────

def _fingerprints_with_edit(monkeypatch, edited: Path) -> tuple[dict, dict]:
    """Every kind's rules fingerprint as the code is, and again with ``edited``'s bytes changed."""
    def all_kinds() -> dict:
        return {k: dirty.rules_fingerprint(base.kind_datasets(k)) for k in serving.KINDS}
    before = all_kinds()
    real = Path.read_bytes
    target = edited.resolve()
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: real(self) + (b"\n# edited\n" if self.resolve() == target else b""))
    return before, all_kinds()


def test_a_version_bump_leaves_every_kinds_rules_fingerprint_unchanged(monkeypatch):
    from RevenueChaser.rcdata import serving_versions
    assert serving.SERVING_SCHEMA_VERSION is serving_versions.SERVING_SCHEMA_VERSION
    before, after = _fingerprints_with_edit(monkeypatch, Path(serving_versions.__file__))
    assert before == after


def test_an_edit_to_the_serving_base_or_dirty_code_changes_every_kinds_rules_fingerprint(monkeypatch):
    for module in (serving, base, dirty):
        before, after = _fingerprints_with_edit(monkeypatch, Path(module.__file__))
        monkeypatch.undo()
        assert all(before[k] != after[k] for k in serving.KINDS), module.__name__


def test_a_version_bump_makes_only_its_own_kind_whole(monkeypatch):
    _build("market")
    _build("spine")
    monkeypatch.setitem(serving.SERVING_SCHEMA_VERSION, "market", serving.SERVING_SCHEMA_VERSION["market"] + 1)
    assert dirty.full_rebuild_reason("market", datasets=())
    assert dirty.full_rebuild_reason("spine", datasets=()) is None


def test_a_current_live_file_needs_no_whole_rebuild():
    _build()
    assert dirty.full_rebuild_reason("market", datasets=()) is None


def test_a_dataset_may_ask_for_a_whole_rebuild():
    _build()
    quiet = _Ds("sec/ftd")
    loud = _Ds("wrds/prices", full_rebuild_reason=lambda ctx: "the source restated its history")
    why = dirty.full_rebuild_reason("market", datasets=(quiet, loud))
    assert why == "wrds/prices: the source restated its history"


def test_a_dataset_that_asks_for_nothing_leaves_the_build_incremental():
    _build()
    ds = _Ds("sec/ftd", full_rebuild_reason=lambda ctx: None)
    assert dirty.full_rebuild_reason("market", datasets=(ds,)) is None


# ── the mtime shortcut's two known gaps, red until the manifest lands ──────────────────────────────────────────

@pytest.mark.xfail(reason="mtime signal: a copy carries the old mtime over; the (path, size, mtime) manifest "
                          "catches it (#446, before step 3)", strict=True)
def test_a_partition_copied_in_with_its_old_mtime_is_dirty():
    _build()
    built_at = dirty.live_built_at("market")
    copied = _partition("from-another-machine.parquet", built_at - 3600)   # as cp -p / rsync -t leaves it
    assert copied in dirty.changed_partitions(built_at)


@pytest.mark.xfail(reason="mtime signal: a deleted partition leaves its rows in the live file with nothing to "
                          "mark them; the manifest's 'in the manifest, not on disk' branch catches it "
                          "(#446, before step 3)", strict=True)
def test_a_partition_deleted_after_the_build_is_dirty():
    old = _partition("went-away.parquet", time.time() - 600)
    _build()
    built_at = dirty.live_built_at("market")
    old.unlink()
    assert dirty.changed_partitions(built_at), "the kind still serves rows no partition backs any more"
