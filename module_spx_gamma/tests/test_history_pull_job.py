"""The ``spxgamma_history`` dashboard job: its registration, its route's gates and the counts it records."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from RevenueChaser.app import dashboard, jobs, runners
from RevenueChaser.core import calendar as nyse_calendar
from RevenueChaser.core import rawstore
from RevenueChaser.module_spx_gamma import history_pull as hp

SAT = datetime(2025, 3, 15, 15, 0, tzinfo=timezone.utc)       # outside any session
OPEN = datetime(2025, 3, 14, 15, 0, tzinfo=timezone.utc)      # a Friday, 11:00 New York
CLI = {"X-RC-Panel": "cli"}
_REAL_SESSION_WINDOW = nyse_calendar.in_us_session_window       # read at import, before any fixture patches it


@pytest.fixture(autouse=True)
def _a_fetch_day(tmp_path, monkeypatch):
    """The board's newest Fetch is the Friday: every range below ends before it."""
    monkeypatch.setattr(hp, "DATA_ROOT", tmp_path)
    folder = tmp_path / "raw" / "2025-03-14"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text('{"origin": "fetch"}', encoding="utf-8")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(nyse_calendar, "_now", lambda: SAT)
    submitted = []
    monkeypatch.setattr(dashboard, "_submit",
                        lambda module, params: submitted.append((module, params)) or ("queued", 202))
    dashboard.app.config["TESTING"] = True
    c = dashboard.app.test_client()
    c.submitted = submitted
    return c


def test_the_job_is_registered_on_the_long_lane_with_checkpoints():
    assert jobs._RUNNERS["spxgamma_history"] is runners.run_spxgamma_history
    assert jobs._LANE["spxgamma_history"] == "long"
    assert jobs._CANCELLABLE["spxgamma_history"] is True
    assert jobs._REQUIRES_UW["spxgamma_history"] is True
    assert jobs._DEADLINE["spxgamma_history"] is None


def test_the_route_takes_its_range_from_the_query_string(client):
    r = client.post("/run/spxgamma_history?start=2024-10-25&end=2025-03-14", headers=CLI)
    assert r.status_code == 202
    assert client.submitted == [("spxgamma_history", {"start": "2024-10-25", "end": "2025-03-14"})]


def test_the_route_passes_the_cap_and_the_pause(client):
    client.post("/run/spxgamma_history", data={"start": "2025-03-10", "end": "2025-03-14", "max_calls": "260",
                                               "sleep": "1.5"}, headers=CLI)
    assert client.submitted == [("spxgamma_history", {"start": "2025-03-10", "end": "2025-03-14",
                                                      "max_calls": 260, "sleep_s": 1.5})]


@pytest.mark.parametrize("query", [
    "",                                          # no range
    "start=2025-03-10",                          # no end
    "start=2025-03-14&end=2025-03-10",           # reversed
    "start=2025-03-10&end=2025-03-15",           # today (New York) is not a past day
    "start=2025-03-10&end=2025-03-14&max_calls=0",
    "start=2025-03-10&end=2025-03-14&max_calls=150001",      # above UW's daily budget
    "start=2025-03-10&end=2025-03-14&max_calls=lots",
    "start=2025-03-10&end=2025-03-14&sleep=-1",
    "start=2025-03-10&end=2025-03-14&sleep=61",
])
def test_the_route_refuses_bad_input(client, query):
    assert client.post(f"/run/spxgamma_history?{query}", headers=CLI).status_code == 400
    assert client.submitted == []


def test_the_route_refuses_during_the_session(client, monkeypatch):
    # this package's conftest pins the session shut; put the real window back and stand inside it
    monkeypatch.setattr(nyse_calendar, "in_us_session_window", _REAL_SESSION_WINDOW)
    monkeypatch.setattr(nyse_calendar, "_now", lambda: OPEN)
    r = client.post("/run/spxgamma_history?start=2025-03-10&end=2025-03-13", headers=CLI)
    assert r.status_code == 409 and "session" in r.get_json()["error"]
    assert client.submitted == []


@pytest.mark.parametrize("busy", ["spxgamma", "spxgamma_stored", "spxgamma_history"])
def test_the_route_refuses_while_an_spx_job_is_queued_or_running(client, monkeypatch, busy):
    monkeypatch.setattr(jobs, "_JOBS", {"j1": {"id": "j1", "module": busy, "status": "running"}})
    r = client.post("/run/spxgamma_history?start=2025-03-10&end=2025-03-13", headers=CLI)
    assert r.status_code == 409 and r.get_json()["job_id"] == "j1"
    assert client.submitted == []


