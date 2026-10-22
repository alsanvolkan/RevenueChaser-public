"""PL6-a (#26): the two nightly CLIs take their own ``job_lock`` names.

``RC-Forecast-Outcomes`` (``scripts/forecast_outcomes.py``) takes ``"forecast_outcomes"`` around the write; a dry run
writes nothing and takes no lock, so it still answers while the task runs. ``scripts/nightly_inputs.py`` takes
``"nightly_inputs_<step>"`` per step, because two different steps write different tables and may run together.

Busy is not an error: both exit ``BUSY_EXIT`` (3) with one line on stderr, the same convention as
``app/universe_cli.UNIVERSE_BUSY_EXIT``.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from RevenueChaser.core import job_lock, nightly_inputs as N, outcomes
from RevenueChaser.core.tests.lock_holders import lock_held
from RevenueChaser.scripts import forecast_outcomes, nightly_inputs as inputs_cli

NIGHT = datetime(2026, 10, 13, 19, 30, tzinfo=ZoneInfo("America/New_York")).timestamp()


def _result() -> dict:
    return {"run_id": "r1", "picks": 3, "rows": 6, "skipped_closed": 0, "written": 6,
            "status": {"open": 6}, "seconds": 0.1}


@pytest.fixture
def outcomes_run(monkeypatch):
    """``outcomes.run`` faked: these tests are about the lock and the exit code, not the computation."""
    seen: list[dict] = []

    def fake_run(*, full=False, write=True, run_id=None, now=None):
        seen.append({"full": full, "write": write})
        return _result()

    monkeypatch.setattr(outcomes, "run", fake_run)
    return seen


# ── forecast outcomes ────────────────────────────────────────────────────────

def test_the_task_exits_with_its_busy_code_while_the_lock_is_held(outcomes_run, capsys):
    started = time.monotonic()
    with lock_held("forecast_outcomes"):
        assert forecast_outcomes.main(["--nightly"]) == forecast_outcomes.BUSY_EXIT == 3
    assert time.monotonic() - started < 10, "a busy run must refuse at once, not wait on the lock"
    err = capsys.readouterr().err
    assert "forecast_outcomes" in err and "nothing was written" in err
    assert f"pid {os.getpid()}" in err
    assert outcomes_run == [], "a busy run computed and wrote anyway"


def test_a_free_lock_runs_and_releases(outcomes_run):
    assert forecast_outcomes.main(["--nightly"]) == 0
    assert outcomes_run == [{"full": False, "write": True}]
    assert "forecast_outcomes" not in dict(job_lock.holders()), "the lock outlived the run"


def test_a_dry_run_takes_no_lock_and_still_answers(outcomes_run):
    with lock_held("forecast_outcomes"):
        assert forecast_outcomes.main(["--nightly", "--dry-run"]) == 0
    assert outcomes_run == [{"full": False, "write": False}]


# ── nightly inputs: one lock per step ───────────────────────────────────────

RAN: list[str] = []


def _step_sweeps():
    def ok(ctx):
        RAN.append("ran")
        return {"status": "ok"}

    return (N.Sweep("A", ("short_interest",), ok),)


@pytest.fixture(autouse=True)
def steps(monkeypatch, tmp_path):
    RAN.clear()
    monkeypatch.setattr(N, "STEPS", {"alpha": f"{__name__}:_step_sweeps"})
    monkeypatch.setattr(N.config, "OUTPUT_DIR", tmp_path)
    # After the US close (2026-10-13 19:30 New York), as the nightly task runs: inside the session a step skips
    # itself and no sweep runs, so the lock tests would pass for the wrong reason.
    monkeypatch.setattr(N.time, "time", lambda: NIGHT)


def test_a_held_step_lock_exits_3_and_runs_nothing(capsys):
    with lock_held("nightly_inputs_alpha"):
        assert inputs_cli.main(["alpha"]) == inputs_cli.BUSY_EXIT == 3
    err = capsys.readouterr().err
    assert "nightly_inputs_alpha" in err and "nothing was written" in err
    assert RAN == [], "a busy step ran its sweeps anyway"


def test_a_free_step_runs_and_releases():
    assert inputs_cli.main(["alpha"]) == 0
    assert RAN == ["ran"]
    assert "nightly_inputs_alpha" not in dict(job_lock.holders()), "the lock outlived the run"


def test_another_steps_lock_does_not_hold_this_step():
    """The steps write different tables: ``nightly_inputs_<other>`` never blocks this one."""
    with lock_held("nightly_inputs_beta"):
        assert inputs_cli.main(["alpha"]) == 0
    assert RAN == ["ran"]
