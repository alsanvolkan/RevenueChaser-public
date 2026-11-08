"""Each serving kind's automation (avo, 6 Nov 2026): what the hourly pass queues on its own for the kind's due
datasets. ``off`` (no row) queues nothing; ``raw`` / ``parquet`` / ``full`` queue the run stage a Fetch's ▾ asks for.
A partial stage's own ``skipped`` check counts as the run, so the dataset is not queued again every hour. No
provider call: every test stops at the queue."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import DATASETS, base, due

MS = "market_series"
TUE_EVENING = datetime(2026, 10, 27, 22, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def market_only(monkeypatch):
    from RevenueChaser.rcdata import build
    monkeypatch.setattr(build, "DATASETS", {MS: DATASETS[MS]})
    monkeypatch.setattr(calendar, "_now", lambda: TUE_EVENING)


def _queued() -> list[dict]:
    return [{c: r[c] for c in ("dataset", "requested_by", "params")} for r in ops.rows("build_requests")]


def _stage_checked(note: str) -> None:
    # A partial run of today's as-of: next check tomorrow, as base._finish writes it.
    as_of = DATASETS[MS].default_as_of()
    ops.record_check(MS, "skipped", source_as_of=as_of.isoformat(), note=note,
                     next_check_due=calendar.next_trading_day(as_of).isoformat())


def test_a_kind_with_no_row_is_off_and_queues_nothing():
    assert ops.kind_mode("market") == "off"
    [(name, why, rid, error)] = due.enqueue_due()
    assert (name, rid, error) == (MS, None, False) and why == "not due (automation off for market)"
    assert _queued() == []


@pytest.mark.parametrize("mode, params", [("raw", '{"no_parquet": true}'), ("parquet", '{"no_duckdb": true}'),
                                          ("full", None)])
def test_each_mode_queues_the_stage_a_fetch_press_asks_for(mode, params):
    ops.kind_mode_set("market", mode, by="avo")
    due.enqueue_due()
    assert _queued() == [{"dataset": MS, "requested_by": "schedule", "params": params}]


def test_another_kinds_mode_does_not_open_this_one():
    ops.kind_mode_set("prices", "full", by="avo")
    due.enqueue_due()
    assert _queued() == []


@pytest.mark.parametrize("mode, note, due_now", [
    ("raw", base.skip_note(1, raw_only=True), False),      # its own raw run: done until the next check
    ("raw", base.skip_note(1), False),                     # a raw + Parquet Fetch covers raw too
    ("parquet", base.skip_note(1), False),
    ("parquet", base.skip_note(1, raw_only=True), True),   # raw alone leaves the Parquet stage owed
    ("full", base.skip_note(1), True),                     # full: the next pass builds the rest, as before
    ("raw", "skipped: paused by avo at 2026-10-27 21:00 UTC", True),   # not a stage skip
])
def test_a_partial_stage_skip_counts_as_the_run_only_for_the_modes_it_covers(mode, note, due_now):
    _stage_checked(note)
    assert due.is_due(DATASETS[MS], mode)[0] is due_now


def test_an_unknown_mode_is_refused_and_writes_nothing():
    with pytest.raises(ValueError, match="unknown mode"):
        ops.kind_mode_set("market", "duckdb", by="avo")
    assert ops.kind_mode_get("market") is None


def test_the_row_says_who_set_it_and_the_reader_never_creates_the_file():
    assert ops.read_kind_modes() == {}
    assert not ops.ops_path().exists()
    ops.kind_mode_set("market", "raw", by="avo")
    ops.kind_mode_set("market", "full", by="settings")
    row = ops.read_kind_modes()["market"]
    assert (row["mode"], row["set_by"]) == ("full", "settings") and row["set_at"] > 0
    assert ops.kind_mode("market") == "full"


def test_the_stage_params_are_the_fetch_presses_own():
    from RevenueChaser.app.dashboard import REQUEST_STAGES
    assert REQUEST_STAGES == base.STAGE_PARAMS
    assert set(base.STAGE_PARAMS) | {"off"} == set(ops.KIND_MODES)
