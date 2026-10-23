"""#82: a ``running`` job_runs row whose end write was skipped is closed on start.

A row stays ``running`` only while it might still be that job. Reconcile closes it as
``lost`` when it is older than the module's deadline (or a generous bound) and the
process is gone. A live process, a row still inside its bound, and a second call are
left untouched.
"""
import os

import pytest

from RevenueChaser.app import jobs
from RevenueChaser.core import automations as A
from RevenueChaser.core import procs, rawstore

NOW = 1_700_000_000.0
DEAD = 4_000_000_001
OTHER = 4_000_000_002


@pytest.fixture
def sync_jobs(monkeypatch):
    jobs._JOB_QUEUE.queue.clear()
    with jobs._LOCK:
        jobs._JOBS.clear()
    monkeypatch.setattr(jobs, "_RUNNERS", {})
    monkeypatch.setattr(jobs, "_PRODUCES", {})
    monkeypatch.setattr(jobs, "_REQUIRES_UW", {})
    monkeypatch.setattr(jobs, "_REQUIRES", {})
    monkeypatch.setattr(jobs, "_LANE", {})
    monkeypatch.setattr(jobs, "_DEADLINE", {})
    monkeypatch.setattr(jobs, "_CANCELLABLE", {})
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)
    monkeypatch.setattr(jobs, "_ensure_long_worker", lambda: None)


def _insert(**over):
    row = {
        "run_id": "r", "job_id": "r", "module": "t_bound", "lane": "short",
        "trigger": "manual", "source": "dashboard", "pid": DEAD, "registry_name": "t_bound",
        "params": {"a": 1}, "queued_at": NOW - 120, "started_at": NOW - 120,
        "finished_at": None, "status": "running", "error": None, "message": None, "counts": None,
    }
    row.update(over)
    rawstore.write_rows("job_runs", [row])
    return row["run_id"]


def _bound_module():
    jobs.register("t_bound", lambda uw, **p: None, [], requires_uw=False,
                  deadline_s=60, cancellable=True)


def test_stale_dead_run_is_lost_live_and_young_stay_and_the_second_call_is_a_no_op(sync_jobs):
    _bound_module()
    stale = _insert(run_id="stale", pid=DEAD, started_at=NOW - 120)
    live = _insert(run_id="live", pid=os.getpid(), started_at=NOW - 120)
    young = _insert(run_id="young", pid=DEAD, started_at=NOW - 10)
    done = _insert(run_id="done", pid=DEAD, started_at=NOW - 500, status="done",
                   finished_at=NOW - 400, message="exit 0")
    queued = _insert(run_id="queued", pid=DEAD, started_at=None, queued_at=NOW - 500, status="queued")

    assert jobs.reconcile_lost_runs(now=NOW) == 1

    closed = rawstore.get("job_runs", stale)
    assert closed["status"] == "lost"
    assert closed["finished_at"] == NOW
    assert "outcome unknown" in closed["message"] and "not recorded" in closed["message"]
    assert closed["error"] is None
    # a partial upsert would have wiped the columns the sweep does not own
    assert (closed["module"], closed["pid"], closed["trigger"], closed["params"]) == (
        "t_bound", DEAD, "manual", {"a": 1})

    assert rawstore.get("job_runs", live)["status"] == "running"
    assert rawstore.get("job_runs", live)["finished_at"] is None
    assert rawstore.get("job_runs", young)["status"] == "running"
    assert rawstore.get("job_runs", done)["status"] == "done"
    assert rawstore.get("job_runs", done)["finished_at"] == NOW - 400
    assert rawstore.get("job_runs", queued)["status"] == "queued"

    snapshot = rawstore.get("job_runs", stale)
    assert jobs.reconcile_lost_runs(now=NOW + 10) == 0
    assert rawstore.get("job_runs", stale) == snapshot


def test_a_row_exactly_at_the_deadline_is_not_older_than_it(sync_jobs):
    _bound_module()
    _insert(run_id="edge", pid=DEAD, started_at=NOW - 60)
    assert jobs.reconcile_lost_runs(now=NOW) == 0
    assert rawstore.get("job_runs", "edge")["status"] == "running"


def test_no_deadline_uses_the_generous_bound_not_zero(sync_jobs):
    _insert(run_id="five_h", module="no_such_module", pid=DEAD, started_at=NOW - 5 * 3600)
    _insert(run_id="seven_h", module="no_such_module", pid=DEAD, started_at=NOW - 7 * 3600)
    assert jobs.reconcile_lost_runs(now=NOW) == 1
    assert rawstore.get("job_runs", "five_h")["status"] == "running"
    assert rawstore.get("job_runs", "seven_h")["status"] == "lost"


