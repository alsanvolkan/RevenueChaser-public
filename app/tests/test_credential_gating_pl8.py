"""PL8 (#26): every credential a module declares is checked when it is submitted, not inside the runner.

A Discord Fetch with no ``DISCORD_TOKEN`` used to be accepted (202), queued on the long lane behind whatever ran
there, and only then fail in ``module_discord/engine.py``. The route tests below are that regression; the rest pin
the mechanism (``register(requires=...)`` / ``jobs.credential_problem``) and its limits: the check is presence
only, it costs nothing, and it never gates an optional key whose absence merely degrades a board.
"""
from __future__ import annotations

import pytest

from RevenueChaser.app import dashboard, jobs
from RevenueChaser.core import credentials
from RevenueChaser.core.tests.panel_client import panel_test_client

# Never a real key: a recognisable string, only ever asserted absent from a message (see core/tests/test_credentials).
DUMMY_TOKEN = "pl8-dummy-discord-token-value"

# The providers core/credentials.py marks optional: a missing key degrades a board, it does not break a job.
OPTIONAL_PROVIDERS = ("fred", "openfigi", "deepseek", "claude_subscription", "grok_subscription")


class _FakeUW:
    def verify(self):
        return True, None

    def close(self):
        return None


@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return panel_test_client(dashboard.app)


@pytest.fixture
def no_credentials(monkeypatch):
    """Unset every credential these tests look at, whatever the developer's .env happens to hold."""
    for name in ("DISCORD_TOKEN", "RC_DCE_CLI", "RC_DISCORD_GUILD_ID", "UNUSUAL_WHALES_API_KEY",
                 "FRED_API_KEY", "OPENFIGI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


# ── the registry: a requirement name is validated, and a module may declare several ──

def test_register_refuses_a_requirement_name_that_does_not_exist(monkeypatch):
    """A typo must fail at import, not at the first click."""
    monkeypatch.setattr(jobs, "_REQUIRES", dict(jobs._REQUIRES))
    with pytest.raises(ValueError, match="nope"):
        jobs.register("pl8_bad", lambda uw, **p: None, [], requires_uw=False, requires=("nope",))
    assert "pl8_bad" not in jobs._RUNNERS, "a refused registration must not half-register the module"
    assert "pl8_bad" not in jobs._REQUIRES


def test_a_module_without_requires_declares_nothing(monkeypatch):
    monkeypatch.setattr(jobs, "_REQUIRES", dict(jobs._REQUIRES))
    jobs.register("pl8_plain", lambda uw, **p: None, [], requires_uw=False)
    assert jobs._REQUIRES["pl8_plain"] == ()
    assert jobs.credential_problem("pl8_plain") is None


def test_credential_problem_reports_the_first_declared_problem(monkeypatch):
    monkeypatch.setattr(jobs, "_REQUIRES", dict(jobs._REQUIRES))
    monkeypatch.setitem(jobs._REQUIRES, "pl8_multi", ("discord_cli", "discord", "discord_guild"))
    for name in ("RC_DCE_CLI", "DISCORD_TOKEN", "RC_DISCORD_GUILD_ID"):
        monkeypatch.delenv(name, raising=False)
    assert jobs.credential_problem("pl8_multi") == credentials.problem("discord_cli")
    assert jobs.credential_problem("pl8_multi").startswith("RC_DCE_CLI")


def test_credential_problem_is_none_when_every_requirement_is_satisfied(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "_REQUIRES", dict(jobs._REQUIRES))
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("DISCORD_TOKEN", DUMMY_TOKEN)
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    monkeypatch.setenv("RC_DISCORD_GUILD_ID", "123456789012345678")
    monkeypatch.setitem(jobs._REQUIRES, "pl8_multi", ("discord", "discord_cli", "discord_guild"))
    assert jobs.credential_problem("pl8_multi") is None


def test_the_discord_modules_declare_exactly_what_they_run():
    assert jobs._REQUIRES["module_discord"] == ("discord", "discord_cli")
    assert jobs._REQUIRES["module_discord_discover"] == ("discord", "discord_cli", "discord_guild")


def test_no_optional_provider_is_declared_as_a_requirement():
    declared = {name for reqs in jobs._REQUIRES.values() for name in reqs}
    assert declared.isdisjoint(OPTIONAL_PROVIDERS)
    assert declared <= credentials.requirement_names()


# ── the routes: a missing credential is refused at the button, with nothing queued ──

def test_a_discord_fetch_without_a_token_is_refused_and_queues_nothing(client, no_credentials):
    before = jobs.list_jobs()
    r = client.post("/run/module_discord")
    assert r.status_code == 400
    body = r.get_json()
    assert body["ok"] is False
    assert body["error"] == credentials.missing_message("discord")
    assert "DISCORD_TOKEN" in body["error"]
    assert jobs.list_jobs() == before, "a refused Fetch must not be queued"


def test_a_discord_fetch_without_the_cli_is_refused_and_queues_nothing(client, no_credentials, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", DUMMY_TOKEN)
    before = jobs.list_jobs()
    r = client.post("/run/module_discord")
    assert r.status_code == 400 and "RC_DCE_CLI" in r.get_json()["error"]
    assert jobs.list_jobs() == before


def test_discover_without_a_guild_id_is_refused_and_queues_nothing(client, no_credentials, monkeypatch, tmp_path):
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("DISCORD_TOKEN", DUMMY_TOKEN)
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    before = jobs.list_jobs()
    r = client.post("/run/module_discord_discover")
    assert r.status_code == 400 and "RC_DISCORD_GUILD_ID" in r.get_json()["error"]
    assert jobs.list_jobs() == before


def test_a_configured_discord_fetch_still_submits(client, no_credentials, monkeypatch, tmp_path):
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("DISCORD_TOKEN", DUMMY_TOKEN)
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    monkeypatch.setenv("RC_DISCORD_GUILD_ID", "123456789012345678")
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda module, params=None: submitted.append(module) or "job1")
    assert client.post("/run/module_discord").status_code == 202
    assert client.post("/run/module_discord_discover").status_code == 202
    assert submitted == ["module_discord", "module_discord_discover"]


def test_a_module_with_no_declared_requirement_still_submits(client, no_credentials, monkeypatch):
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda module, params=None: submitted.append(module) or "job1")
    # store-only, requires_uw=False: no key of any kind is set and nothing refuses it
    assert client.post("/run/module_sec_13f").status_code == 202
    assert submitted == ["module_sec_13f"]
    assert client.post("/run/quick", data={"ticker": "AAPL"}).status_code == 202
    assert submitted[-1] == "quick"


