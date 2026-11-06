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
