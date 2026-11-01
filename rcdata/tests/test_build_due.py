"""The builder queues what is due (``rcdata/build/due.py``): ``run`` without ``--dataset`` first enqueues every
dataset that ``dataset_schedule`` and its last check make due, at most one request per dataset (``ops.enqueue_once``),
and never a paused one. No provider call: every test stops at the queue or replaces ``base.run_request``."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import DATASETS, due
from RevenueChaser.rcdata.build import __main__ as cli

MS, UW = "market_series", "uw/spot_expiry_strike"
# Tue 27 Oct 2026: 11:00 New York (the session open, Monday is the latest complete one) and 18:00 (Tuesday complete).
TUE_MORNING = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
TUE_EVENING = datetime(2026, 10, 27, 22, 0, tzinfo=timezone.utc)
WED_MORNING = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def market_only(monkeypatch):
    """These tests are about the two market datasets' due rule; the spine's datasets (PR 5) are left out."""
    from RevenueChaser.rcdata import build
    only = {k: v for k, v in build.DATASETS.items() if k in (MS, UW)}
    monkeypatch.setattr(build, "DATASETS", only)
    monkeypatch.setitem(globals(), "DATASETS", only)


@pytest.fixture
def clock(monkeypatch):
    def set_to(when: datetime) -> None:
        monkeypatch.setattr(calendar, "_now", lambda: when)
    set_to(TUE_EVENING)
    return set_to


def _queue() -> list[dict]:
    with ops.connect() as con:
        return [dict(r) for r in con.execute("SELECT dataset, requested_by, params, status FROM build_requests "
                                             "ORDER BY id")]


def _checked(dataset: str, result: str = "changed", *, as_of: str | None = None, checked_on: str | None = None):
    nxt = calendar.next_trading_day(date.fromisoformat(as_of)).isoformat() if as_of else None
    ops.record_check(dataset, result, source_as_of=as_of, next_check_due=nxt, checked_on=checked_on)


# ── The due rule ──────────────────────────────────────────────────────────────────────────────────────────────

def test_a_dataset_never_built_is_due_and_queued_by_the_schedule(clock):
    out = due.enqueue_due()
    assert {name for name, _, rid, _ in out if rid} == set(DATASETS)
    rows = _queue()
    assert sorted(r["dataset"] for r in rows) == sorted(DATASETS)
    assert all(r["requested_by"] == "schedule" and r["params"] is None and r["status"] == "queued" for r in rows)


def test_two_overlapping_passes_queue_each_due_dataset_once(clock):
    due.enqueue_due()
    second = due.enqueue_due()
    assert all(rid is None and not error for _, _, rid, error in second)
    assert len(_queue()) == len(DATASETS)


def test_a_paused_dataset_is_not_queued(clock):
    ops.schedule_pause(MS, by="settings")
    due.enqueue_due()
    assert [r["dataset"] for r in _queue()] == [UW]
    ops.schedule_resume(MS)
    due.enqueue_due()
    assert sorted(r["dataset"] for r in _queue()) == sorted([UW, MS])


def test_daily_is_due_once_the_datasets_own_as_of_reaches_the_next_check_day(clock):
    _checked(MS, as_of="2026-10-27")                       # today's run done: next check Wed
    _checked(UW, as_of="2026-10-26")                       # Monday's session in: next check Tue
    clock(TUE_MORNING)                                     # Tuesday's session not complete yet
    assert due.is_due(DATASETS[MS])[0] is False
    assert due.is_due(DATASETS[UW])[0] is False
    clock(TUE_EVENING)                                     # Tuesday complete: UW is due, the series are not
    assert due.is_due(DATASETS[UW])[0] is True
    assert due.is_due(DATASETS[MS])[0] is False
    clock(WED_MORNING)
    assert due.is_due(DATASETS[MS])[0] is True
    due.enqueue_due()
    assert sorted(r["dataset"] for r in _queue()) == sorted([MS, UW])


def test_a_failed_or_skipped_last_check_is_due_again(clock):
    _checked(MS, "failed", as_of="2026-10-27")             # a failed run still carries a next check day
    assert due.is_due(DATASETS[MS]) == (True, "the last check failed")
    _checked(UW, "skipped")                                # a day cap or a pause: nothing was fetched
    assert due.is_due(DATASETS[UW]) == (True, "the last check was skipped")


def test_a_check_with_no_next_day_is_due_again_once_the_as_of_moves_on(clock):
    # base.next_due() is None past core/calendar.py's table (2028): the series must not then run every hour.
    ops.record_check(MS, "changed", source_as_of="2026-10-27", next_check_due=None)
    assert due.is_due(DATASETS[MS]) == (False, "no next session known; ran for 2026-10-27")
    clock(WED_MORNING)
    assert due.is_due(DATASETS[MS]) == (True, "no next session known; as-of 2026-10-28 is newer")
    ops.record_check(UW, "changed", source_as_of=None, next_check_due=None)
    assert due.is_due(DATASETS[UW]) == (True, "the last check names no as-of")


@pytest.mark.parametrize("cadence, days, expect", [("weekly", 6, False), ("weekly", 7, True),
                                                   ("monthly", 29, False), ("monthly", 30, True)])
def test_weekly_and_monthly_count_days_since_the_last_check(clock, cadence, days, expect):
    ops.schedule_set(MS, cadence, None)
    on = date.fromordinal(calendar.today_ny().toordinal() - days).isoformat()
    _checked(MS, as_of=on, checked_on=on)
    assert due.is_due(DATASETS[MS])[0] is expect


def test_an_unknown_cadence_reads_as_daily(clock):
    ops.schedule_set(MS, "hourly", None)
    _checked(MS, as_of="2026-10-27")
    assert due.is_due(DATASETS[MS])[0] is False
    clock(WED_MORNING)
    assert due.is_due(DATASETS[MS])[0] is True


def test_the_schedule_next_due_holds_a_dataset_until_that_day(clock):
    ops.schedule_set(UW, "daily", "2026-10-28")
    assert due.is_due(DATASETS[UW]) == (False, "held until 2026-10-28")
    clock(WED_MORNING)
    assert due.is_due(DATASETS[UW])[0] is True             # never built, and the hold has passed


def test_one_dataset_whose_due_check_raises_does_not_stop_the_others(clock, monkeypatch):
    monkeypatch.setattr(DATASETS[UW], "default_as_of", lambda: (_ for _ in ()).throw(ValueError("past the table")))
    _checked(UW, as_of="2026-10-26")
    out = {name: (reason, rid, error) for name, reason, rid, error in due.enqueue_due()}
    assert out[UW][1] is None and "past the table" in out[UW][0] and out[UW][2] is True
    assert out[MS][2] is False
    assert [r["dataset"] for r in _queue()] == [MS]


def test_a_locked_ops_file_while_queueing_one_dataset_does_not_stop_the_others(clock, monkeypatch):
    import sqlite3
    real = ops.enqueue_once

    def locked_for_uw(name, params=None, *, by):
        if name == UW:
            raise sqlite3.OperationalError("database is locked")
        return real(name, params, by=by)
    monkeypatch.setattr(ops, "enqueue_once", locked_for_uw)
    out = {name: (reason, rid, error) for name, reason, rid, error in due.enqueue_due()}
    assert out[UW] == ("due (never built), not queued: ops.sqlite busy: database is locked", None, True)
    assert out[MS][1] is not None and out[MS][2] is False
    assert [r["dataset"] for r in _queue()] == [MS]


# ── The CLI ───────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def ran(monkeypatch):
    from RevenueChaser.rcdata.build import base
    seen = []

    def fake(req, **kw):
        seen.append(req)
        ops.record_check(req["dataset"], "unchanged", source_as_of="2026-10-27", next_check_due="2026-10-28")
        ops.finish_request(req["id"], "done")
        return ops.latest_check(req["dataset"])
    monkeypatch.setattr(base, "run_request", fake)
    return seen


def test_run_once_queues_and_runs_what_is_due_and_skips_the_paused(clock, ran, capsys):
    ops.schedule_pause(UW, by="settings")
    assert cli.main(["run", "--once"]) == 0
    assert [(r["dataset"], r["requested_by"]) for r in ran] == [(MS, "schedule")]
    err = capsys.readouterr().err
    assert f"{MS}: due (never built), queued as request" in err and f"{UW}: not due (paused" in err
    assert cli.main(["run", "--once"]) == 0                # the run is in: nothing due an hour later
    assert len(ran) == 1


def test_run_once_with_a_dataset_queues_nothing_else(clock, ran):
    assert cli.main(["run", "--once", "--dataset", MS]) == 0
    assert [r["dataset"] for r in ran] == [MS] and [r["dataset"] for r in _queue()] == [MS]


def test_a_due_step_error_fails_the_pass_after_the_queue_is_drained(clock, ran, monkeypatch, capsys):
    monkeypatch.setattr(DATASETS[UW], "default_as_of", lambda: (_ for _ in ()).throw(ValueError("past the table")))
    _checked(UW, as_of="2026-10-26")
    assert cli.main(["run", "--once"]) == 1
    assert [r["dataset"] for r in ran] == [MS]             # the queue is still drained
    assert f"{UW}: due check failed: ValueError: past the table" in capsys.readouterr().err
