"""PL6-a (#26): the two Discord jobs are one at a time — ``job_lock`` name ``"discord"``.

``engine.discover()`` and ``engine.update()`` write ``discord.duckdb`` (and drive the DCE CLI), so a second run while
one holds the lock returns a ``{"busy": True, "holder_pid": …}`` result and starts no subprocess. A busy run is not an
error: the runner turns it into the same ``JobMessage`` a finished run ends with. The DCE CLI is faked throughout —
nothing here starts it.
"""
from __future__ import annotations

import os

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.core import job_lock
from RevenueChaser.core.tests.lock_holders import lock_held
from RevenueChaser.module_discord import engine, exporter


class FakeCli:
    """Stands in for ``exporter.run_cli``; records every call and never starts anything."""

    def __init__(self):
        self.calls: list = []

    def __call__(self, args, env=None, timeout=600):
        self.calls.append(list(args))
        return exporter.CliResult(0, "", "")


@pytest.fixture
def fake_cli(monkeypatch):
    cli = FakeCli()
    monkeypatch.setattr(exporter, "run_cli", cli)
    return cli


# ── a held lock refuses, and starts nothing ──────────────────────────────────

def test_discover_returns_busy_and_starts_no_subprocess(rawstore_tmp_db, fake_cli):
    with lock_held("discord"):
        res = engine.discover()
    assert res["busy"] is True and res["holder_pid"] == os.getpid()
    assert res["channels"] == 0 and res["new"] == 0
    assert fake_cli.calls == [], "a busy discover started the DCE CLI"


def test_update_returns_busy_and_starts_no_subprocess(rawstore_tmp_db, fake_cli):
    with lock_held("discord"):
        res = engine.update(media=True)
    assert res["busy"] is True and res["holder_pid"] == os.getpid()
    assert (res["channels"], res["messages"], res["new"], res["failed"]) == (0, 0, 0, [])
    assert fake_cli.calls == [], "a busy update started the DCE CLI"


def test_the_discover_runner_turns_the_busy_result_into_a_job_message(monkeypatch):
    monkeypatch.setattr(engine, "discover", lambda: {
        "busy": True, "holder_pid": 4242, "guild_id": None, "channels": 0, "new": 0})
    with pytest.raises(JobMessage) as err:
        runners.run_discord_discover()
    assert "another Discord job" in err.value.message and "pid 4242" in err.value.message
    assert "nothing was written" in err.value.message


def test_the_update_runner_turns_the_busy_result_into_a_job_message(monkeypatch):
    monkeypatch.setattr(engine, "update", lambda media=False, channel_ids=None: {
        "busy": True, "holder_pid": 4242, "channels": 0, "messages": 0, "new": 0, "failed": []})
    with pytest.raises(JobMessage) as err:
        runners.run_discord(force=True)
    assert "another Discord job" in err.value.message and "pid 4242" in err.value.message


# ── a free lock runs, and releases ───────────────────────────────────────────

def test_a_free_lock_lets_update_run_and_releases_it(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr(engine, "_require_token", lambda: {})
    monkeypatch.setattr(engine.store, "channels", lambda enabled_only=False: [])
    res = engine.update()
    assert not res.get("busy")
    assert "discord" not in dict(job_lock.holders()), "the lock outlived the run"
