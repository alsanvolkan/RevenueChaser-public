"""The two board switches (#527, avo 6 Nov 2026): **fetch** stops every fetch and build, **backup** every backup.

A switch with no row is on. Off stops a queued or hand-run request before it sends anything, and a running fetch
before its next provider call (the same point a dataset's Pause uses); a build that started is not stopped. No
provider call: the fake dataset only counts what it would have sent."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import DATASETS, base


def test_a_switch_with_no_row_is_on_and_the_last_set_wins():
    assert ops.switch_off("fetch") is None and ops.read_switches() == {}
    ops.switch_set("fetch", False, by="avo")
    row = ops.switch_off("fetch")
    assert row["off_by"] == "avo" and row["off_at"] is not None
    assert ops.switch_off("backup") is None, "the two switches are separate"
    ops.switch_set("fetch", True, by="avo")
    assert ops.switch_off("fetch") is None
    assert set(ops.read_switches()) == {"fetch"}
    with pytest.raises(ValueError):
        ops.switch_set("everything", False, by="avo")


class _Fetcher:
    name = "test/switch_stops"
    kind = "market"
    providers = ()
    tables = ()
    cadence = "daily"

    def __init__(self, root: Path):
        self.root = root
        self.steps: list[str] = []
        self.off_after: int | None = None

    def default_as_of(self):
        return date(2026, 11, 6)

    def fetch(self, ctx, as_of):
        for i in range(3):
            ctx.check_cap("test")
            self.steps.append(f"call{i}")
            if self.off_after == i + 1:
                ops.switch_set("fetch", False, by="avo")
        return []

    def to_parquet_all(self, ctx, raws):
        self.steps.append("to_parquet_all")
        return []


@pytest.fixture()
def fetcher(monkeypatch, tmp_path):
    ds = _Fetcher(tmp_path / "raw")
    monkeypatch.setitem(DATASETS, ds.name, ds)
    monkeypatch.setattr(base, "build_kind", lambda ctx, d: d.steps.append("build") or {"dataset": d.name,
                                                                                         "result": "changed"})
    return ds


def _run(ds, **kw):
    rid = ops.enqueue(ds.name, None, by="cli")
    row = base.run_request(ops.next_request(ds.name), **kw)
    return row, next(r for r in ops.rows("build_requests") if r["id"] == rid)


def test_fetch_switch_off_skips_a_request_before_any_call(fetcher):
    ops.switch_set("fetch", False, by="avo")
    row, req = _run(fetcher)
    assert fetcher.steps == []
    assert row["result"] == "skipped" and row["note"].startswith("skipped: the fetch switch is off (avo at ")
    assert req["status"] == "skipped"


def test_fetch_switch_turned_off_mid_fetch_stops_before_the_next_call(fetcher):
    fetcher.off_after = 1
    row, _ = _run(fetcher, clock=iter(range(0, 1000, 10)).__next__)
    assert fetcher.steps == ["call0"] and "build" not in fetcher.steps
    assert row["result"] == "skipped" and "fetch switch is off" in row["note"]


def test_backup_switch_does_not_stop_a_fetch(fetcher):
    ops.switch_set("backup", False, by="avo")
    row, _ = _run(fetcher)
    assert fetcher.steps[-1] == "build" and row["result"] == "changed"


def test_a_files_switch_holds_its_datasets_by_hand_too_and_no_other_file(fetcher):
    """avo, 10 Nov 2026: "main > midswitch > subswitches" and "Fetch de dursun": a file's switch off skips a Fetch
    pressed by hand on one of its datasets before any call; another file's switch does not."""
    from RevenueChaser.rcdata import serving
    assert set(ops.FILE_KINDS) == set(serving.KINDS)
    ops.switch_set("file-options", False, by="avo")
    row, _ = _run(fetcher)
    assert fetcher.steps[-1] == "build" and row["result"] == "changed", "market's datasets run on"
    ops.switch_set("file-market", False, by="avo")
    fetcher.steps.clear()
    row, req = _run(fetcher)
    assert fetcher.steps == [] and req["status"] == "skipped"
    assert row["note"].startswith("skipped: the market.duckdb switch is off (avo at ")
    with pytest.raises(ValueError):
        ops.switch_set("file-nope", False, by="avo")


def test_the_fetch_switch_is_read_before_the_files(fetcher):
    ops.switch_set("file-market", False, by="avo")
    ops.switch_set("fetch", False, by="avo")
    assert base.held_by_switch(fetcher.name)[0] == "fetch"
    ops.switch_set("fetch", True, by="avo")
    assert base.held_by_switch(fetcher.name)[0] == "file-market"
    assert base.held_by_switch("no/such_dataset") is None


def test_a_held_dataset_is_not_queued_by_the_hourly_pass(fetcher, monkeypatch):
    """avo, 10 Nov 2026 ("burada yaptigim her ayarin uygulanabilir oldugundan emin ol"): a file switched off queues
    nothing every hour (no skipped row an hour); switched on, the dataset is due again."""
    from RevenueChaser.rcdata.build import due
    monkeypatch.setattr(due, "is_due", lambda ds, mode: (True, "test"))
    ops.dataset_mode_set(fetcher.name, "full", by="avo")      # due, in a mode the hourly pass queues
    ops.switch_set("file-market", False, by="avo")
    [(name, why, rid, error)] = due.enqueue_due({fetcher.name: fetcher})
    assert rid is None and not error and "market.duckdb switch is off" in why
    ops.switch_set("file-market", True, by="avo")
    [(_, why, rid, _)] = due.enqueue_due({fetcher.name: fetcher})
    assert rid is not None, why
