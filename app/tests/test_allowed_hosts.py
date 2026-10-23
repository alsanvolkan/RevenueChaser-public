"""``RC_ALLOWED_HOSTS``: extra ``Host`` names the dashboard answers to.

The dashboard answers only requests whose ``Host`` is a loopback name (``_local_only``), which is
what keeps a DNS-rebinding page out. A machine reached over a private network by its own name
sends that name in ``Host`` instead, so the guard refuses it. This variable names the extra hosts,
exactly; the default stays loopback only.
"""
from __future__ import annotations

import pytest

from RevenueChaser.app import dashboard

TAILNET = "http://homesrv.tail1234.ts.net:8000"
FORBIDDEN = {"ok": False, "error": "forbidden host"}


@pytest.fixture(autouse=True)
def no_variable(monkeypatch):
    """Whatever the developer's environment holds, each test states its own value."""
    monkeypatch.delenv("RC_ALLOWED_HOSTS", raising=False)


def _guard(base_url=TAILNET, method="GET", headers=None):
    with dashboard.app.test_request_context("/api/jobs", base_url=base_url, method=method,
                                            headers=headers or {}):
        return dashboard._local_only()


def test_by_default_a_non_loopback_host_is_refused():
    body, status = _guard()
    assert status == 403 and body.get_json() == FORBIDDEN


def test_an_empty_variable_changes_nothing(monkeypatch):
    monkeypatch.setenv("RC_ALLOWED_HOSTS", "   ")
    assert _guard(base_url="http://127.0.0.1:8000") is None
    body, status = _guard()
    assert status == 403 and body.get_json() == FORBIDDEN


def test_a_listed_host_is_answered(monkeypatch):
    monkeypatch.setenv("RC_ALLOWED_HOSTS", "homesrv.tail1234.ts.net")
    assert _guard() is None


def test_loopback_is_still_answered_when_the_variable_names_another_host(monkeypatch):
    monkeypatch.setenv("RC_ALLOWED_HOSTS", "homesrv.tail1234.ts.net")
    assert _guard(base_url="http://localhost:8000") is None


def test_several_hosts_are_comma_separated_and_the_port_and_case_are_ignored(monkeypatch):
    monkeypatch.setenv("RC_ALLOWED_HOSTS", " HomeSrv.tail1234.ts.net:8000 , other.example ")
    assert _guard() is None
    assert _guard(base_url="http://other.example:8000") is None


def test_a_wildcard_entry_is_dropped_so_it_cannot_widen_to_a_suffix(monkeypatch):
    """A wildcard is not a host name: accepting one would open every name under it."""
    monkeypatch.setenv("RC_ALLOWED_HOSTS", "*.ts.net")
    body, status = _guard()
    assert status == 403 and body.get_json() == FORBIDDEN
    assert dashboard._extra_allowed_hosts() == frozenset()


def test_an_unlisted_host_is_still_refused_while_another_is_listed(monkeypatch):
    monkeypatch.setenv("RC_ALLOWED_HOSTS", "other.example")
    body, status = _guard()
    assert status == 403 and body.get_json() == FORBIDDEN


def test_a_write_from_a_listed_hosts_own_page_passes_the_guard(monkeypatch):
    """The page the dashboard serves over that name sends it as ``Origin`` too."""
    monkeypatch.setenv("RC_ALLOWED_HOSTS", "homesrv.tail1234.ts.net")
    assert _guard(method="POST", headers={"X-RC-Panel": "dashboard",
                                          "Origin": "http://homesrv.tail1234.ts.net:8000"}) is None


def test_a_write_from_another_site_is_still_refused_over_a_listed_host(monkeypatch):
    monkeypatch.setenv("RC_ALLOWED_HOSTS", "homesrv.tail1234.ts.net")
    body, status = _guard(method="POST", headers={"X-RC-Panel": "dashboard",
                                                  "Origin": "https://evil.example"})
    assert status == 403 and body.get_json()["error"] == "cross-site request refused"


def test_the_variable_is_read_per_request_not_at_import(monkeypatch):
    body, status = _guard()
    assert status == 403
    monkeypatch.setenv("RC_ALLOWED_HOSTS", "homesrv.tail1234.ts.net")
    assert _guard() is None
