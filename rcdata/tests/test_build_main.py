"""The builder CLI's start: orphaned rows of a dead builder are failed (S2), stale build files are swept (S4), and
the run loop outlives a locked ops.sqlite (S2)."""
from __future__ import annotations

import sqlite3

from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build import DATASETS
from RevenueChaser.rcdata.build import __main__ as cli


def _nothing_due() -> None:
    """Pause every dataset, so ``run --once`` queues nothing of its own (``due.py``) and sends no call."""
    for name in DATASETS:
        ops.schedule_pause(name, by="test")


def test_s2_requests_and_runs_left_running_by_a_dead_builder_are_failed_at_start():
    rid = ops.enqueue("market_series", None, by="test")
    assert ops.next_request()["id"] == rid                 # a builder took it ...
    ops.start_run("dead-run", "market_series", "queue")     # ... and died mid-run
    with ops.connect() as con:                              # as another process
        con.execute("UPDATE job_runs2 SET pid = 999999 WHERE run_id = 'dead-run'")
    _nothing_due()
    assert cli.main(["run", "--once"]) == 0
    with ops.connect() as con:
        req = con.execute("SELECT status, note FROM build_requests WHERE id = ?", (rid,)).fetchone()
        run = con.execute("SELECT status, error FROM job_runs2 WHERE run_id = 'dead-run'").fetchone()
    assert req["status"] == "failed" and "builder died" in req["note"]
    assert run["status"] == "failed" and "builder died" in run["error"]


def test_s2_the_run_loop_outlives_a_locked_ops_file(monkeypatch):
    calls = []

    def flaky(dataset=None, **kw):
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
    _nothing_due()
    assert cli.main(["run", "--once"]) == 0
    assert not stale[0].exists() and (d / "market.duckdb").exists()


