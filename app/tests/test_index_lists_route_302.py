"""The index lists job and route (#302, D24): long lane, needs UW, the sp600 first fill is a named parameter."""
from __future__ import annotations

import pytest

from RevenueChaser.app import dashboard, runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.core import index_registry as ir
from RevenueChaser.core.tests.panel_client import panel_test_client


def _client():
    return panel_test_client(dashboard.app)


@pytest.fixture
def submitted(monkeypatch):
    import RevenueChaser.app.dashboard as d

    class _UW:
        def verify(self):
            return (True, None)

        def close(self):
            pass

    monkeypatch.setattr(d, "_uw", lambda: _UW())
    captured: list[tuple] = []
    monkeypatch.setattr(d.jobs, "submit", lambda module, params: captured.append((module, params)) or "stubjob")
    return captured


def test_registered_long_lane_and_requires_uw():
    from RevenueChaser.app import jobs
    assert jobs._LANE["index_lists"] == "long"
    assert jobs._REQUIRES_UW["index_lists"] is True


def test_route_submits_the_weekly_run_with_no_params(submitted):
    r = _client().post("/run/index-lists")
    assert r.status_code == 202 and r.get_json()["job_id"] == "stubjob"
    assert submitted == [("index_lists", {})]


def test_route_passes_the_sp600_first_fill(submitted):
    r = _client().post("/run/index-lists", data={"first_fill": "sp600"})
    assert r.status_code == 202
    assert submitted == [("index_lists", {"first_fill": ["sp600"]})]


@pytest.mark.parametrize("value", ["sp500", "nasdaq", "sp600,dow", "x"])
def test_a_first_fill_of_another_slug_is_400(submitted, value):
    r = _client().post("/run/index-lists", data={"first_fill": value})
    assert r.status_code == 400 and r.get_json()["ok"] is False and submitted == []


def test_the_route_needs_the_panel_header():
    r = dashboard.app.test_client().post("/run/index-lists")
    assert r.status_code == 403


def _result(statuses: dict, locked: bool = False) -> dict:
    return {"run_id": "r1", "locked": locked, "slugs": {
        s: {"status": st, "members": 500, "floor": 400, "appended": 0} for s, st in statuses.items()}}


def test_the_runner_hands_its_run_id_and_first_fill_to_the_step(monkeypatch):
    seen = {}
    monkeypatch.setattr(runners, "run_id", lambda: "job-1")
    monkeypatch.setattr(ir, "refresh_lists",
                        lambda run_id, first_fill=(): seen.update(run_id=run_id, first_fill=list(first_fill))
                        or _result({"sp500": "written"}))
    with pytest.raises(JobMessage, match="Index lists: sp500 500"):
        runners.run_index_lists(first_fill=["sp600"])
    assert seen == {"run_id": "job-1", "first_fill": ["sp600"]}


def test_a_list_under_its_floor_fails_the_job(monkeypatch):
    monkeypatch.setattr(ir, "refresh_lists", lambda run_id, first_fill=(): _result({"sp500": "below_floor"}))
    with pytest.raises(RuntimeError, match="below floor"):
        runners.run_index_lists()


def test_a_running_copy_is_a_message_not_a_failure(monkeypatch):
    monkeypatch.setattr(ir, "refresh_lists", lambda run_id, first_fill=(): _result({}, locked=True))
    with pytest.raises(JobMessage, match="already running"):
        runners.run_index_lists()
