"""#72 S1: the two runners (no HTML, no dashboard wiring yet; that is S2)."""
from __future__ import annotations

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.module_discord import engine


def test_discover_runner_ends_with_a_job_message(monkeypatch):
    monkeypatch.setattr(engine, "discover", lambda: {"guild_id": "1", "channels": 4, "new": 3})
    with pytest.raises(JobMessage, match=r"4 channels.*3 new"):
        runners.run_discord_discover()


def test_update_runner_reports_counts_and_failures(monkeypatch):
    monkeypatch.setattr(engine, "update", lambda media=False, channel_ids=None: {
        "channels": 2, "messages": 10, "new": 7, "failed": ["9"]})
    with pytest.raises(JobMessage) as ei:
        runners.run_discord()
    msg = ei.value.message
    assert "2 channels" in msg and "7 new" in msg and "9" in msg


def test_update_runner_with_no_enabled_channels_says_so(monkeypatch):
    monkeypatch.setattr(engine, "update", lambda media=False, channel_ids=None: {
        "channels": 0, "messages": 0, "new": 0, "failed": []})
    with pytest.raises(JobMessage, match="no enabled channels"):
        runners.run_discord()


def test_runners_turn_an_engine_error_into_an_actionable_job_error(monkeypatch):
    def boom():
        raise engine.DiscordEngineError("DISCORD_TOKEN is missing")
    monkeypatch.setattr(engine, "discover", boom)
    with pytest.raises(RuntimeError, match="DISCORD_TOKEN is missing"):
        runners.run_discord_discover()


def test_discover_runner_docstring_does_not_claim_store_only():
    assert "Store-only" not in (runners.run_discord_discover.__doc__ or "")
