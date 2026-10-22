"""PL6-a (#26): the 13F jobs are one at a time, and repeated clicks do not queue a second long-lane run.

Two halves:

* the three 13F runners take ``job_lock`` name ``"sec_13f"`` for the whole composed job — the engine functions are
  building blocks, the job is the unit — so a second Fetch, rebuild or map refuses and writes nothing;
* ``/run/module_sec_13f``, its ``-rebuild`` / ``-map`` siblings, ``/run/identity``, ``/run/edgar-sync``,
  ``/run/daily-bars``, ``/run/index-lists`` and ``/run/universe-stats`` answer ``409`` while a job of their family is
  queued or running, instead of queueing a second multi-minute ingest behind the first.

The universe run and the single-board Fetches keep their own locks (``refused_during_universe``, #244/#356) and are
not touched here.
"""
from __future__ import annotations

import os

import pytest

from RevenueChaser.app import dashboard, jobs, runners
from RevenueChaser.core import job_lock, rawstore
from RevenueChaser.core.tests.lock_holders import lock_held
from RevenueChaser.core.tests.panel_client import panel_test_client


# ── the 13F runners: one family lock ─────────────────────────────────────────

_13F_RUNNERS = [("build", runners.build_sec13f), ("rebuild", runners.run_sec13f_rebuild),
                ("map", runners.run_sec13f_map)]


@pytest.mark.parametrize("runner", [r for _, r in _13F_RUNNERS], ids=[n for n, _ in _13F_RUNNERS])
def test_each_13f_runner_refuses_while_the_family_lock_is_held(rawstore_tmp_db, runner):
    rawstore.write_rows("sec13f_totals", [{"ticker": "AAPL", "entry": {"2026-03-31": {"shares": 1.0}}}])
    before = rawstore.query("SELECT * FROM sec13f_totals")
    with lock_held("sec_13f"):
        with pytest.raises(jobs.JobMessage) as err:
            runner()
    assert err.value.message == f"A 13F job is already running (pid {os.getpid()}); nothing was written."
    assert rawstore.query("SELECT * FROM sec13f_totals") == before, "a refused run wrote the totals"


def test_a_refused_fetch_downloads_nothing(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_sec_13f import engine as sec_13f

    called: list[tuple] = []
    monkeypatch.setattr(sec_13f, "download_missing_zips", lambda *a, **k: called.append(a) or [])
    with lock_held("sec_13f"):
        with pytest.raises(jobs.JobMessage):
            runners.build_sec13f()
    assert called == [], "the lock is taken before the first download"


def test_a_finished_13f_run_releases_the_lock(rawstore_tmp_db):
    with pytest.raises(jobs.JobMessage):              # an empty store: "nothing to rebuild", a normal short run
        runners.run_sec13f_rebuild()
    assert "sec_13f" not in dict(job_lock.holders()), "the lock outlived the run"


# ── the run routes: a second click is a 409, not a second job ────────────────

FAMILIES = [
    ("/run/module_sec_13f", "module_sec_13f"),
    ("/run/module_sec_13f-rebuild", "sec13f_rebuild"),
    ("/run/module_sec_13f-map", "sec13f_map"),
    ("/run/identity", "identity"),
    ("/run/edgar-sync", "edgar_sync"),
    ("/run/daily-bars", "daily_bars"),
    ("/run/index-lists", "index_lists"),
    ("/run/universe-stats", "universe_stats"),
]


class _FakeUW:
    """A UW client that verifies: these route tests must not reach the network."""

    def verify(self):
        return True, None

    def close(self):
        pass


@pytest.fixture(autouse=True)
def queued_jobs_only(monkeypatch):
    """Submit without starting a worker and without a real UW probe; the registry starts empty."""
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)
    monkeypatch.setattr(jobs, "_ensure_long_worker", lambda: None)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_SEQ", {})
    monkeypatch.setattr(dashboard, "_uw", lambda: _FakeUW())


@pytest.fixture
def client():
    return panel_test_client(dashboard.app)


def _queued(module: str) -> list[dict]:
    return [j for j in jobs.list_jobs() if j["module"] == module]


@pytest.mark.parametrize("route,module", FAMILIES)
def test_a_second_post_is_refused_while_the_first_is_queued(client, route, module):
    first = client.post(route)
    assert first.status_code == 202, first.get_json()
    second = client.post(route)
    assert second.status_code == 409
    body = second.get_json()
    assert body["ok"] is False and "already queued or running" in body["error"]
    assert len(_queued(module)) == 1, "the refused click queued a second job"


def test_the_three_13f_routes_share_one_family(client):
    assert client.post("/run/module_sec_13f").status_code == 202
    for route in ("/run/module_sec_13f-rebuild", "/run/module_sec_13f-map"):
        r = client.post(route)
        assert r.status_code == 409
        assert r.get_json()["error"] == "A 13F job is already queued or running."
    assert _queued("sec13f_rebuild") == [] and _queued("sec13f_map") == []


def test_an_unrelated_route_still_submits(client):
    assert client.post("/run/module_sec_13f").status_code == 202
    assert client.post("/run/module_value_chain").status_code == 202
