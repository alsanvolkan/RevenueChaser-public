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


def test_s4_build_files_a_dead_builder_left_are_swept_at_start():
    from RevenueChaser.core import config
    d = config.MARKET_DIR
    d.mkdir(parents=True, exist_ok=True)
    keep = [d / "market.duckdb", d / "market.prev.duckdb", d / "ops.sqlite", d / "notes.txt"]
    stale = [d / "market.20261027T101010Z-deadbeef.duckdb", d / "market.20261027T101010Z-deadbeef.duckdb.wal"]
    for p in keep + stale:
        p.write_bytes(b"x")
    assert serving.sweep_builds("market") == sorted(stale)
    assert all(p.exists() for p in keep) and not any(p.exists() for p in stale)
    stale[0].write_bytes(b"x")
    assert cli.main(["run", "--once"]) == 0
    assert not stale[0].exists() and (d / "market.duckdb").exists()


def test_n2_an_operational_error_inside_a_run_does_not_end_the_loop(monkeypatch):
    ops.enqueue("market_series", None, by="test")
    from RevenueChaser.rcdata.build import base
    monkeypatch.setattr(base, "run_request", lambda req: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    monkeypatch.setattr(cli, "POLL_S", 0)
    assert cli._run_queue(None, once=True) == 1
    with ops.connect() as con:
        assert con.execute("SELECT status FROM build_requests").fetchone()[0] == "failed"


def test_n2_a_database_error_at_the_end_of_a_run_is_failed_not_killed(monkeypatch):
    import pytest
    from RevenueChaser.rcdata.build import base
    monkeypatch.setattr(ops, "record_check", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    with pytest.raises(sqlite3.OperationalError):
        base.run_request({"dataset": "nope", "params": {}})
    with ops.connect() as con:
        assert con.execute("SELECT status FROM job_runs2").fetchone()[0] == "failed"


def test_n3_allow_shrink_reaches_the_request(monkeypatch):
    from RevenueChaser.rcdata.build import base
    seen = []
    monkeypatch.setattr(base, "run_request", lambda req: (seen.append(req), ops.latest_check("x") or {
        "dataset": req["dataset"], "result": "changed", "source_as_of": None, "rows": 0, "added": 0,
        "removed": 0, "calls": 0, "note": None})[1])
    assert cli.main(["run", "--once", "--dataset", "market_series", "--allow-shrink"]) == 0
    assert seen[0]["params"] == {"allow_shrink": True}
    assert cli.main(["backfill", "market_series", "--from", "2026-10-01", "--allow-shrink"]) == 0
    assert seen[1]["params"]["allow_shrink"] is True


def test_f5_allow_shrink_or_as_of_without_a_dataset_is_an_error(capsys):
    import pytest
    for argv in (["run", "--once", "--allow-shrink"], ["run", "--once", "--as-of", "2026-10-27"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 2
        assert "--dataset" in capsys.readouterr().err
