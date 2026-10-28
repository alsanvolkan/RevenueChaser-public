"""PR 2 step 1 (F8 A): ``jobs.idle()`` and ``GET /api/idle``, the deploy gate's one question.

Not idle while a dashboard job is queued or running, while any ``core/job_lock`` name is held (by another process:
a systemd writer the dashboard's job list never sees), or while a live ``.rc-proc-<pid>`` marker of another process
sits in either store dir (``.store`` beside ``raw.duckdb``, and the market dir); idle otherwise.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from RevenueChaser.app import jobs
from RevenueChaser.core import config, procs, rawstore

REPO = Path(__file__).resolve().parents[2]
_HOLD = ("import sys, time; from pathlib import Path; from RevenueChaser.core import rawstore, job_lock\n"
         "rawstore.DB_PATH = Path(sys.argv[1])\n"
         "with job_lock.acquire(sys.argv[2], wait=0) as got:\n"
         "    print(got, flush=True); time.sleep(60)")


@pytest.fixture(autouse=True)
def _roots(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MARKET_DIR", tmp_path / "market")
    (tmp_path / "market").mkdir()
    with jobs._LOCK:
        saved = dict(jobs._JOBS)
        jobs._JOBS.clear()
    yield
    with jobs._LOCK:
        jobs._JOBS.clear()
        jobs._JOBS.update(saved)


@pytest.fixture
def sleeper():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield p
    p.kill()
    p.wait()


def _marker(directory: Path, pid: int) -> Path:
    f = directory / f".rc-proc-{pid}"
    f.write_text(f"{pid} {procs.process_start_time(pid) or ''}".strip(), encoding="ascii")
    return f


def test_idle_when_nothing_runs():
    assert jobs.idle() == (True, [])


def test_not_idle_while_a_job_lock_is_held_by_another_process():
    p = subprocess.Popen([sys.executable, "-c", _HOLD, str(rawstore.DB_PATH), "daily_bars"], cwd=REPO.parent,
                         stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "True"
        ok, reasons = jobs.idle()
        assert not ok and any("daily_bars" in r and str(p.pid) in r for r in reasons)
    finally:
        p.kill()
        p.wait()
    assert jobs.idle() == (True, [])


def test_not_idle_while_an_in_memory_job_is_queued_or_running():
    with jobs._LOCK:
        jobs._JOBS["j1"] = {"id": "j1", "module": "earnings", "status": "running", "queued_at": "x"}
    ok, reasons = jobs.idle()
    assert not ok and reasons == ["job earnings running (j1)"]
    with jobs._LOCK:
        jobs._JOBS["j1"]["status"] = "done"
    assert jobs.idle() == (True, [])


@pytest.mark.parametrize("which", ["store", "market"])
def test_not_idle_while_a_live_marker_sits_in_either_store_dir(which, sleeper):
    d = Path(rawstore.DB_PATH).parent if which == "store" else Path(config.MARKET_DIR)
    _marker(d, sleeper.pid)
    ok, reasons = jobs.idle()
    assert not ok and any(str(sleeper.pid) in r and str(d) in r for r in reasons)
    sleeper.kill()
    sleeper.wait()
    assert jobs.idle() == (True, [])                    # a dead pid's marker is a leftover, not a writer


def test_api_idle_mirrors_idle_and_needs_no_header(sleeper):
    from RevenueChaser.app.dashboard import app
    client = app.test_client()                         # a plain client: GET needs no panel header
    r = client.get("/api/idle")
    assert r.status_code == 200 and r.get_json() == {"idle": True, "reasons": []}
    _marker(Path(config.MARKET_DIR), sleeper.pid)
    body = client.get("/api/idle").get_json()
    assert body["idle"] is False and len(body["reasons"]) == 1
