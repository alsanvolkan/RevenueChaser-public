"""RC_WRDS_UNATTENDED=1 lets the hourly builder run the WRDS datasets on their cadence (avo, 1 Nov 2026: homesrv's
``life`` connects with no Duo push), and the worker's login can never outwait ``PGCONNECT_TIMEOUT``."""
from __future__ import annotations

import pytest

from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import DATASETS, base, due
from RevenueChaser.rcdata.build.spine import wrds

WRDS = sorted(n for n in DATASETS if n.startswith("wrds/"))


def test_the_wrds_datasets_are_attended_by_default(monkeypatch):
    monkeypatch.delenv(base.WRDS_UNATTENDED_ENV, raising=False)
    assert len(WRDS) >= 5 and all(base.attended(DATASETS[n]) for n in WRDS)
    assert set(WRDS) <= base.attended_names()


def test_unattended_switch_hands_them_to_the_hourly_builder(monkeypatch):
    monkeypatch.setenv(base.WRDS_UNATTENDED_ENV, "1")
    assert not any(base.attended(DATASETS[n]) for n in WRDS)
    assert not set(WRDS) & base.attended_names()
    assert all(due.is_due(DATASETS[n])[1] != due.ATTENDED_WHY for n in WRDS)


@pytest.mark.parametrize("value", ["", "0", "yes"])
def test_only_exactly_one_switches_it(monkeypatch, value):
    monkeypatch.setenv(base.WRDS_UNATTENDED_ENV, value)
    assert all(base.attended(DATASETS[n]) for n in WRDS)


def test_worker_login_has_a_connect_timeout(monkeypatch):
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    assert wrds.worker_env()["PGCONNECT_TIMEOUT"] == str(wrds.CONNECT_TIMEOUT_S) == "120"
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "30")
    assert wrds.worker_env()["PGCONNECT_TIMEOUT"] == "30"


@pytest.mark.parametrize("name", WRDS)
def test_a_failed_run_waits_for_a_person_instead_of_retrying_hourly(monkeypatch, name):
    # Review must-fix 1: after one success, a failed WRDS run must not be due again every hour (one login each).
    monkeypatch.setenv(base.WRDS_UNATTENDED_ENV, "1")
    ops.record_check(name, "changed", source_as_of="2026-10-01", checked_on="2026-10-01")
    ops.record_check(name, "failed", note="WRDS login failed")
    is_due, why = due.is_due(DATASETS[name])
    assert is_due is False and why == due.WRDS_FAILED_WHY
    ops.record_check(name, "changed", source_as_of="2026-10-01", checked_on="2026-10-01")
    assert due.is_due(DATASETS[name])[1] != due.WRDS_FAILED_WHY       # a person's successful run clears it


def test_an_empty_inherited_timeout_still_gets_the_default(monkeypatch):
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "")
    assert wrds.worker_env()["PGCONNECT_TIMEOUT"] == "120"


def test_attended_command_says_wrds_runs_unattended(monkeypatch, capsys):
    from RevenueChaser.rcdata.build import __main__ as cli
    monkeypatch.setenv(base.WRDS_UNATTENDED_ENV, "1")
    assert cli.main(["attended", "--dataset", WRDS[0]]) == 0
    assert base.WRDS_UNATTENDED_ENV in capsys.readouterr().err
