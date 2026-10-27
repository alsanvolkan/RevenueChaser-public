"""The builder CLI's start: orphaned rows of a dead builder are failed (S2), stale build files are swept (S4), and
the run loop outlives a locked ops.sqlite (S2)."""
from __future__ import annotations

import sqlite3

from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build import __main__ as cli


def test_s2_requests_and_runs_left_running_by_a_dead_builder_are_failed_at_start():
    rid = ops.enqueue("market_series", None, by="test")
    assert ops.next_request()["id"] == rid                 # a builder took it ...
    ops.start_run("dead-run", "market_series", "queue")     # ... and died mid-run
    with ops.connect() as con:                              # as another process
        con.execute("UPDATE job_runs2 SET pid = 999999 WHERE run_id = 'dead-run'")
    assert cli.main(["run", "--once"]) == 0
    with ops.connect() as con:
        req = con.execute("SELECT status, note FROM build_requests WHERE id = ?", (rid,)).fetchone()
        run = con.execute("SELECT status, error FROM job_runs2 WHERE run_id = 'dead-run'").fetchone()
    assert req["status"] == "failed" and "builder died" in req["note"]
    assert run["status"] == "failed" and "builder died" in run["error"]


def test_s2_the_run_loop_outlives_a_locked_ops_file(monkeypatch):
    calls = []

    def flaky(dataset=None):
        calls.append(dataset)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return None
    monkeypatch.setattr(ops, "next_request", flaky)
    monkeypatch.setattr(cli, "POLL_S", 0)
    assert cli._run_queue(None, once=True) == 1
    assert len(calls) == 2