def test_a_scheduled_name_uses_its_task_time_limit(sync_jobs, monkeypatch):
    entry = A.AutomationEntry(
        name="RC-Test-Bound", label="t", args=("-c", "pass"), time="23:00", description="d",
        settings=A.TaskSettings(time_limit_hours=1))
    monkeypatch.setattr(A, "_BY_NAME", {entry.name: entry})
    _insert(run_id="inside", module=entry.name, pid=DEAD, started_at=NOW - 1800)
    _insert(run_id="past", module=entry.name, pid=DEAD, started_at=NOW - 7200)
    assert jobs.reconcile_lost_runs(now=NOW) == 1
    assert rawstore.get("job_runs", "inside")["status"] == "running"
    assert rawstore.get("job_runs", "past")["status"] == "lost"


def test_an_alive_pid_whose_lock_start_does_not_match_is_not_that_job(sync_jobs, monkeypatch):
    """PID reuse: the number is alive, but the lock's start mark is a different process (#355)."""
    _bound_module()
    monkeypatch.setattr(procs, "pid_alive", lambda p: p in (OTHER, os.getpid()))
    monkeypatch.setattr(procs, "process_start_time", lambda p: "newstart")
    lock = rawstore.DB_PATH.parent / "universe.lock"
    lock.write_text(f"{OTHER} foreign-token oldstart\n", encoding="ascii")
    _insert(run_id="reused", pid=OTHER, started_at=NOW - 120)
    assert jobs.reconcile_lost_runs(now=NOW) == 1
    assert rawstore.get("job_runs", "reused")["status"] == "lost"


def test_an_alive_pid_whose_lock_start_matches_is_left_running(sync_jobs, monkeypatch):
    _bound_module()
    monkeypatch.setattr(procs, "pid_alive", lambda p: p in (OTHER, os.getpid()))
    monkeypatch.setattr(procs, "process_start_time", lambda p: "samestart")
    lock = rawstore.DB_PATH.parent / "universe.lock"
    lock.write_text(f"{OTHER} foreign-token samestart\n", encoding="ascii")
    _insert(run_id="holder", pid=OTHER, started_at=NOW - 120)
    assert jobs.reconcile_lost_runs(now=NOW) == 0
    assert rawstore.get("job_runs", "holder")["status"] == "running"


def test_a_job_running_in_this_process_is_never_closed(sync_jobs, monkeypatch):
    _bound_module()
    monkeypatch.setattr(procs, "pid_alive", lambda p: False)
    _insert(run_id="ours", pid=DEAD, started_at=NOW - 120)
    with jobs._LOCK:
        jobs._JOBS["ours"] = {"id": "ours", "status": "running", "module": "t_bound"}
    assert jobs.reconcile_lost_runs(now=NOW) == 0
    assert rawstore.get("job_runs", "ours")["status"] == "running"


def test_a_row_with_no_pid_is_judged_by_age_alone(sync_jobs):
    _bound_module()
    _insert(run_id="no_pid", pid=None, started_at=NOW - 120)
    _insert(run_id="queued_only", pid=None, started_at=None, queued_at=NOW - 120)
    _insert(run_id="no_time", pid=None, started_at=None, queued_at=None)
    assert jobs.reconcile_lost_runs(now=NOW) == 2
    assert rawstore.get("job_runs", "no_pid")["status"] == "lost"
    assert rawstore.get("job_runs", "queued_only")["status"] == "lost"
    assert rawstore.get("job_runs", "no_time")["status"] == "running"


def test_dashboard_start_reconciles(monkeypatch):
    from RevenueChaser.app import dashboard
    seen = []
    monkeypatch.setattr(jobs, "reconcile_lost_runs", lambda **k: seen.append("dash") or 0)
    monkeypatch.setattr(dashboard, "_kill_stale_server", lambda port: None)
    monkeypatch.setattr(dashboard.app, "run", lambda *a, **k: None)
    dashboard.main(port=9, open_browser=False)
    assert seen == ["dash"]


def test_scheduled_runner_start_reconciles(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(jobs, "reconcile_lost_runs", lambda **k: seen.append("sched") or 0)
    monkeypatch.setattr(A.config, "OUTPUT_DIR", tmp_path)
    entry = A.AutomationEntry(name="RC-Writer-82", label="w", args=("-c", "pass"), time="23:00",
                              description="d", tables_written=())
    monkeypatch.setattr(A, "_BY_NAME", {entry.name: entry})
    assert A.run_task(entry.name) == 0
    assert seen == ["sched"]
