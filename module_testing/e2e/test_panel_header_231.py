"""A write to a live dashboard needs ``X-RC-Panel`` (#231).

The unit tests call ``_local_only`` in a request context; this sends the requests over a
real socket to the real server process. The harness client carries the header (as every
page does), so each refused request here is made with a bare ``httpx`` call on purpose.
"""
from __future__ import annotations

import httpx

REFUSED = {"ok": False, "error": "panel header required: reload this board"}


def _jobs(dashboard) -> list:
    return dashboard.get("/api/jobs").json()["jobs"]


def test_a_write_without_the_header_is_refused_and_starts_nothing(dashboard):
    before = _jobs(dashboard)
    r = httpx.post(dashboard.url + "/run/universe-stats")
    assert r.status_code == 403 and r.json() == REFUSED
    assert _jobs(dashboard) == before


def test_another_localhost_port_is_same_site_and_still_refused(dashboard):
    """A page on localhost:9999 posting here: same-site, loopback Origin, no header."""
    before = _jobs(dashboard)
    r = httpx.post(dashboard.url + "/run/universe-stats",
                   headers={"Origin": "http://localhost:9999", "Sec-Fetch-Site": "same-site"})
    assert r.status_code == 403 and r.json() == REFUSED
    assert _jobs(dashboard) == before


def test_a_preflight_for_the_header_is_not_answered(dashboard):
    """The reason a cross-origin page cannot send it: no CORS grant, ever."""
    r = httpx.options(dashboard.url + "/run/universe-stats", headers={
        "Origin": "http://localhost:9999", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-rc-panel"})
    assert not [h for h in r.headers if h.lower().startswith("access-control-")]


def test_with_the_header_the_same_write_starts_a_job(dashboard):
    r = dashboard.post_fetch("/run/universe-stats")
    assert r.status_code == 202, r.text
    assert dashboard.wait_for_job(r.json()["job_id"]).status == "done"


def test_a_cancel_without_the_header_is_refused(dashboard):
    r = httpx.post(dashboard.url + "/api/job/nope/cancel")
    assert r.status_code == 403 and r.json() == REFUSED
    assert dashboard.client.post("/api/job/nope/cancel").status_code == 404


def test_reads_need_no_header(dashboard):
    assert httpx.get(dashboard.url + "/api/jobs").status_code == 200
