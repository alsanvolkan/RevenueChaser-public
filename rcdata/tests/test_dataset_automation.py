"""A source's own automation (#527, avo 6 Nov 2026: "bu bir duckdb icin var, ama altinda her source icin yok"): one
dataset may differ from its file's automation. No row (``file``, "as the file") follows the file; a row wins over
the file's mode for that dataset only. No provider call: every test stops at the queue."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import DATASETS, due

MS = "market_series"
TUE_EVENING = datetime(2026, 10, 27, 22, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def market_only(monkeypatch):
    from RevenueChaser.rcdata import build
    monkeypatch.setattr(build, "DATASETS", {MS: DATASETS[MS]})
    monkeypatch.setattr(calendar, "_now", lambda: TUE_EVENING)


def _queued() -> list[dict]:
    return [{c: r[c] for c in ("dataset", "params")} for r in ops.rows("build_requests")]


def test_no_row_is_off_whatever_the_file_says_and_the_reader_never_creates_the_file():
    assert ops.read_dataset_modes() == {} and not ops.ops_path().exists()
    assert ops.dataset_mode(MS, "market") == "off"
    ops.kind_mode_set("market", "raw", by="avo")
    assert ops.dataset_mode(MS, "market") == "off"


def test_the_one_time_copy_gives_each_dataset_its_files_mode_once():
    from types import SimpleNamespace as NS
    ds = {MS: NS(kind="market"), "own": NS(kind="market"), "idle": NS(kind="options")}
    ops.kind_mode_set("market", "raw", by="avo")
    ops.dataset_mode_set("own", "full", by="avo")
    assert ops.copy_kind_modes(ds, by="deploy") == [(MS, "raw")]
    modes = {k: v["mode"] for k, v in ops.read_dataset_modes().items()}
    assert modes == {MS: "raw", "own": "full"}, "an own row is kept and an off kind writes nothing"
    assert ops.copy_kind_modes(ds, by="deploy") == []
    assert ops.dataset_mode(MS, "market") == "raw"


def test_a_sources_own_mode_wins_for_that_source_only():
    ops.kind_mode_set("market", "off", by="avo")
    ops.dataset_mode_set(MS, "full", by="avo")
    row = ops.read_dataset_modes()[MS]
    assert (row["mode"], row["set_by"]) == ("full", "avo") and row["set_at"] > 0
    assert ops.dataset_mode(MS, "market") == "full"
    due.enqueue_due()
    assert _queued() == [{"dataset": MS, "params": None}]


def test_a_source_set_off_is_not_queued_while_its_file_runs_full():
    ops.kind_mode_set("market", "full", by="avo")
    ops.dataset_mode_set(MS, "off", by="avo")
    [(name, why, rid, _)] = due.enqueue_due()
    assert rid is None and "automation off" in why and _queued() == []


def test_as_the_file_drops_the_row_and_an_unknown_mode_writes_nothing():
    ops.dataset_mode_set(MS, "raw", by="avo")
    ops.dataset_mode_set(MS, "file", by="avo")
    assert ops.read_dataset_modes() == {}
    with pytest.raises(ValueError, match="unknown mode"):
        ops.dataset_mode_set(MS, "duckdb", by="avo")
    assert ops.read_dataset_modes() == {}
