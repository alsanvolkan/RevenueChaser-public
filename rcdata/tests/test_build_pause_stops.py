"""Pause stops a running dataset (#492 PR (ii)): before its next provider call, or at the fetch/build gap.

The fake dataset calls ``ctx.check_cap`` like ``http_get`` does; a test writes the pause between two calls. What
was fetched before the pause stays; no build runs; the request ends ``skipped`` with who paused and when."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import DATASETS, base


class _Fetcher:
    name = "test/pause_stops"
    kind = "market"
    providers = ()
    tables = ()
    cadence = "daily"

    def __init__(self, root: Path):
        self.root = root
        self.steps: list[str] = []
        self.pause_after: int | None = None        # write the pause after this many provider calls
        self.pause_in_build = False

    def default_as_of(self):
        return date(2026, 10, 28)

    def fetch(self, ctx, as_of):
        raws = []
        for i in range(3):
            ctx.check_cap("test")                   # what every http_get does before it sends
            self.steps.append(f"call{i}")
            raw = self.root / f"raw-{i}.json"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_text("{}")
            raws.append(raw)
            if self.pause_after == i + 1:
                ops.schedule_pause(self.name, by="avo")
        return raws

    def to_parquet_all(self, ctx, raws):
        self.steps.append("to_parquet_all")
        if self.pause_after == 0:
            ops.schedule_pause(self.name, by="avo")    # paused between the fetch and the build
        return []

    def backfill(self, ctx, start, end):
        self.steps.append("backfill")


@pytest.fixture()
def fetcher(monkeypatch, tmp_path):
    ds = _Fetcher(tmp_path / "raw")
    monkeypatch.setitem(DATASETS, ds.name, ds)

    def build_kind(ctx, d):
        d.steps.append("build")
        if d.pause_in_build:
            ops.schedule_pause(d.name, by="avo")
        return {"dataset": d.name, "result": "changed"}
    monkeypatch.setattr(base, "build_kind", build_kind)
    return ds


def _run(ds, **kw) -> tuple[dict, dict]:
    rid = ops.enqueue(ds.name, None, by="settings")
    req = ops.next_request(ds.name)
    row = base.run_request(req, **kw)
    return row, next(r for r in ops.rows("build_requests") if r["id"] == rid)


def test_a_pause_during_the_fetch_stops_before_the_next_call(fetcher):
    fetcher.pause_after = 1
    row, req = _run(fetcher, clock=iter(range(0, 1000, 10)).__next__)   # 10 s a call: past the read throttle
    assert fetcher.steps == ["call0"], "the second provider call is never sent"
    assert "build" not in fetcher.steps
    assert row["result"] == "skipped" and row["note"].startswith("skipped: stopped, paused by avo at ")
    assert req["status"] == "skipped" and req["note"].startswith("skipped: stopped, paused by avo")
    assert (fetcher.root / "raw-0.json").is_file(), "what was fetched before the pause stays"


def test_a_pause_between_the_fetch_and_the_build_skips_the_build(fetcher):
    fetcher.pause_after = 0
    row, req = _run(fetcher)
    assert fetcher.steps == ["call0", "call1", "call2", "to_parquet_all"]
    assert row["result"] == "skipped" and "stopped, paused by avo" in row["note"] and req["status"] == "skipped"


def test_a_build_that_started_is_not_stopped(fetcher):
    fetcher.pause_in_build = True
    row, req = _run(fetcher)
    assert fetcher.steps[-1] == "build" and row["result"] == "changed" and req["status"] == "done"


def test_the_pause_is_read_at_most_once_every_five_seconds(monkeypatch):
    reads = []
    monkeypatch.setattr(ops, "schedule_get", lambda ds: reads.append(ds) or None)
    now = [100.0]
    ctx = base.RunContext(run_id="r", dataset="x", clock=lambda: now[0])
    for _ in range(50):
        ctx.check_pause()
    assert len(reads) == 1, "fifty calls inside a second read the schedule once"
    now[0] += base.PAUSE_READ_EVERY_S
    ctx.check_pause()
    assert len(reads) == 2
    ctx.check_pause(force=True)
    assert len(reads) == 3, "the fetch/build gap always reads"


def test_a_pause_once_seen_stops_every_later_call():
    """A dataset that catches its errors per name must not go on: Paused is not an Exception, and it sticks."""
    assert not issubclass(base.Paused, Exception)
    ops.schedule_pause("x", by="avo")
    ctx = base.RunContext(run_id="r", dataset="x")
    for _ in range(2):
        with pytest.raises(base.Paused):
            ctx.check_cap("test")
    ops.schedule_resume("x")
    with pytest.raises(base.Paused):
        ctx.check_pause(force=True)
