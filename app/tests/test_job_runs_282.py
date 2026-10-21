"""#282 J4: every dashboard job leaves one ``job_runs`` row, and a failing write never fails the job."""

import pytest

from RevenueChaser.app import jobs
from RevenueChaser.core import ledger, rawstore


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


def test_a_finished_job_has_one_done_row_with_its_times_and_params(sync_jobs):
    jobs.register("t_ok", lambda uw, **p: None, [], requires_uw=False)
    jid = jobs.submit("t_ok", {"a": 1})
    jobs.execute(jid)
    row = _row(jid)
    assert row["run_id"] == jid and row["job_id"] == jid
    assert (row["module"], row["lane"], row["status"]) == ("t_ok", "short", "done")
    assert row["params"] == {"a": 1}
    assert row["queued_at"] <= row["started_at"] <= row["finished_at"]
    assert row["pid"] > 0
    assert rawstore.count("job_runs") == 1


def test_a_failing_job_has_an_error_row_with_the_message(sync_jobs):
    def boom(uw, **p):
        raise RuntimeError("kaput")

    jobs.register("t_boom", boom, [], requires_uw=False)
    jid = jobs.submit("t_boom")
    jobs.execute(jid)
    row = _row(jid)
    assert row["status"] == "error" and "kaput" in row["error"] and row["finished_at"] is not None


def test_a_job_message_is_stored_on_a_done_row(sync_jobs):
    def info(uw, **p):
        raise jobs.JobMessage("nothing to do")

    jobs.register("t_msg", info, [], requires_uw=False)
    jid = jobs.submit("t_msg")
    jobs.execute(jid)
    row = _row(jid)
    assert (row["status"], row["message"]) == ("done", "nothing to do")


def test_the_row_is_already_running_while_the_runner_works(sync_jobs):
    seen = []
    jobs.register("t_run", lambda uw, **p: seen.append(_row(jobs.current_job_id())), [], requires_uw=False)
    jid = jobs.submit("t_run")
    jobs.execute(jid)
    assert seen[0]["status"] == "running" and seen[0]["finished_at"] is None
    assert _row(jid)["status"] == "done"


def test_a_uw_key_failure_and_an_unknown_module_leave_error_rows(sync_jobs, monkeypatch):
    jobs.register("t_uw", lambda uw, **p: None, [], requires_uw=True)
    monkeypatch.setattr(jobs, "UWClient", lambda: type("U", (), {"verify": lambda s: (False, "no key")})())
    jid = jobs.submit("t_uw")
    jobs.execute(jid)
    assert (_row(jid)["status"], _row(jid)["error"]) == ("error", "no key")
    jid2 = jobs.submit("t_gone")
    jobs.execute(jid2)
    assert _row(jid2)["status"] == "error" and "unknown module" in _row(jid2)["error"]


def test_a_cancelled_job_leaves_a_cancelled_row(sync_jobs):
    jobs.register("t_c", lambda uw, **p: None, [], requires_uw=False)
    jid = jobs.submit("t_c")
    assert jobs.cancel(jid)
    assert _row(jid)["status"] == "cancelled"


def test_the_long_lane_is_recorded(sync_jobs):
    jobs.register("t_long", lambda uw, **p: None, [], requires_uw=False, lane="long")
    jid = jobs.submit("t_long")
    jobs.execute(jid)
    assert _row(jid)["lane"] == "long"


def test_a_failing_job_runs_write_never_fails_the_job(sync_jobs, monkeypatch, caplog):
    def broken(name, rows):
        raise OSError("disk full")

    monkeypatch.setattr(rawstore, "write_rows", broken)
    ran = []
    jobs.register("t_ok2", lambda uw, **p: ran.append(1), [], requires_uw=False)
    jid = jobs.submit("t_ok2")
    jobs.execute(jid)
    assert ran == [1]
    assert jobs.get(jid)["status"] == "done" and jobs.get(jid)["error"] is None
    assert "job_runs" in caplog.text


