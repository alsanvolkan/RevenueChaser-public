"""#243 (PL4): a job deadline, and cancel of a running job.

A deadline comes from ``jobs.register`` (none by default) and can be overridden per ``submit``. Cancel of a running
job sets the job's stop token; the runner stops at its next checkpoint and the job ends ``cancelled``. A passed
deadline ends it ``error`` with a message naming the deadline and the phase. Both are cooperative: nothing is
interrupted mid-write.
"""
from __future__ import annotations

import queue
import threading
import time

import pytest

from RevenueChaser.app import jobs, runners
from RevenueChaser.app.tests.test_runners_freeze_128 import freezers, order  # noqa: F401 - fixtures
from RevenueChaser.core import job_control, rawstore


@pytest.fixture
def sync_jobs(monkeypatch):
    """Jobs run only when a test calls ``jobs.execute``."""
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)
    monkeypatch.setattr(jobs, "_ensure_long_worker", lambda: None)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_SEQ", {})


@pytest.fixture
def long_lane(monkeypatch):
    """A real long-lane worker on fresh queues."""
    monkeypatch.setattr(jobs, "_JOB_QUEUE", queue.Queue())
    monkeypatch.setattr(jobs, "_LONG_QUEUE", queue.Queue())
    monkeypatch.setattr(jobs, "_WORKER_STARTED", False)
    monkeypatch.setattr(jobs, "_LONG_STARTED", False)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_SEQ", {})


