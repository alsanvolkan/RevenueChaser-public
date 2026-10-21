"""#356 (review N5): a universe run is refused while a single-board Fetch runs.

#244 refused a board Fetch (AG, Swing, Fundamentals) while a universe run holds the lock. The reverse was open: a
universe run that started while a board Fetch ran went ahead, and both wrote the same board's caches and froze. A
board Fetch now holds a ``board_fetch_<board>`` marker (``core.universe_fetch.board_fetch_lock``) while it runs, and
the universe run is refused while any marker is held. Each side takes its own lock first and then checks the
other's, so two that start together cannot both go ahead. A Refresh takes no marker and is never refused.
"""
from __future__ import annotations

import os
import threading

import pytest

from RevenueChaser.app import jobs, runners, universe_cli
from RevenueChaser.app.tests.test_runners_freeze_128 import freezers, order  # noqa: F401 - fixtures
from RevenueChaser.app.tests.test_universe_lock_244 import (  # noqa: F401 - fixtures
    _held_by_another_process, _universe_job, dashboard_jobs, other_process_holds_the_lock, sync_jobs, writes)
from RevenueChaser.core import job_lock
from RevenueChaser.core import universe_fetch as uf

_BOARDS = [("swing", "swing", "Swing", "swing.scan"),
           ("module_fundamentals", "fundamentals", "Fundamentals", "fund.scan"),
           ("module_asymmetric_growth", "asymmetric_growth", "Asymmetric Growth", "ag.scan")]


def _universe_released() -> bool:
    held = dict(job_lock.holders())
    return "universe" not in held and "universe_run" not in held


# ── the universe run is refused while a board Fetch runs ──────────────────────

@pytest.mark.parametrize("key, board, label, scan", _BOARDS)
def test_a_universe_run_is_refused_while_a_board_fetch_runs_in_this_process(
        sync_jobs, order, freezers, writes, monkeypatch, key, board, label, scan):
    """The dashboard case: the board Fetch on the short lane, the universe run on the long lane."""
    from RevenueChaser.module_asymmetric_growth import engine as ag_engine
    from RevenueChaser.module_fundamentals import pipeline as fu_pipeline
    from RevenueChaser.module_swing_potential import pipeline as sw_pipeline
    target, attr = {"swing": (sw_pipeline, "run_scan"), "fundamentals": (fu_pipeline, "run_scan"),
                    "asymmetric_growth": (ag_engine, "run_asymmetric_growth_hunter")}[board]
    inside, release = threading.Event(), threading.Event()
    real = getattr(target, attr)

    def slow(*a, **k):
        inside.set()
        assert release.wait(5)
        return real(*a, **k)
    monkeypatch.setattr(target, attr, slow)
    fetch = jobs.submit(key)
    t = threading.Thread(target=jobs.execute, args=(fetch,))
    t.start()
    try:
        assert inside.wait(5)
        jid = _universe_job()
        jobs.execute(jid)
        job = jobs.get(jid)
        assert job["status"] == "done", job
        assert job["message"] == f"a {label} Fetch is in progress, pid {os.getpid()}; this universe run did not start"
    finally:
        release.set()
        t.join(10)
    assert jobs.get(fetch)["status"] == "done"
    assert order.count(scan) == 1, "the universe run scanned nothing"
    assert [s for s in order if s.endswith(".scan")] == [scan]
    assert _universe_released()


@pytest.mark.parametrize("key, board, label, scan", _BOARDS)
def test_a_universe_run_is_refused_while_another_process_runs_a_board_fetch(
        sync_jobs, order, writes, key, board, label, scan):
    gen = _held_by_another_process(f"board_fetch_{board}")
    pid = next(gen)
    try:
        jid = _universe_job()
        jobs.execute(jid)
        assert jobs.get(jid)["message"] == f"a {label} Fetch is in progress, pid {pid}; this universe run did not start"
        assert order == [] and writes == []
        assert _universe_released()
    finally:
        gen.close()


def test_the_cli_exits_busy_while_a_board_fetch_runs(order, writes, capsys):
    gen = _held_by_another_process("board_fetch_swing")
    pid = next(gen)
    try:
        assert universe_cli.main([]) == universe_cli.UNIVERSE_BUSY_EXIT
        assert f"a Swing Fetch is in progress, pid {pid}" in capsys.readouterr().err
        assert order == [] and writes == []
    finally:
        gen.close()