def test_the_route_needs_the_panel_header(client):
    assert client.post("/run/spxgamma_history?start=2025-03-10&end=2025-03-13").status_code == 403


# ── the job end to end on the queue (synchronously), with the pull faked ──
class _FakeUW:
    def verify(self):
        return True, None


@pytest.fixture
def sync_jobs(monkeypatch):
    with jobs._LOCK:
        jobs._JOBS.clear()
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)
    monkeypatch.setattr(jobs, "_ensure_long_worker", lambda: None)
    monkeypatch.setattr(jobs, "UWClient", lambda: _FakeUW())
    yield
    jobs._LONG_QUEUE.queue.clear()


def test_the_runs_row_records_the_counts(sync_jobs, monkeypatch):
    seen = {}

    def fake_pull(uw, start, end, **kw):
        seen.update(uw=uw, start=start, end=end, **kw)
        return {"counts": {"calls": 27, "status": {"200": 25, "429": 1, "422": 1}, "written": 2}}
    monkeypatch.setattr(hp, "pull_history", fake_pull)
    jid = jobs.submit("spxgamma_history", {"start": "2025-03-10", "end": "2025-03-12", "max_calls": 40})
    jobs.execute(jid)
    row = rawstore.get("job_runs", jid)
    assert row["status"] == "done" and row["module"] == "spxgamma_history" and row["lane"] == "long"
    assert row["counts"] == {"calls": 27, "status": {"200": 25, "429": 1, "422": 1}, "written": 2}
    assert isinstance(seen["uw"], _FakeUW) and seen["max_calls"] == 40 and "sleep_s" not in seen


