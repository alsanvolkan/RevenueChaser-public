"""#162: the ``RC-IBKR-Login-Check`` job. No socket is opened — the probe is injected."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from RevenueChaser.core import ibkr_login
from RevenueChaser.scripts import ibkr_login_check

_NOW = datetime(2026, 10, 23, 23, 40, tzinfo=timezone.utc)


def _probe(state: str, detail: str = "d", version: int | None = None):
    def probe(host, port, **kw):
        probe.seen = (host, port)
        return ibkr_login.Reading(state=state, detail=detail, server_version=version)
    return probe


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv("RC_IBKR_LOGIN_CHECK", "1")
    monkeypatch.delenv("RC_IBKR_HOST", raising=False)
    monkeypatch.delenv("RC_IBKR_PORT", raising=False)


def test_a_login_that_is_up_records_it_and_exits_zero(on, tmp_path, capsys):
    path = tmp_path / "state.json"
    code = ibkr_login_check.main([], probe=_probe(ibkr_login.UP, "answered", 178), path=path, now=_NOW)
    assert code == 0
    assert ibkr_login.read(path=path)["state"] == ibkr_login.UP
    assert "logged in" in capsys.readouterr().err.lower()


def test_a_login_that_expired_is_recorded_and_still_exits_zero(on, tmp_path, capsys):
    """A weekly login expiry is not a failed job: a non-zero exit would turn the scheduler row red
    for something nobody broke, and the state is the output, not the exit code."""
    path = tmp_path / "state.json"
    code = ibkr_login_check.main([], probe=_probe(ibkr_login.DOWN, "connection refused"), path=path, now=_NOW)
    assert code == 0
    assert ibkr_login.read(path=path)["state"] == ibkr_login.DOWN
    assert "login needed" in capsys.readouterr().err.lower()


def test_a_silent_port_is_recorded_and_still_exits_zero(on, tmp_path):
    path = tmp_path / "state.json"
    assert ibkr_login_check.main([], probe=_probe(ibkr_login.SILENT), path=path, now=_NOW) == 0
    assert ibkr_login.read(path=path)["state"] == ibkr_login.SILENT


def test_the_job_probes_the_configured_target(monkeypatch, tmp_path):
    monkeypatch.setenv("RC_IBKR_LOGIN_CHECK", "1")
    monkeypatch.setenv("RC_IBKR_HOST", "10.0.0.5")
    monkeypatch.setenv("RC_IBKR_PORT", "4002")
    probe = _probe(ibkr_login.UP)
    ibkr_login_check.main([], probe=probe, path=tmp_path / "state.json", now=_NOW)
    assert probe.seen == ("10.0.0.5", 4002)


def test_the_switch_being_off_probes_nothing_writes_nothing_and_exits_zero(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("RC_IBKR_LOGIN_CHECK", raising=False)
    path = tmp_path / "state.json"

    def never(*a, **kw):
        pytest.fail("the job must not probe while the check is switched off")

    assert ibkr_login_check.main([], probe=never, path=path, now=_NOW) == 0
    assert not path.exists()
    assert "RC_IBKR_LOGIN_CHECK" in capsys.readouterr().err


def test_once_turns_into_exactly_one_reading(on, tmp_path):
    path = tmp_path / "state.json"
    ibkr_login_check.main([], probe=_probe(ibkr_login.UP), path=path, now=_NOW)
    ibkr_login_check.main([], probe=_probe(ibkr_login.DOWN), path=path, now=_NOW + timedelta(minutes=15))
    state = json.loads(path.read_text(encoding="utf-8"))
    assert len(state["changes"]) == 1
    assert state["changes"][0]["at"] == (_NOW + timedelta(minutes=15)).isoformat()


def test_the_summary_says_how_long_the_state_has_held(on, tmp_path, capsys):
    path = tmp_path / "state.json"
    ibkr_login_check.main([], probe=_probe(ibkr_login.DOWN), path=path, now=_NOW)
    capsys.readouterr()
    ibkr_login_check.main([], probe=_probe(ibkr_login.DOWN), path=path, now=_NOW + timedelta(days=2))
    assert "2 days" in capsys.readouterr().err


def test_the_job_writes_no_store_file_and_no_table(on, tmp_path, monkeypatch):
    """Like ``RC-Open-Issues-Check``: this job touches no DuckDB file, so it can never queue behind
    or hold the dashboard's single writer."""
    from RevenueChaser.core import rawstore

    def no_connect(*a, **kw):
        pytest.fail("the login check must not open a store file")

    monkeypatch.setattr(rawstore, "connect", no_connect, raising=False)
    assert ibkr_login_check.main([], probe=_probe(ibkr_login.UP), path=tmp_path / "state.json", now=_NOW) == 0


def test_a_probe_that_explodes_is_reported_and_still_exits_zero(on, tmp_path, capsys):
    """The probe is written not to raise; if it ever does, the job says so rather than crashing a
    scheduler row that will be looked at a week later."""
    def boom(host, port, **kw):
        raise RuntimeError("socket layer exploded")

    assert ibkr_login_check.main([], probe=boom, path=tmp_path / "state.json", now=_NOW) == 0
    assert "exploded" in capsys.readouterr().err
