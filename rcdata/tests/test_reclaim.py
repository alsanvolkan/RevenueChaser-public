"""PR 2 step 1: a request a dead builder left ``running`` is reclaimed at builder start, with the dead pid named and
a ``failed`` check row; a request whose builder is still alive (a live ``.rc-proc-<pid>`` marker of that pid in the
market dir) is left alone."""
from __future__ import annotations

import subprocess
import sys

from RevenueChaser.core import config, procs
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import __main__ as cli


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not procs.pid_alive(p.pid)
    return p.pid


def _taken_and_started(dataset: str, run_id: str, pid: int) -> int:
    rid = ops.enqueue(dataset, None, by="test")
    assert ops.next_request(dataset)["id"] == rid
    ops.start_run(run_id, dataset, "queue")
    [run] = [r for r in ops.rows("job_runs2") if r["run_id"] == run_id]
    ops.put_rows("job_runs2", [{**run, "pid": pid}])
    return rid


def _request(rid: int) -> dict:
    return next(r for r in ops.rows("build_requests") if r["id"] == rid)


def test_a_running_request_whose_pid_is_gone_is_reclaimed_at_builder_start(monkeypatch):
    from RevenueChaser.rcdata.build import due
    monkeypatch.setattr(due, "enqueue_due", lambda datasets=None: [])   # this start is about the reclaim only
    dead = _dead_pid()
    rid = _taken_and_started("market_series", "dead-run", dead)
    assert cli.main(["run", "--once"]) == 0
    req = _request(rid)
    assert req["status"] == "failed"
    assert req["note"].startswith(f"lost: pid {dead} gone")
    check = ops.latest_check("market_series")
    assert check["result"] == "failed" and check["note"].startswith(f"lost: pid {dead} gone")
    [run] = [r for r in ops.rows("job_runs2") if r["run_id"] == "dead-run"]
    assert run["status"] == "failed" and f"lost: pid {dead} gone" in run["error"]


def test_a_running_request_without_a_run_row_is_reclaimed_with_the_pid_unknown():
    rid = ops.enqueue("market_series", None, by="test")
    ops.next_request()                                     # taken, then the builder died before start_run
    assert ops.fail_orphans() == (1, 0)
    req = _request(rid)
    assert req["status"] == "failed" and req["note"].startswith("lost: pid unknown")
    assert ops.latest_check("market_series")["result"] == "failed"


def test_a_running_request_whose_builder_is_alive_is_left_alone():
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        config.MARKET_DIR.mkdir(parents=True, exist_ok=True)
        start = procs.process_start_time(sleeper.pid) or ""
        (config.MARKET_DIR / f".rc-proc-{sleeper.pid}").write_text(f"{sleeper.pid} {start}".strip(),
                                                                  encoding="ascii")
        rid = _taken_and_started("market_series", "live-run", sleeper.pid)
        assert ops.fail_orphans() == (0, 0)
        assert _request(rid)["status"] == "running"
        assert ops.latest_check("market_series") is None
    finally:
        sleeper.kill()
        sleeper.wait()
    # Once that builder is gone, the next start reclaims it.
    assert ops.fail_orphans() == (1, 1)
    assert _request(rid)["note"].startswith(f"lost: pid {sleeper.pid} gone")


# #602: between builder starts (the timer is off), a reader closes what a SIGKILL or an OOM kill left running.

def test_a_request_whose_run_pid_is_gone_is_closed_without_a_builder_start():
    dead = _dead_pid()
    rid = _taken_and_started("market_series", "killed-run", dead)
    assert ops.fail_dead_requests() == (1, 1)
    req = _request(rid)
    assert req["status"] == "failed" and req["note"].startswith(f"lost: pid {dead} gone")
    [run] = [r for r in ops.rows("job_runs2") if r["run_id"] == "killed-run"]
    assert run["status"] == "failed"
    assert ops.fail_dead_requests() == (0, 0)                  # idempotent


def test_a_request_with_no_run_row_yet_is_left_to_the_builder():
    rid = ops.enqueue("market_series", None, by="test")
    ops.next_request()                                     # just taken: its run row may be a moment away
    assert ops.fail_dead_requests() == (0, 0)
    assert _request(rid)["status"] == "running"


def test_a_request_whose_run_pid_is_alive_is_left_alone_even_without_a_builder_marker():
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        rid = _taken_and_started("market_series", "live-cli-run", sleeper.pid)   # a cli run marks no presence
        assert ops.fail_dead_requests() == (0, 0)
        assert _request(rid)["status"] == "running"
    finally:
        sleeper.kill()
        sleeper.wait()


def test_the_data_health_page_read_closes_a_request_whose_run_was_killed():
    """#602: the board's own queue rows (the Stop buttons) come from this read, not from /api/build/queue."""
    from RevenueChaser.module_settings import report
    rid = _taken_and_started("market_series", "killed-page-run", _dead_pid())
    nd = report._collect_newdata()
    assert all(r["id"] != rid for r in (nd.get("queue") or {}).get("active", []))
    assert _request(rid)["status"] == "failed"


def test_a_reader_leaves_a_run_another_host_started_alone():
    """#611: a pid is only meaningful on the host that wrote it, so the queue sweep skips another host's run."""
    rid = _taken_and_started("market_series", "other-host-run", _dead_pid())
    [run] = [r for r in ops.rows("job_runs2") if r["run_id"] == "other-host-run"]
    ops.put_rows("job_runs2", [{**run, "host": "some-other-host"}])
    assert ops.fail_dead_requests() == (0, 0)
    assert _request(rid)["status"] == "running"
