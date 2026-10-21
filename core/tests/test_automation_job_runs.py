"""A scheduled run leaves a ``job_runs`` row and stamps its writes with that row's id (#129 A1).

``run_task`` runs a registry command as a subprocess, so the dashboard's job registry (``app/jobs.py``) never sees
it. ``run_task`` therefore upserts the run's own row (``trigger = 'scheduled'``, ``registry_name``) when the command
starts and when it ends, and hands the id to the child as ``RC_RUN_ID`` (``RC_RUN_SOURCE`` = the registry name).
In the child, ``ledger.new_run_id`` and the store's change capture use that id, so the rows the job writes join to
the row. Only an entry that writes tables records a row: the backup leaves the store as it copied it.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from RevenueChaser.core import automations as A
from RevenueChaser.core import ledger, rawstore

AMS = ZoneInfo("Europe/Amsterdam")


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setattr(A.config, "OUTPUT_DIR", tmp_path)
    monkeypatch.delenv(rawstore.RUN_ID_ENV, raising=False)
    monkeypatch.delenv(rawstore.RUN_SOURCE_ENV, raising=False)
    A.set_scheduler(None)
    yield
    A.set_scheduler(None)


_SHOW_ENV = "import os; print('env', os.environ.get('RC_RUN_ID'), os.environ.get('RC_RUN_SOURCE'))"


def _entry(name="RC-Writer", code=_SHOW_ENV, tables=("equity_universe",), **kw):
    return A.AutomationEntry(name=name, label="w", args=("-c", code), time="23:00", description="d",
                             tables_written=tables, **kw)


def _only(monkeypatch, *entries):
    monkeypatch.setattr(A, "_BY_NAME", {e.name: e for e in entries})


def _rows():
    with rawstore.connect() as conn:
        cur = conn.execute("SELECT run_id, job_id, module, lane, trigger, source, registry_name, status, error, "
                           "message, queued_at, started_at, finished_at, params, pid FROM job_runs")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def test_a_scheduled_run_leaves_one_job_runs_row(monkeypatch, rawstore_tmp_db):
    e = _entry()
    _only(monkeypatch, e)
    assert A.run_task(e.name) == 0
    [row] = _rows()
    assert re.fullmatch(r"[0-9a-f]{12}", row["run_id"]) and row["job_id"] == row["run_id"]
    assert (row["trigger"], row["registry_name"], row["module"], row["source"]) == (
        "scheduled", e.name, e.name, e.name)
    assert (row["status"], row["error"], row["message"]) == ("done", None, "exit 0")
    assert row["queued_at"] <= row["started_at"] <= row["finished_at"]
    assert json.loads(row["params"]) == {"args": ["-c", _SHOW_ENV]}


def test_the_child_gets_the_rows_id_and_source(monkeypatch, rawstore_tmp_db):
    e = _entry()
    _only(monkeypatch, e)
    A.run_task(e.name)
    [row] = _rows()
    assert f"env {row['run_id']} {e.name}" in e.log_path.read_text(encoding="utf-8")


def test_a_failed_command_ends_the_row_as_error(monkeypatch, rawstore_tmp_db):
    e = _entry(code="import sys; sys.exit(3)")
    _only(monkeypatch, e)
    assert A.run_task(e.name) == 3
    [row] = _rows()
    assert (row["status"], row["error"]) == ("error", "exit 3")


def test_the_row_is_running_while_the_command_runs(monkeypatch, rawstore_tmp_db):
    seen = []

    class Done:
        returncode = 0

    def fake_run(*a, **k):
        seen.append(_rows())
        return Done()

    e = _entry()
    _only(monkeypatch, e)
    monkeypatch.setattr(A.subprocess, "run", fake_run)
    A.run_task(e.name)
    [[during]] = seen
    assert during["status"] == "running" and during["finished_at"] is None
    assert _rows()[0]["status"] == "done"


def test_a_blocked_step_records_why_and_runs_nothing(monkeypatch, rawstore_tmp_db):
    pred = A.AutomationEntry(name="RC-Pred", label="p", args=("-c", "pass"), time="22:35", description="d")
    step = _entry(name="RC-Step", after="RC-Pred", chain_wait_minutes=30)
    _only(monkeypatch, pred, step)
    pred.log_path.parent.mkdir(parents=True, exist_ok=True)
    pred.log_path.write_text("=== 2026-10-09T20:35:00+00:00 start RC-Pred\n"
                             "=== 2026-10-09T20:40:00+00:00 exit 1\n", encoding="utf-8")
    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k: pytest.fail("a blocked step ran its command"))
    now = datetime(2026, 10, 9, 23, 0, tzinfo=AMS)
    assert A.run_task(step.name, clock=lambda: now, sleep=lambda s: None, tz=AMS) == A.CHAIN_BLOCKED
    [row] = _rows()
    assert row["status"] == "error" and row["started_at"] is None
    assert row["error"].startswith("chain blocked: RC-Pred exit 1")


def test_an_entry_that_writes_no_table_records_no_row(monkeypatch, rawstore_tmp_db):
    e = _entry(name="RC-Copy", tables=())
    _only(monkeypatch, e)
    assert A.run_task(e.name) == 0
    assert _rows() == []
    assert "env None None" in e.log_path.read_text(encoding="utf-8")


def test_the_shipped_backup_records_no_row():
    assert A.entry("RC-Store-Backup").tables_written == ()


def test_a_store_that_cannot_be_written_never_changes_the_outcome(monkeypatch, rawstore_tmp_db):
    def boom(*a, **k):
        raise rawstore.RawStoreError("store locked")

    e = _entry(code="import sys; sys.exit(0)")
    _only(monkeypatch, e)
    monkeypatch.setattr(rawstore, "write_rows", boom)
    assert A.run_task(e.name) == 0
    text = e.log_path.read_text(encoding="utf-8")
    assert "job_runs: not recorded (store locked)" in text
    assert re.search(r"=== \S+ exit 0\s*$", text)       # the footer stays the last line: the chain reads it


# --- the child's side ------------------------------------------------------------------------------------------

def test_outside_a_scheduled_run_ids_stay_fresh():
    assert rawstore.task_run() is None
    assert ledger.new_run_id() != ledger.new_run_id()
    assert rawstore._change_writer()[1] is None


def test_inside_a_scheduled_run_every_writer_uses_the_tasks_id(monkeypatch):
    monkeypatch.setenv(rawstore.RUN_ID_ENV, "abc123def456")
    monkeypatch.setenv(rawstore.RUN_SOURCE_ENV, "RC-Screeners-Daily")
    assert rawstore.task_run() == ("abc123def456", "RC-Screeners-Daily")
    assert ledger.new_run_id() == "abc123def456"
    assert rawstore._change_writer() == ("abc123def456", "RC-Screeners-Daily")
    assert rawstore.writer_run_id() == "abc123def456"


def test_a_dashboard_job_id_still_wins_over_the_task(monkeypatch):
    monkeypatch.setenv(rawstore.RUN_ID_ENV, "abc123def456")
    monkeypatch.setattr(rawstore, "_CHANGE_WRITER", lambda: ("job000000001", "universe"))
    assert rawstore._change_writer() == ("job000000001", "universe")