def test_optional_provider_keys_are_not_gated(client, no_credentials, monkeypatch):
    """fred / openfigi missing degrades a board — adding a gate would refuse a job that works today."""
    monkeypatch.setattr(dashboard, "_uw", lambda: _FakeUW())
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda module, params=None: submitted.append(module) or "job1")
    assert client.post("/run/rotation").status_code == 202
    assert client.post("/run/module_sec_13f-map").status_code == 202
    assert submitted[0] == "rotation"
    assert "moneyflow" in submitted          # the Rotation Fetch's follow-up job, unchanged
    assert submitted[-1] == "sec13f_map"


def test_the_gate_runs_before_the_uw_probe(no_credentials, monkeypatch):
    """The presence check is free (it reads env vars); it must not wait on the HTTPS pool the probe builds."""
    monkeypatch.setattr(jobs, "_REQUIRES", dict(jobs._REQUIRES))
    monkeypatch.setitem(jobs._REQUIRES_UW, "pl8_order", True)
    monkeypatch.setitem(jobs._REQUIRES, "pl8_order", ("discord",))
    probed = []

    def _probe():
        probed.append(True)
        return _FakeUW()

    monkeypatch.setattr(dashboard, "_uw", _probe)
    with dashboard.app.test_request_context("/run/pl8_order", method="POST"):
        response, status = dashboard._submit("pl8_order", {})
    assert status == 400 and "DISCORD_TOKEN" in response.get_json()["error"]
    assert probed == [], "no UWClient may be built when a credential requirement is already unmet"
