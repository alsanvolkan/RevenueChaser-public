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
    with ops.connect() as con:
        con.execute("UPDATE job_runs2 SET pid = ? WHERE run_id = ?", (pid, run_id))
    return rid


def _request(rid: int) -> dict:
    with ops.connect() as con:
        return dict(con.execute("SELECT * FROM build_requests WHERE id = ?", (rid,)).fetchone())


def test_a_running_request_whose_pid_is_gone_is_reclaimed_at_builder_start():
    dead = _dead_pid()
    rid = _taken_and_started("market_series", "dead-run", dead)
    assert cli.main(["run", "--once"]) == 0
    req = _request(rid)
    assert req["status"] == "failed"
    assert req["note"].startswith(f"lost: pid {dead} gone")
    check = ops.latest_check("market_series")
    assert check["result"] == "failed" and check["note"].startswith(f"lost: pid {dead} gone")
    with ops.connect() as con:
        run = con.execute("SELECT status, error FROM job_runs2 WHERE run_id = 'dead-run'").fetchone()
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