def test_params_that_are_not_json_do_not_fail_the_job(sync_jobs):
    jobs.register("t_p", lambda uw, **p: None, [], requires_uw=False)
    jid = jobs.submit("t_p", {"when": object()})
    jobs.execute(jid)
    assert jobs.get(jid)["status"] == "done"
    assert _row(jid)["status"] == "done"


def test_run_id_is_the_running_jobs_id_and_a_fresh_one_outside(sync_jobs):
    seen = []
    jobs.register("t_r", lambda uw, **p: seen.append(jobs.run_id()), [], requires_uw=False)
    jid = jobs.submit("t_r")
    jobs.execute(jid)
    assert seen == [jid]
    a, b = jobs.run_id(), jobs.run_id()
    assert a != b and len(a) == len(ledger.new_run_id())


def test_job_semantics_are_unchanged_by_the_new_row(sync_jobs):
    jobs.register("t_s", lambda uw, **p: None, [], requires_uw=False)
    jid = jobs.submit("t_s")
    jobs.execute(jid)
    # #243 added the deadline and cancel fields to the job JSON; the job_runs row needed no new column.
    assert set(jobs.get(jid)) == {"id", "module", "params", "status", "produces", "queued_at", "started_at",
                                  "finished_at", "error", "message", "notes", "lane",
                                  "deadline_s", "deadline_at", "cancellable", "cancel_requested", "phase"}


def test_the_row_is_stored_before_the_registry_shows_the_new_status(sync_jobs, monkeypatch):
    """A poller (the e2e harness, the UI) that sees "done" may open the store at once. If the final row were
    written after that, the dashboard's writer would still hold the file and the poller's connect would fail
    with "Conflicting lock"."""
    seen = []
    real = rawstore.write_rows

    def spy(name, rows):
        seen.append((rows[0]["status"], jobs.get(rows[0]["run_id"])["status"]))
        return real(name, rows)

    monkeypatch.setattr(rawstore, "write_rows", spy)
    jobs.register("t_order", lambda uw, **p: None, [], requires_uw=False)
    jobs.execute(jobs.submit("t_order"))
    assert seen == [("running", "running"), ("done", "running")]


def test_params_are_stored_as_a_json_object_that_sql_can_read(sync_jobs):
    """A dict that was already dumped to text would be dumped again: a JSON string, ``params->>'$.a'`` NULL."""
    jobs.register("t_json", lambda uw, **p: None, [], requires_uw=False)
    jid = jobs.submit("t_json", {"a": 1})
    jobs.execute(jid)
    with rawstore.connect(read_only=True) as conn:
        kind, value = conn.execute("SELECT json_type(params), params->>'$.a' FROM job_runs WHERE run_id = ?",
                                   [jid]).fetchone()
    assert (kind, value) == ("OBJECT", "1")


def test_started_at_and_finished_at_are_their_own_times(sync_jobs, monkeypatch):
    clock = iter(f"2026-10-07T10:00:{n:02d}+00:00" for n in range(10, 60))
    monkeypatch.setattr(jobs, "_utc_now", lambda: next(clock))
    jobs.register("t_clock", lambda uw, **p: None, [], requires_uw=False)
    jid = jobs.submit("t_clock")                  # queued 10:00:10
    jobs.execute(jid)                             # running 10:00:11, done 10:00:12
    row = _row(jid)
    assert (row["started_at"] - row["queued_at"], row["finished_at"] - row["started_at"]) == (1, 1)


def test_a_slow_store_does_not_hold_the_job_up(sync_jobs, monkeypatch):
    """The write asks for the brief lock wait (a peer holding the file would otherwise cost ~29 s per write)."""
    flags = []

    def spy(name, rows):
        flags.append(getattr(rawstore._LOCAL, "brief", False))

    monkeypatch.setattr(rawstore, "write_rows", spy)
    jobs.register("t_brief", lambda uw, **p: None, [], requires_uw=False)
    jobs.execute(jobs.submit("t_brief"))
    assert flags == [True, True]
    assert getattr(rawstore._LOCAL, "brief", False) is False
