"""/api/automations routes and the rule that nothing reaches the scheduler on its own (#130)."""
from __future__ import annotations

import pytest

from RevenueChaser.app import dashboard
from RevenueChaser.core import automations as A


@pytest.fixture
def fake():
    f = A.FakeScheduler()
    A.set_scheduler(f)
    yield f
    A.set_scheduler(None)


_HDR = {"X-RC-Panel": "automations"}


class _Client:
    """The test client, sending the panel's header on POST like the real panel does."""

    def __init__(self, c):
        self._c = c

    def post(self, *a, headers=None, **k):
        return self._c.post(*a, headers={**_HDR, **(headers or {})}, **k)

    def __getattr__(self, name):
        return getattr(self._c, name)


@pytest.fixture
def client():
    return _Client(dashboard.app.test_client())


def test_get_lists_registry_rows_with_live_state(client, fake):
    d = client.get("/api/automations").get_json()
    assert d["ok"] and d["available"]
    assert [r["name"] for r in d["rows"]] == [e.name for e in A.REGISTRY]
    assert {r["state"] for r in d["rows"]} == {"not_installed"}
    assert fake.mutations == []


def test_post_install_runs_the_action_and_returns_the_row(client, fake):
    r = client.post("/api/automations/RC-Store-Backup/install")
    assert r.status_code == 200
    assert r.get_json()["row"]["state"] == "enabled"
    assert fake.mutations == [("install", "RC-Store-Backup")]


@pytest.mark.parametrize("action", ["enable", "disable", "remove", "run"])
def test_post_other_actions(client, fake, action):
    assert client.post(f"/api/automations/RC-Store-Backup/{action}").status_code == 200
    assert fake.mutations == [(action, "RC-Store-Backup")]


def test_unknown_name_is_404_and_touches_nothing(client, fake):
    r = client.post("/api/automations/Some%20Other%20Task/remove")
    assert r.status_code == 404
    assert "Some" not in r.get_data(as_text=True)
    assert fake.calls == []


def test_unknown_action_is_400(client, fake):
    assert client.post("/api/automations/RC-Store-Backup/format").status_code == 400
    assert fake.calls == []


@pytest.mark.parametrize("kw", [
    {"json": {"command": "calc.exe"}},
    {"data": "x=1"},
    {"query_string": {"args": "-m evil"}},
])
def test_extra_arguments_are_refused(client, fake, kw):
    r = client.post("/api/automations/RC-Store-Backup/install", **kw)
    assert r.status_code == 400
    assert fake.calls == []


def test_get_is_not_a_mutation_route(client, fake):
    assert client.get("/api/automations/RC-Store-Backup/install").status_code == 405
    assert fake.calls == []


def test_cross_site_post_is_refused(client, fake):
    r = client.post("/api/automations/RC-Store-Backup/install",
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and fake.calls == []


def test_unavailable_host_answers_409_for_actions_and_a_state_for_the_list(client):
    A.set_scheduler(A.FakeScheduler(available=False))
    try:
        assert client.post("/api/automations/RC-Store-Backup/install").status_code == 409
        d = client.get("/api/automations").get_json()
        assert d["available"] is False and d["rows"][0]["state"] == "unavailable"
    finally:
        A.set_scheduler(None)


def test_settings_collect_carries_static_rows_and_never_calls_the_scheduler(rawstore_tmp_db, fake):
    from RevenueChaser.module_settings import inventory
    payload = inventory.collect()
    assert [r["name"] for r in payload["automations"]] == [e.name for e in A.REGISTRY]
    assert "state" not in payload["automations"][0]
    assert fake.calls == []


def test_nothing_mutates_on_import_get_or_a_settings_refresh(rawstore_tmp_db, client, fake):
    import importlib

    importlib.reload(A)          # import time: nothing may install anything
    A.set_scheduler(fake)
    assert client.get("/api/automations").status_code == 200
    reads = len(fake.calls)
    r = client.get("/view/module_settings?refresh=1")
    assert r.status_code == 200 and "RC-Store-Backup" in r.get_data(as_text=True)
    assert len(fake.calls) == reads, "a Settings Refresh must not reach the scheduler"
    assert fake.mutations == []


def test_post_without_the_panel_header_is_refused(fake):
    """N3: another localhost origin can send a simple POST, but not a custom header
    without a CORS preflight the dashboard never answers."""
    raw = dashboard.app.test_client()
    r = raw.post("/api/automations/RC-Store-Backup/install")
    assert r.status_code == 403 and fake.calls == []
    r = raw.post("/api/automations/RC-Store-Backup/install", headers={"X-RC-Panel": "nope"})
    assert r.status_code == 403 and fake.calls == []


def test_a_file_upload_is_an_argument_too(client, fake):
    import io
    r = client.post("/api/automations/RC-Store-Backup/install",
                    data={"f": (io.BytesIO(b""), "x.txt")},
                    content_type="multipart/form-data")
    assert r.status_code == 400 and fake.calls == []


def test_a_declared_body_length_is_refused(client, fake):
    r = client.post("/api/automations/RC-Store-Backup/install",
                    environ_overrides={"CONTENT_LENGTH": "5"})
    assert r.status_code == 400 and fake.calls == []


def test_one_failing_row_still_answers_200(client):
    class Broken(A.FakeScheduler):
        def status(self, name):
            raise A.AutomationError("unreadable scheduler answer")

    A.set_scheduler(Broken())
    try:
        r = client.get("/api/automations")
        assert r.status_code == 200
        assert {x["state"] for x in r.get_json()["rows"]} == {"error"}
    finally:
        A.set_scheduler(None)
