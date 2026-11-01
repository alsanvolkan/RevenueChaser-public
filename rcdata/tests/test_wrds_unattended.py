"""RC_WRDS_UNATTENDED=1 lets the hourly builder run the WRDS datasets on their cadence (avo, 1 Nov 2026: homesrv's
``life`` connects with no Duo push), and the worker's login can never outwait ``PGCONNECT_TIMEOUT``."""
from __future__ import annotations

import pytest

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