def test_each_run_leaves_its_counts_on_disk(tmp_path, monkeypatch):
    """The dashboard holds the store, so a run's counts are also a file the owner can open while it runs."""
    monkeypatch.setattr(nyse_calendar, "_now", lambda: SAT)
    from RevenueChaser.module_spx_gamma import snapshot as snap
    monkeypatch.setattr(snap, "stored_spx_candles", lambda asof=None, ticker=None: [])

    class _UW:
        def get_result(self, ep, params=None):
            raise AssertionError("no day has a stored close: nothing may be asked but the iv-rank series")
    monkeypatch.setattr(hp, "_iv_rank_series", lambda *a, **k: None)
    counts = hp.pull_history(_UW(), "2025-03-10", "2025-03-12", data_root=tmp_path)["counts"]
    reports = list((tmp_path / "history_pull").glob("run_*.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text(encoding="utf-8")) == counts
    assert counts["report_file"] == str(reports[0].relative_to(tmp_path))


# ── both directions with real worker threads (#194 review MUST): the Fetch and the pull never overlap ──
@pytest.fixture
def real_lanes(monkeypatch):
    """Jobs submitted to the real registry and run by ``jobs.execute`` on real threads, with blocking stand-ins
    for the two SPX runners.

    The module's queues, worker flags and registry are swapped for fresh ones (the ``long_lane`` pattern of
    app/tests/test_job_deadline_cancel_243.py), and no ``rc-jobs`` worker is started: a worker left on the real
    queues takes a later test file's jobs as well (#194 delta review M1). Each job runs on a thread this fixture
    owns and joins, so nothing outlives the test.
    """
    import queue
    import threading

    workers_before = {t for t in threading.enumerate() if t.name.startswith("rc-jobs")}
    monkeypatch.setattr(jobs, "_JOB_QUEUE", queue.Queue())
    monkeypatch.setattr(jobs, "_LONG_QUEUE", queue.Queue())
    monkeypatch.setattr(jobs, "_WORKER_STARTED", False)
    monkeypatch.setattr(jobs, "_LONG_STARTED", False)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_SEQ", {})
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)
    monkeypatch.setattr(jobs, "_ensure_long_worker", lambda: None)
    monkeypatch.setattr(jobs, "UWClient", lambda: _FakeUW())
    started, release = threading.Event(), threading.Event()
    threads: list[threading.Thread] = []

    def blocking(uw, **params):
        started.set()
        release.wait(10)
    monkeypatch.setitem(jobs._RUNNERS, "spxgamma_history", blocking)
    monkeypatch.setitem(jobs._RUNNERS, "spxgamma", blocking)

    def run(module, params):
        jid = jobs.submit(module, params)
        t = threading.Thread(target=jobs.execute, args=(jid,), name=f"test-lane-{module}", daemon=True)
        t.start()
        threads.append(t)
        return jid
    run.started = started
    yield run
    release.set()
    for t in threads:                                    # the end is recorded before the store goes
        t.join(10)
    assert not [t for t in threads if t.is_alive()], "a job thread outlived the test"
    left = {t for t in threading.enumerate() if t.name.startswith("rc-jobs") and t.is_alive()} - workers_before
    assert not left, f"worker threads left alive: {sorted(t.name for t in left)}"


def _wait_done(jid):
    import time
    for _ in range(200):
        if jobs.get(jid)["status"] in ("done", "error", "cancelled"):
            return
        time.sleep(0.05)


def test_a_board_fetch_is_refused_while_the_pull_runs(real_lanes, monkeypatch):
    from RevenueChaser.core.tests.panel_client import panel_test_client
    from RevenueChaser.module_spx_gamma import run as spx_run
    monkeypatch.setattr(nyse_calendar, "_now", lambda: SAT)
    jid = real_lanes("spxgamma_history", {"start": "2025-03-10", "end": "2025-03-12"})
    assert real_lanes.started.wait(5)
    assert jobs.get(jid)["status"] == "running" and jobs.get(jid)["lane"] == "long"
    client = panel_test_client(dashboard.app)
    r = client.post("/run/spxgamma", data={"date": "2025-03-14"})
    assert r.status_code == 409 and r.get_json()["job_id"] == jid
    monkeypatch.setattr(spx_run, "serves_from_disk", lambda day, root=None: True)     # a stored past day
    r = client.post("/run/spxgamma", data={"date": "2025-03-12"})
    assert r.status_code == 409 and r.get_json()["job_id"] == jid


def test_the_pull_is_refused_while_a_board_fetch_runs(real_lanes, monkeypatch):
    from RevenueChaser.core.tests.panel_client import panel_test_client
    monkeypatch.setattr(nyse_calendar, "_now", lambda: SAT)
    jid = real_lanes("spxgamma", {"asof_date": "2025-03-14"})
    assert real_lanes.started.wait(5)
    assert jobs.get(jid)["status"] == "running" and jobs.get(jid)["lane"] == "short"
    r = panel_test_client(dashboard.app).post("/run/spxgamma_history?start=2025-03-10&end=2025-03-12")
    assert r.status_code == 409 and r.get_json()["job_id"] == jid


def test_the_route_refuses_an_end_after_the_newest_fetch_day(client, tmp_path):
    (tmp_path / "raw" / "2025-03-14").rename(tmp_path / "raw" / "2025-03-12")
    r = client.post("/run/spxgamma_history?start=2025-03-10&end=2025-03-13", headers=CLI)
    assert r.status_code == 400 and "newest Fetch" in r.get_json()["error"]
    assert client.post("/run/spxgamma_history?start=2025-03-10&end=2025-03-12", headers=CLI).status_code == 202


def test_the_route_queues_iso_dates(client):
    client.post("/run/spxgamma_history?start=20250310&end=2025-W11-3", headers=CLI)
    assert client.submitted == [("spxgamma_history", {"start": "2025-03-10", "end": "2025-03-12"})]


def test_the_runs_row_gets_compact_counts(sync_jobs, monkeypatch):
    """The answer shapes and the failure list stay in the run file; the store row stays small (#194 nit)."""
    big = {"calls": 400, "status": {"200": 400}, "failed": 30, "report_file": "history_pull/run_x.json",
           "failures": [{"day": f"2025-01-{d:02d}", "statuses": [422], "error": "x" * 200} for d in range(1, 21)],
           "shapes": {"sessions": {f"2025-02-{d:02d}": {"interpolated_iv": {"keys": ["k"] * 20}}
                                   for d in range(1, 21)}, "iv_rank": {"rows": 500}}}
    monkeypatch.setattr(hp, "pull_history", lambda uw, start, end, **kw: {"counts": big})
    jid = jobs.submit("spxgamma_history", {"start": "2025-03-10", "end": "2025-03-12"})
    jobs.execute(jid)
    counts = rawstore.get("job_runs", jid)["counts"]
    assert "shapes" not in counts and len(counts["failures"]) == hp.ROW_FAILURES == 3
    assert counts["calls"] == 400 and counts["failed"] == 30 and counts["report_file"] == "history_pull/run_x.json"
    assert len(json.dumps(counts)) < 2000


def test_the_route_refuses_when_the_session_clock_cannot_tell(client, monkeypatch):
    """Past the holiday table the session window raises: that is not "closed", so the pull does not start."""
    def unknown(now=None):
        raise ValueError("no holiday table for 2029")
    monkeypatch.setattr(nyse_calendar, "in_us_session_window", unknown)
    r = client.post("/run/spxgamma_history?start=2025-03-10&end=2025-03-13", headers=CLI)
    assert r.status_code == 409 and client.submitted == []