def _wait(job_id, statuses, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        j = jobs.get(job_id)
        if j and j["status"] in statuses:
            return j
        time.sleep(0.01)
    return jobs.get(job_id)


def _stuck(started=None, after=None):
    """A runner that never finishes on its own: it loops on checkpoints (a 5 s safety net ends the test)."""
    def run(uw, **params):
        job_control.checkpoint("Stuck phase")
        if started is not None:
            started.set()
        end = time.time() + 5
        while time.time() < end:
            job_control.checkpoint()
            time.sleep(0.005)
        if after is not None:
            after.append("ran past the stop")
    return run


# ── deadline ──────────────────────────────────────────────────────────────────

def test_a_register_deadline_turns_a_stuck_runner_into_error(sync_jobs):
    jobs.register("t_dead_reg", _stuck(), [], requires_uw=False, lane="long", deadline_s=0.05, cancellable=True)
    jid = jobs.submit("t_dead_reg")
    t0 = time.time()
    jobs.execute(jid)
    job = jobs.get(jid)
    assert time.time() - t0 < 2
    assert job["status"] == "error", job
    assert "deadline" in job["error"] and "Stuck phase" in job["error"]
    assert job["deadline_s"] == 0.05 and job["deadline_at"]


def test_a_submit_deadline_overrides_the_register_default(sync_jobs):
    jobs.register("t_dead_sub", _stuck(), [], requires_uw=False, lane="long", cancellable=True)
    jid = jobs.submit("t_dead_sub", deadline_s=0.05)
    jobs.execute(jid)
    assert jobs.get(jid)["status"] == "error"


def test_a_job_without_a_deadline_has_none(sync_jobs):
    jobs.register("t_nodead", lambda uw, **p: None, [], requires_uw=False)
    jid = jobs.submit("t_nodead")
    jobs.execute(jid)
    job = jobs.get(jid)
    assert job["status"] == "done" and job["deadline_s"] is None and job["deadline_at"] is None


def test_a_deadline_needs_a_runner_that_can_stop_early(sync_jobs):
    """A deadline on a job with no checkpoints would never fire, so it is refused up front."""
    with pytest.raises(ValueError, match="cancellable"):
        jobs.register("t_dead_fixed", lambda uw, **p: None, [], requires_uw=False, deadline_s=60)
    jobs.register("t_dead_fixed2", lambda uw, **p: None, [], requires_uw=False)
    with pytest.raises(ValueError, match="cancellable"):
        jobs.submit("t_dead_fixed2", deadline_s=60)


def test_the_deadline_is_stored_in_job_runs(sync_jobs):
    jobs.register("t_dead_row", _stuck(), [], requires_uw=False, deadline_s=0.05, cancellable=True)
    jid = jobs.submit("t_dead_row")
    jobs.execute(jid)
    row = rawstore.query("SELECT status, error FROM job_runs WHERE run_id = ?", [jid])[0]
    assert row["status"] == "error" and "deadline" in row["error"]


# ── cancel of a running job ───────────────────────────────────────────────────

def test_cancel_of_a_running_job_stops_it_at_the_next_boundary(long_lane):
    started, after = threading.Event(), []
    jobs.register("t_cancel_run", _stuck(started, after), [], requires_uw=False, lane="long", cancellable=True)
    jid = jobs.submit("t_cancel_run")
    assert started.wait(5)
    assert jobs.get(jid)["status"] == "running"
    assert jobs.cancel(jid) is True
    assert jobs.get(jid)["cancel_requested"] is True
    job = _wait(jid, ("done", "error", "cancelled"))
    assert job["status"] == "cancelled", job
    assert "Stuck phase" in (job["message"] or "")
    assert after == []
    row = rawstore.query("SELECT status FROM job_runs WHERE run_id = ?", [jid])[0]
    assert row["status"] == "cancelled"


def test_the_next_queued_long_job_starts_after_a_cancel(long_lane):
    started, ran = threading.Event(), []
    jobs.register("t_cancel_first", _stuck(started), [], requires_uw=False, lane="long", cancellable=True)
    jobs.register("t_cancel_next", lambda uw, **p: ran.append(1), [], requires_uw=False, lane="long")
    first = jobs.submit("t_cancel_first")
    nxt = jobs.submit("t_cancel_next")
    assert started.wait(5)
    assert jobs.get(nxt)["status"] == "queued"
    jobs.cancel(first)
    assert _wait(nxt, ("done", "error"))["status"] == "done"
    assert ran == [1]
    assert jobs.get(first)["status"] == "cancelled"


def test_a_finished_job_cannot_be_cancelled(sync_jobs):
    jobs.register("t_cancel_done", lambda uw, **p: None, [], requires_uw=False)
    jid = jobs.submit("t_cancel_done")
    jobs.execute(jid)
    assert jobs.cancel(jid) is False
    assert jobs.get(jid)["status"] == "done"
    from RevenueChaser.app import dashboard
    from RevenueChaser.core.tests.panel_client import panel_test_client
    r = panel_test_client(dashboard.app).post(f"/api/job/{jid}/cancel")
    assert r.status_code == 409 and r.get_json()["error"] == "job already finished"   # delta review D14


def test_the_cancel_route_accepts_a_running_job(long_lane):
    from RevenueChaser.app import dashboard
    from RevenueChaser.core.tests.panel_client import panel_test_client
    client = panel_test_client(dashboard.app)
    started = threading.Event()
    jobs.register("t_cancel_route", _stuck(started), [], requires_uw=False, lane="long", cancellable=True)
    jid = jobs.submit("t_cancel_route")
    assert started.wait(5)
    r = client.post(f"/api/job/{jid}/cancel")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["job"]["cancel_requested"] is True
    assert _wait(jid, ("cancelled", "done", "error"))["status"] == "cancelled"
    assert client.get(f"/api/job/{jid}").get_json()["job"]["status"] == "cancelled"
    assert client.post(f"/api/job/{jid}/cancel").status_code == 409


# ── only a job that can stop early accepts a cancel while it runs (review N1) ──

# dashboard.py's registrations, captured at import (some test files clear the registry later in a run).
from RevenueChaser.app import dashboard as _dashboard  # noqa: E402
_DASHBOARD_CANCELLABLE = {k for k, v in jobs._CANCELLABLE.items() if v}
_DASHBOARD_JOBS = set(jobs._RUNNERS)


def test_dashboard_marks_the_checkpointed_jobs_cancellable():
    assert _DASHBOARD_CANCELLABLE == {
        "universe", "module_asymmetric_growth", "swing", "module_fundamentals",
        "module_sec_13f", "edgar_sync", "identity", "daily_bars", "spxgamma_history",
    }
    assert {"module_sec_13f", "edgar_sync", "identity", "holdings", "daily_bars"} <= _DASHBOARD_JOBS


def test_a_running_job_that_cannot_stop_early_refuses_the_cancel(long_lane):
    from RevenueChaser.core.tests.panel_client import panel_test_client
    client = panel_test_client(_dashboard.app)
    started, release = threading.Event(), threading.Event()
    jobs.register("t_no_stop", lambda uw, **p: (started.set(), release.wait(5)), [], requires_uw=False,
                  lane="long")
    jid = jobs.submit("t_no_stop")
    try:
        assert started.wait(5)
        job = jobs.get(jid)
        assert job["status"] == "running" and job["cancellable"] is False
        assert jobs.cancel(jid) is False
        r = client.post(f"/api/job/{jid}/cancel")
        assert r.status_code == 409 and "cannot stop early" in r.get_json()["error"]
        assert jobs.get(jid)["cancel_requested"] is False
    finally:
        release.set()
    assert _wait(jid, ("done", "error", "cancelled"))["status"] == "done"


def test_a_queued_job_that_cannot_stop_early_can_still_be_cancelled(sync_jobs):
    jobs.register("t_no_stop_queued", lambda uw, **p: None, [], requires_uw=False)
    jid = jobs.submit("t_no_stop_queued")
    assert jobs.cancel(jid) is True
    assert jobs.get(jid)["status"] == "cancelled"


# The badge itself (renderStatus under node) is tested in test_job_badge_243.py.


# ── the universe job: phase boundaries and the freeze ─────────────────────────

def _universe_job(monkeypatch, ag_freeze_hook):
    from RevenueChaser.module_asymmetric_growth import freeze as ag_freeze
    real = ag_freeze.freeze_result

    def freeze_then(result, **kw):
        real(result, **kw)
        ag_freeze_hook()
    monkeypatch.setattr(ag_freeze, "freeze_result", freeze_then)
    jobs.register("t_universe_243", runners.run_universe_refresh, [], requires_uw=False, lane="long",
                  cancellable=True)
    return jobs.submit("t_universe_243")


def test_a_cancel_between_ag_and_swing_stops_before_swing_and_any_later_freeze(sync_jobs, order, freezers,
                                                                               monkeypatch):
    jid = _universe_job(monkeypatch, lambda: job_control.current().cancel())
    jobs.execute(jid)
    job = jobs.get(jid)
    assert job["status"] == "cancelled", job
    assert "ag.freeze" in order
    assert not [c for c in order if c.startswith(("swing.", "fund."))], order
    assert set(freezers) == {"ag"}
    assert "Swing" in job["message"]


def test_a_deadline_between_ag_and_swing_stops_before_swing(sync_jobs, order, freezers, monkeypatch):
    # The deadline passes at the end of AG, whatever the machine's speed (a fixed 0.05 s raced a loaded runner).
    jid = _universe_job(monkeypatch, lambda: setattr(job_control.current(), "deadline_s", 0.0))
    jobs.execute(jid)
    job = jobs.get(jid)
    assert job["status"] == "error", job
    assert "deadline" in job["error"] and "Swing" in job["error"]
    assert not [c for c in order if c.startswith(("swing.", "fund."))], order


def test_a_universe_job_without_a_stop_still_runs_all_three(sync_jobs, order, freezers, monkeypatch):
    jid = _universe_job(monkeypatch, lambda: None)
    jobs.execute(jid)
    assert jobs.get(jid)["status"] == "done"
    assert set(freezers) == {"ag", "swing", "fund"}