def test_n2_an_operational_error_inside_a_run_does_not_end_the_loop(monkeypatch):
    ops.enqueue("market_series", None, by="test")
    from RevenueChaser.rcdata.build import base
    monkeypatch.setattr(base, "run_request",
                        lambda req, **kw: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
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
    monkeypatch.setattr(base, "run_request", lambda req, **kw: (seen.append(req), ops.latest_check("x") or {
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


def test_the_cli_queues_the_tiers_and_names_it_was_given(monkeypatch):
    """A fill on the server is run from the CLI (#265): ``--tiers`` and ``--names`` shape the request the same way
    the Fetch route's body does, and a wrong tier is refused before the queue, not inside the run."""
    import pytest

    from RevenueChaser.rcdata.build import base
    seen = []
    monkeypatch.setattr(base, "run_request", lambda req, **kw: (seen.append(req), {
        "dataset": req["dataset"], "result": "changed", "source_as_of": None, "rows": 0, "added": 0,
        "removed": 0, "calls": 0, "note": None})[1])
    assert cli.main(["run", "--once", "--dataset", "alpaca/bars", "--tiers", "a,b", "--names", "ko",
                     "--include", "anchors"]) == 0
    assert seen[0]["params"] == {"tiers": ["A", "B"], "names": ["KO"], "include": ["anchors"]}
    assert cli.main(["backfill", "alpaca/bars", "--from", "2026-10-01", "--tiers", "B"]) == 0
    assert seen[1]["params"] == {"mode": "backfill", "from": "2026-10-01", "tiers": ["B"]}
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--once", "--dataset", "alpaca/bars", "--tiers", "Z"])
    assert e.value.code == 2


def test_tiers_or_names_without_a_dataset_is_an_error(capsys):
    import pytest

    for argv in (["run", "--once", "--tiers", "A"], ["run", "--once", "--names", "KO"],
                 ["run", "--once", "--include", "anchors"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 2


# ── Waiting its turn instead of exit 3, and the attended runner ────────────────────────────────────────────────

def _fake_run(rows: list) -> callable:
    """A ``base.run_request`` that records the request and writes nothing."""
    def run(req, **kw):
        rows.append((req["dataset"], req.get("requested_by"), kw.get("attended_ok", False)))
        return {"dataset": req["dataset"], "result": "ok", "source_as_of": None, "rows": 0, "added": 0,
                "removed": 0, "calls": 0}
    return run


def test_the_hourly_pass_leaves_an_attended_request_in_the_queue(monkeypatch):
    """A Fetch press on a WRDS dataset waits for the person's terminal; the hourly builder must not consume it."""
    ran: list = []
    monkeypatch.setattr(cli.base, "run_request", _fake_run(ran))
    rid = ops.enqueue("wrds/sec_idhist", None, by="settings")
    _nothing_due()
    assert cli.main(["run", "--once"]) == 0
    assert ran == []
    with ops.connect() as con:
        assert con.execute("SELECT status FROM build_requests WHERE id = ?", (rid,)).fetchone()[0] == "queued"


def test_the_attended_runner_drains_the_attended_requests_only(monkeypatch):
    ran: list = []
    monkeypatch.setattr(cli.base, "run_request", _fake_run(ran))
    monkeypatch.setattr(cli, "at_a_terminal", lambda: True)
    plain = ops.enqueue("market_series", None, by="settings")
    ops.enqueue("wrds/sec_idhist", None, by="settings")
    assert cli.main(["attended"]) == 0
    assert ran == [("wrds/sec_idhist", "settings", True)]
    with ops.connect() as con:
        assert con.execute("SELECT status FROM build_requests WHERE id = ?", (plain,)).fetchone()[0] == "queued"


def test_the_attended_runner_says_so_when_nothing_is_queued(monkeypatch, capsys):
    """N2: it must not sit on the writer turn for half an hour and then exit without a word."""
    monkeypatch.setattr(cli, "at_a_terminal", lambda: True)
    monkeypatch.setattr(cli.base, "idle_problem", lambda: (_ for _ in ()).throw(AssertionError("waited")))
    assert cli.main(["attended"]) == 0
    assert "no attended request queued" in capsys.readouterr().err


def test_the_attended_runner_refuses_a_runner_with_nobody_at_it(monkeypatch, capsys):
    """N4: the WRDS login and the Duo push need a person; a mis-wired timer must not reach them."""
    monkeypatch.setattr(cli, "at_a_terminal", lambda: False)
    ops.enqueue("wrds/sec_idhist", None, by="settings")
    assert cli.main(["attended"]) == 3
    assert "terminal" in capsys.readouterr().err
    with ops.connect() as con:
        assert con.execute("SELECT status FROM build_requests").fetchone()[0] == "queued"


def test_a_named_dataset_runs_only_the_cli_s_own_request(monkeypatch):
    """SHOULD-2: `run --dataset` and `backfill` are the person's own run. A page's attended request is not theirs
    to spend the WRDS login and the day cap on, so it stays queued for the attended runner."""
    ran: list = []
    monkeypatch.setattr(cli.base, "run_request", _fake_run(ran))
    theirs = ops.enqueue("wrds/sec_idhist", {"mode": "backfill"}, by="settings")
    assert cli.main(["run", "--once", "--dataset", "wrds/sec_idhist", "--wait", "0"]) == 0
    assert cli.main(["backfill", "wrds/sec_idhist", "--wait", "0"]) == 0
    assert [r[1] for r in ran] == ["cli", "cli"]            # twice its own, never the page's
    with ops.connect() as con:
        assert con.execute("SELECT status FROM build_requests WHERE id = ?", (theirs,)).fetchone()[0] == "queued"


def test_a_named_dataset_that_is_not_attended_still_drains_the_queue(monkeypatch):
    """The rule is for the attended datasets only: elsewhere a CLI run still takes the older request first."""
    ran: list = []
    monkeypatch.setattr(cli.base, "run_request", _fake_run(ran))
    ops.enqueue("market_series", None, by="settings")
    assert cli.main(["run", "--once", "--dataset", "market_series"]) == 0
    assert [r[1] for r in ran] == ["settings"]              # the page's request, then nothing of its own


def test_a_backfill_waits_for_its_turn_and_runs_when_the_store_is_free(monkeypatch):
    ran: list = []
    monkeypatch.setattr(cli.base, "run_request", _fake_run(ran))
    monkeypatch.setattr(cli, "WAIT_POLL_S", 0)
    seen = []

    def busy_twice():
        seen.append(1)
        return "job lock held: relatedness (pid 7)" if len(seen) < 3 else None
    monkeypatch.setattr(cli.base, "idle_problem", busy_twice)
    assert cli.main(["backfill", "market_series", "--from", "2026-10-01"]) == 0
    assert len(seen) == 3 and [r[0] for r in ran] == ["market_series"]


def test_a_wait_that_runs_out_is_still_exit_3(monkeypatch, capsys):
    monkeypatch.setattr(cli, "WAIT_POLL_S", 0)
    monkeypatch.setattr(cli.base, "idle_problem", lambda: "job lock held: relatedness (pid 7)")
    assert cli.main(["backfill", "market_series", "--from", "2026-10-01", "--wait", "0"]) == 3
    assert "relatedness (pid 7)" in capsys.readouterr().err


def test_the_hourly_run_does_not_wait_but_a_person_s_commands_do():
    assert cli.default_wait_s("run", dataset=None) == 0.0
    # N3: `run --dataset X` is a person at a terminal, like backfill; only the hourly pass has a timer behind it.
    assert cli.default_wait_s("run", dataset="market_series") == cli.DEFAULT_WAIT_S
    assert cli.default_wait_s("backfill", dataset=None) == cli.DEFAULT_WAIT_S == 1800.0
    assert cli.default_wait_s("attended", dataset=None) == cli.DEFAULT_WAIT_S


def test_the_waiting_line_is_printed_again_when_the_holder_changes(monkeypatch, capsys):
    """N7: a terminal that waits must not keep showing a holder that has gone."""
    monkeypatch.setattr(cli, "WAIT_POLL_S", 0)
    held = ["job lock held: relatedness (pid 7)", "another builder runs: pid 9", None]
    monkeypatch.setattr(cli.base, "idle_problem", lambda: held.pop(0))
    assert cli._wait_for_turn(60.0) is None
    err = capsys.readouterr().err
    assert "relatedness (pid 7)" in err and "pid 9" in err


def test_a_lock_another_builder_held_is_waited_for_and_then_taken(monkeypatch, capsys):
    """N1: the gate and the lock are one loop, so a lock that frees inside the wait still starts this builder."""
    import contextlib

    ran: list = []
    monkeypatch.setattr(cli.base, "run_request", _fake_run(ran))
    monkeypatch.setattr(cli.base, "idle_problem", lambda: None)
    monkeypatch.setattr(cli, "WAIT_POLL_S", 0)
    tries = []

    @contextlib.contextmanager
    def busy_once(name, wait=0.0, **kw):
        tries.append(wait)
        yield len(tries) > 1
    monkeypatch.setattr(cli.job_lock, "acquire", busy_once)
    ops.enqueue("market_series", None, by="settings")
    assert cli.main(["run", "--once", "--dataset", "market_series"]) == 0
    assert len(tries) == 2 and tries == [0, 0]              # each try is non-blocking; the loop does the waiting
    assert [r[0] for r in ran] == ["market_series"]
    # Delta review nit 2: a lock another builder holds is named while waiting, like a gate holder is.
    assert "waiting for the writer turn: another builder holds the lock" in capsys.readouterr().err


def test_a_lock_that_never_frees_is_exit_3(monkeypatch, capsys):
    import contextlib

    monkeypatch.setattr(cli.base, "idle_problem", lambda: None)
    monkeypatch.setattr(cli, "WAIT_POLL_S", 0)

    @contextlib.contextmanager
    def never(name, wait=0.0, **kw):
        yield False
    monkeypatch.setattr(cli.job_lock, "acquire", never)
    assert cli.main(["run", "--once", "--dataset", "market_series", "--wait", "0"]) == 3
    assert "another builder holds the lock" in capsys.readouterr().err


def test_at_a_terminal_is_false_without_one(monkeypatch):
    """Delta review SHOULD 1: the one safety guard of this PR, read from `sys.stdin` itself rather than through
    the stand-in the other test monkeypatches."""
    import sys as _sys

    class Fake:
        def __init__(self, tty):
            self._t = tty

        def isatty(self):
            return self._t

    monkeypatch.setattr(_sys, "stdin", Fake(False))
    assert cli.at_a_terminal() is False
    monkeypatch.setattr(_sys, "stdin", Fake(True))
    assert cli.at_a_terminal() is True
    monkeypatch.setattr(_sys, "stdin", None)                # a pythonw-style runner has none at all
    assert cli.at_a_terminal() is False


def test_attended_with_a_dataset_says_so_when_only_another_one_is_queued(monkeypatch, capsys):
    """Delta review SHOULD 2: `attended --dataset X` must not take the writer turn for a request of some other
    dataset, wait out the whole budget and exit without a word."""
    monkeypatch.setattr(cli, "at_a_terminal", lambda: True)
    monkeypatch.setattr(cli.base, "idle_problem",
                        lambda: (_ for _ in ()).throw(AssertionError("took the turn")))
    ops.enqueue("wrds/sec_idhist", None, by="settings")
    assert cli.main(["attended", "--dataset", "wrds/prices"]) == 0
    assert "no attended request queued" in capsys.readouterr().err