def test_a_universe_run_goes_ahead_once_the_board_fetch_ended(sync_jobs, order, freezers):
    jobs.execute(jobs.submit("swing"))
    jid = _universe_job()
    jobs.execute(jid)
    assert jobs.get(jid)["status"] == "done"
    assert {"ag.scan", "fund.scan"} <= set(order) and order.count("swing.scan") == 2


# ── the board Fetch's marker ──────────────────────────────────────────────────

@pytest.mark.parametrize("key, board, label, scan", _BOARDS)
def test_a_board_fetch_holds_its_marker_only_while_it_runs(sync_jobs, order, freezers, monkeypatch,
                                                         key, board, label, scan):
    from RevenueChaser.module_asymmetric_growth import freeze as ag_freeze
    from RevenueChaser.module_fundamentals import freeze as fu_freeze
    from RevenueChaser.module_swing_potential import freeze as sw_freeze
    target, attr = {"swing": (sw_freeze, "freeze_payload"), "fundamentals": (fu_freeze, "freeze_payload"),
                    "asymmetric_growth": (ag_freeze, "freeze_result")}[board]
    seen = []
    real = getattr(target, attr)
    monkeypatch.setattr(target, attr, lambda p, **kw: (real(p, **kw), seen.append(uf.board_fetch_holders())))
    jid = jobs.submit(key)
    jobs.execute(jid)
    assert jobs.get(jid)["status"] == "done"
    assert seen == [[(board, os.getpid())]]
    assert uf.board_fetch_holders() == []


