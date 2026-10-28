"""PR 3, sub-step 5: the SPX Gamma Fetch also asks the builder for the two datasets the board reads (A4).

``POST /run/spxgamma`` puts one ``uw/spot_expiry_strike`` and one ``market_series`` request into ``ops.sqlite``
(``ops.enqueue_once``, ``requested_by`` ``spx_gamma``), then submits the board's own job as before and renders at once
with what is stored; it never waits for the builder. A request already queued is fine and adds nothing.
"""
from __future__ import annotations

import sqlite3

import pytest

import RevenueChaser.app.dashboard as d
from RevenueChaser.core.tests.panel_client import panel_test_client
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.tests.conftest import market_roots  # noqa: F401  (each test its own ops.sqlite)

DATASETS = ("uw/spot_expiry_strike", "market_series")


class _FakeUW:
    def verify(self):
        return True, None


@pytest.fixture
def submitted(monkeypatch):
    jobs: list[tuple[str, dict]] = []

    def submit(module, params):
        jobs.append((module, params))
        return f"job{len(jobs)}"
    monkeypatch.setattr(d, "UWClient", lambda: _FakeUW())
    monkeypatch.setattr(d.jobs, "submit", submit)
    monkeypatch.setattr(d.jobs, "active_id", lambda modules: None)
    return jobs


def _client():
    d.app.config["TESTING"] = True
    return panel_test_client(d.app)


def _requests() -> list[tuple]:
    ops.ensure()
    with ops.connect() as con:
        return [tuple(r) for r in con.execute(
            "SELECT dataset, requested_by, status FROM build_requests ORDER BY id").fetchall()]


def test_a_fetch_queues_one_request_per_dataset_and_submits_the_board_job(submitted):
    r = _client().post("/run/spxgamma", data={"date": "2026-10-26"})
    assert r.status_code == 202
    assert _requests() == [(ds, "spx_gamma", "queued") for ds in DATASETS]
    module, params = submitted[0]
    assert module == "spxgamma" and params["asof_date"] == "2026-10-26"
    req = params["builder_request"]
    assert req["requested_at"] and set(req["datasets"]) == set(DATASETS)
    assert set(req["datasets"].values()) == {"queued"}


def test_a_second_fetch_while_queued_adds_none_and_still_runs(submitted):
    c = _client()
    assert c.post("/run/spxgamma", data={"date": "2026-10-26"}).status_code == 202
    r = c.post("/run/spxgamma", data={"date": "2026-10-26"})
    assert r.status_code == 202, "an already queued request is fine"
    assert len(_requests()) == 2
    assert set(submitted[1][1]["builder_request"]["datasets"].values()) == {"already queued"}


def test_a_stored_past_day_fetch_queues_them_too(submitted, monkeypatch):
    from RevenueChaser.module_spx_gamma import run as spx_run
    monkeypatch.setattr(spx_run, "serves_from_disk", lambda asof, data_root=None: True)
    r = _client().post("/run/spxgamma", data={"date": "2026-10-23"})
    assert r.status_code == 202 and submitted[0][0] == "spxgamma_stored"
    assert len(_requests()) == 2 and "builder_request" in submitted[0][1]


def test_a_write_without_the_panel_header_is_refused_and_queues_nothing(submitted):
    d.app.config["TESTING"] = True
    r = d.app.test_client().post("/run/spxgamma", data={"date": "2026-10-26"})
    assert r.status_code == 403
    assert _requests() == [] and submitted == []


def test_the_history_pull_still_refuses_the_fetch_with_409_and_nothing_is_queued(submitted, monkeypatch):
    monkeypatch.setattr(d.jobs, "active_id", lambda modules: "hist1" if "spxgamma_history" in modules else None)
    r = _client().post("/run/spxgamma", data={"date": "2026-10-26"})
    assert r.status_code == 409 and r.get_json()["job_id"] == "hist1"
    assert _requests() == [] and submitted == []


def test_a_locked_queue_never_fails_the_fetch(submitted, monkeypatch):
    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(ops, "enqueue_once", locked)
    r = _client().post("/run/spxgamma", data={"date": "2026-10-26"})
    assert r.status_code == 202
    notes = submitted[0][1]["builder_request"]["datasets"]
    assert all(v.startswith("not queued") and "locked" in v for v in notes.values())


def test_the_board_says_the_builder_was_asked_and_when_it_runs():
    from RevenueChaser.module_spx_gamma import render
    from RevenueChaser.module_spx_gamma.tests.conftest import make_snapshot
    snap = make_snapshot()
    snap["builder_request"] = {"requested_at": "2026-10-26 16:40 New York",
                               "datasets": {"uw/spot_expiry_strike": "queued", "market_series": "already queued"}}
    html = render.render_html(snap)
    assert "builder requested at 2026-10-26 16:40 New York" in html
    assert "the builder runs hourly at :07" in html
