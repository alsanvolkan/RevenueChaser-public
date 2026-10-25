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
