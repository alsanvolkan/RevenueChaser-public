"""#83: a dashboard job_runs row names where it came from, and stores counts when the job has them."""
import pytest

from RevenueChaser.app import jobs
from RevenueChaser.core import rawstore


class _FakeUW:
    active = True

    def verify(self):
        return True, None


@pytest.fixture
def sync_jobs(monkeypatch):
    jobs._JOB_QUEUE.queue.clear()
    with jobs._LOCK:
        jobs._JOBS.clear()
    monkeypatch.setattr(jobs, "_RUNNERS", {})
    monkeypatch.setattr(jobs, "_PRODUCES", {})
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)
    monkeypatch.setattr(jobs, "_ensure_long_worker", lambda: None)
    monkeypatch.setattr(jobs, "UWClient", lambda: _FakeUW())
    monkeypatch.setattr(jobs, "_LANE", dict(jobs._LANE))


def _row(jid):
    return rawstore.get("job_runs", jid)


def test_a_dashboard_run_records_manual_dashboard_and_its_registered_name(sync_jobs):
    jobs.register("t_origin", lambda uw, **p: None, [], requires_uw=False)
    jid = jobs.submit("t_origin", {"a": 1})
    jobs.execute(jid)
    row = _row(jid)
    assert (row["trigger"], row["source"], row["registry_name"]) == ("manual", "dashboard", "t_origin")
    assert row["counts"] is None
    assert row["status"] == "done"


def test_the_running_row_already_names_its_origin(sync_jobs):
    seen = []
    jobs.register("t_run_origin",
                  lambda uw, **p: seen.append(_row(jobs.current_job_id())), [], requires_uw=False)
    jobs.execute(jobs.submit("t_run_origin"))
    assert (seen[0]["trigger"], seen[0]["source"], seen[0]["registry_name"]) == (
        "manual", "dashboard", "t_run_origin")
    assert seen[0]["counts"] is None and seen[0]["status"] == "running"


def test_counts_are_stored_when_the_runner_returns_them(sync_jobs):
    def runner(uw, **p):
        return {"counts": {"rows": 12, "failed": 0}}

    jobs.register("t_counts", runner, [], requires_uw=False)
    jid = jobs.submit("t_counts")
    jobs.execute(jid)
    row = _row(jid)
    assert row["counts"] == {"rows": 12, "failed": 0}
    assert (row["trigger"], row["source"], row["registry_name"], row["status"]) == (
        "manual", "dashboard", "t_counts", "done")


def test_an_empty_counts_dict_is_null(sync_jobs):
    jobs.register("t_empty", lambda uw, **p: {"counts": {}}, [], requires_uw=False)
    jid = jobs.submit("t_empty")
    jobs.execute(jid)
    assert _row(jid)["counts"] is None and _row(jid)["status"] == "done"


def test_a_path_result_and_a_job_message_leave_counts_null(sync_jobs):
    from pathlib import Path

    jobs.register("t_path", lambda uw, **p: Path("board.html"), [], requires_uw=False)
    jid = jobs.submit("t_path")
    jobs.execute(jid)
    assert _row(jid)["counts"] is None and _row(jid)["registry_name"] == "t_path"

    def info(uw, **p):
        raise jobs.JobMessage("nothing to do")

    jobs.register("t_msg", info, [], requires_uw=False)
    jid2 = jobs.submit("t_msg")
    jobs.execute(jid2)
    row = _row(jid2)
    assert row["counts"] is None and row["message"] == "nothing to do"
    assert (row["trigger"], row["source"], row["registry_name"]) == ("manual", "dashboard", "t_msg")