def test_a_board_fetch_releases_its_marker_after_an_exception(sync_jobs, order, monkeypatch):
    from RevenueChaser.module_swing_potential import pipeline as sw_pipeline
    monkeypatch.setattr(sw_pipeline, "run_scan", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    jid = jobs.submit("swing")
    jobs.execute(jid)
    assert jobs.get(jid)["status"] == "error"
    assert uf.board_fetch_holders() == []


def test_a_board_fetch_refused_for_a_universe_run_releases_its_marker(sync_jobs, order, writes,
                                                                     other_process_holds_the_lock):
    jid = jobs.submit("swing")
    jobs.execute(jid)
    assert jobs.get(jid)["message"].startswith(f"A universe run is in progress, pid {other_process_holds_the_lock}")
    assert order == [] and writes == []
    assert uf.board_fetch_holders() == []


def test_a_board_refresh_takes_no_marker(sync_jobs, order, monkeypatch):
    from RevenueChaser.module_swing_potential import pipeline as sw_pipeline
    seen = []
    real = sw_pipeline.run_scan
    monkeypatch.setattr(sw_pipeline, "run_scan", lambda *a, **k: (seen.append(uf.board_fetch_holders()),
                                                                   real(*a, **k))[1])
    jid = jobs.submit("swing", {"no_fetch": True})
    jobs.execute(jid)
    assert jobs.get(jid)["status"] == "done"
    assert seen == [[]]


def test_the_universe_run_takes_no_board_marker(sync_jobs, order, freezers, monkeypatch):
    """It calls the plain runners, so its own board steps are never refused and never refuse it."""
    from RevenueChaser.module_swing_potential import freeze as sw_freeze
    seen = []
    real = sw_freeze.freeze_payload
    monkeypatch.setattr(sw_freeze, "freeze_payload",
                        lambda p, **kw: (real(p, **kw), seen.append(uf.board_fetch_holders())))
    jid = _universe_job()
    jobs.execute(jid)
    assert jobs.get(jid)["status"] == "done"
    assert seen == [[]]


def test_a_board_fetch_is_refused_while_the_same_board_fetch_runs_elsewhere(sync_jobs, order, writes):
    gen = _held_by_another_process("board_fetch_swing")
    pid = next(gen)
    try:
        jid = jobs.submit("swing")
        jobs.execute(jid)
        job = jobs.get(jid)
        assert job["status"] == "done", job
        assert job["message"] == f"Another Swing Fetch is in progress, pid {pid}. This Fetch did not start."
        assert order == [] and writes == []
    finally:
        gen.close()


def test_a_board_fetch_runs_while_another_board_fetch_runs_elsewhere(sync_jobs, order, freezers):
    gen = _held_by_another_process("board_fetch_fundamentals")
    next(gen)
    try:
        jid = jobs.submit("swing")
        jobs.execute(jid)
        assert jobs.get(jid)["status"] == "done"
        assert "swing.scan" in order
    finally:
        gen.close()


def test_a_universe_run_and_a_board_fetch_starting_together_never_both_go_ahead(rawstore_tmp_db):
    """Each takes its own lock, then checks the other's: whichever checks second sees the first."""
    with uf.board_fetch_lock("swing") as busy:
        assert busy is None
        with uf.universe_run_lock() as holder:
            assert holder is None                     # the run's own locks are free...
            assert uf.board_fetch_holders() == [("swing", os.getpid())]   # ...and it sees the Fetch
    with uf.universe_run_lock() as holder:
        assert holder is None
        t_busy = []

        def board_fetch():
            with uf.board_fetch_lock("swing") as busy:
                t_busy.append(busy)
        t = threading.Thread(target=board_fetch)
        t.start()
        t.join(5)
        assert t_busy == [("universe", os.getpid())]
    assert uf.board_fetch_holders() == []


# ── forced interleavings: each side takes its own lock before it checks the other's (review B1) ─────────────

def test_a_universe_run_that_checks_while_a_board_fetch_is_deciding_never_runs_with_it(rawstore_tmp_db,
                                                                                       monkeypatch):
    """The Fetch reads the ``universe_run`` marker; right after that read, a universe run takes its locks and checks
    the boards, then the Fetch goes on. One of the two must refuse: the Fetch took its marker before it read."""
    real = uf.universe_run_holder
    uni_checked, fetch_decided = threading.Event(), threading.Event()
    out: dict = {}

    def universe_side():
        with uf.universe_run_lock() as holder:
            out["uni_go"] = holder is None and not uf.board_fetch_holders()
            uni_checked.set()
            fetch_decided.wait(5)

    def read_then_let_the_universe_run_check():
        pid = real()
        if threading.current_thread().name == "fetch" and "started" not in out:
            out["started"] = True
            out["u"] = threading.Thread(target=universe_side, name="uni")
            out["u"].start()
            assert uni_checked.wait(5)
        return pid
    monkeypatch.setattr(uf, "universe_run_holder", read_then_let_the_universe_run_check)

    def fetch_side():
        with uf.board_fetch_lock("swing") as busy:
            out["fetch_go"] = busy is None
            fetch_decided.set()
    f = threading.Thread(target=fetch_side, name="fetch")
    f.start()
    f.join(10)
    out["u"].join(10)
    assert out.get("started")
    assert not (out["fetch_go"] and out["uni_go"]), out


def test_a_board_fetch_that_starts_while_the_universe_run_is_deciding_never_runs_with_it(
        rawstore_tmp_db, order, freezers, monkeypatch):
    """The universe run reads the board markers; right after that read, a Swing Fetch starts and scans. One of the
    two must refuse: the universe run took its locks before it read."""
    from RevenueChaser.module_swing_potential import pipeline as sw_pipeline
    real_holders = uf.board_fetch_holders
    real_scan = sw_pipeline.run_scan
    fetch_inside, uni_done = threading.Event(), threading.Event()
    out: dict = {"fetch_scanned": False}

    def scan(*a, **k):
        if threading.current_thread().name == "fetch":
            out["fetch_scanned"] = True
            fetch_inside.set()
            uni_done.wait(5)
        return real_scan(*a, **k)
    monkeypatch.setattr(sw_pipeline, "run_scan", scan)

    def fetch():
        try:
            runners.refused_during_universe(runners.run_swing)(None)
        except runners.JobMessage as exc:
            out["fetch_msg"] = exc.message
        finally:
            fetch_inside.set()

    def read_then_start_a_fetch():
        boards = real_holders()
        if "started" not in out:
            out["started"] = True
            out["t"] = threading.Thread(target=fetch, name="fetch")
            out["t"].start()
            fetch_inside.wait(5)
        return boards
    monkeypatch.setattr(uf, "board_fetch_holders", read_then_start_a_fetch)
    try:
        runners.run_universe_refresh(None)
        out["uni_ran"] = True
    except runners.UniverseBusy as exc:
        out["uni_ran"] = False
        out["uni_msg"] = exc.message
    finally:
        uni_done.set()
        out["t"].join(10)
    assert out.get("started")
    assert not (out["uni_ran"] and out["fetch_scanned"]), out
