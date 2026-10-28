"""Review should-fix 2: after the deploy, the hourly builder must not start the spine before the attended steps.

Every spine dataset ``starts_by_hand``: it is never due until it has built once (a CLI run or a Settings Fetch, a
``changed`` or ``unchanged`` check). A failed or skipped first attempt does not make it due either, so a source that
fails is not retried every hour before a person has seen it succeed once. After that, the usual rules apply."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import DATASETS, due

SPINE = [n for n, d in DATASETS.items() if d.kind == "spine"]


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc))


def test_every_spine_dataset_starts_by_hand():
    assert len(SPINE) == 7 and all(getattr(DATASETS[n], "starts_by_hand", False) for n in SPINE)
    assert not any(getattr(DATASETS[n], "starts_by_hand", False) for n in ("market_series", "uw/spot_expiry_strike"))


@pytest.mark.parametrize("name", SPINE)
def test_never_built_is_not_due_and_the_hourly_pass_queues_nothing(name):
    due_now, why = due.is_due(DATASETS[name])
    assert not due_now and ("by hand" in why or "attended" in why)


def test_a_failed_first_run_is_still_not_due():
    ops.record_check("funds/index_holdings", "failed", note="every fund file failed")
    assert due.is_due(DATASETS["funds/index_holdings"])[0] is False


def test_once_built_the_usual_rules_apply():
    ops.record_check("funds/index_holdings", "changed", source_as_of="2026-11-18", checked_on="2026-11-18")
    assert due.is_due(DATASETS["funds/index_holdings"])[0] is True          # daily, a session later
    ops.record_check("funds/index_holdings", "failed", note="x")
    assert due.is_due(DATASETS["funds/index_holdings"])[0] is True          # a failure after a success retries


def test_the_spine_queues_nothing_on_a_pass_after_the_deploy():
    queued = {name for name, _, rid, _ in due.enqueue_due() if rid}
    assert not queued & set(SPINE)
