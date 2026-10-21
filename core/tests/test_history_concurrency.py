"""Overlapping ``core/history.record_run`` calls (O1, #346): two Fetches recording at once.

The dashboard runs jobs on two lanes, each with its own worker thread (``app/jobs.py``), so two board runs can call
``record_run`` at the same moment in one process. Another process can hold ``history.duckdb``: a RevenueChaser peer
(a scheduled job, the backup) for one operation, or a DuckDB CLI for as long as it stays open. The behaviour pinned
here:

- In one process, a session keeps the rows of ONE run, and every run's header lands.
- Across processes, a run waits for a peer that holds the file and then lands whole. A run that cannot get the file
  raises and leaves no header and no rows; its runner turns the error into a job note (``app/runners.py``).

Temp stores only.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import pytest

from RevenueChaser.core import history, rawstore

# 2026-10-06 is a Tuesday: 22:00 and 23:00 UTC are 18:00 and 19:00 ET, both after the close (one complete session).
COMPLETE_1 = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()
COMPLETE_2 = datetime(2026, 10, 6, 23, 0, tzinfo=timezone.utc).timestamp()

SESSION_BOARD, SESSION_TABLE = "module_money_flow", "money_flow_history"
RUN_BOARD, RUN_TABLE = "module_earnings_radar", "earnings_cards_history"

# How long a run held inside its transaction waits for the other run to reach the same point. Two unguarded runs
# meet in milliseconds; a guarded second run is still outside its transaction, so the first goes on after this.
OVERLAP_WAIT = 2.0


def _flow(*ranks):
    return [{"side": "in", "rank": r, "etf": f"E{r}", "name": "n", "flow_score": float(r)} for r in ranks]


def _run(run_id, run_at, rows, board=SESSION_BOARD, table=SESSION_TABLE):
    return history.record_run(board, run_id=run_id, run_at=run_at, model_version="m1", rows={table: rows})


def _headers(board=SESSION_BOARD):
    return rawstore.query("SELECT run_id, session_snapshot, quality_note FROM result_runs WHERE board = ? "
                          "ORDER BY run_id", [board], db="history")


def _rows(table, run_id):
    return rawstore.query(f"SELECT * FROM {table} WHERE run_id = ?", [run_id], db="history")


def _together(*calls):
    """Run each call on its own thread and wait for all; returns (results, errors) keyed by position."""
    results, errors = {}, {}

    def go(i, call):
        try:
            results[i] = call()
        except Exception as exc:                          # noqa: BLE001 - the test asserts on it
            errors[i] = exc

    threads = [threading.Thread(target=go, args=(i, c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads), "a record_run call never returned"
    return results, errors


@pytest.fixture
def overlap(rawstore_tmp_db, monkeypatch):
    """Hold each run inside its history transaction, after its checks and before its header insert, until the other
    run gets there too (or ``OVERLAP_WAIT`` passes): the widest overlap two lanes can produce."""
    rawstore.ensure_history()
    barrier = threading.Barrier(2, timeout=OVERLAP_WAIT)
    real = rawstore._Tx.write_rows

    def held(self, name, rows):
        if name == "result_runs":
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return real(self, name, rows)

    monkeypatch.setattr(rawstore._Tx, "write_rows", held)
    return barrier


def test_two_overlapping_runs_of_one_session_keep_one_snapshot(overlap):
    _, errors = _together(lambda: _run("a", COMPLETE_1, _flow(1, 2)),
                          lambda: _run("b", COMPLETE_2, _flow(1, 3, 4)))
    assert errors == {}
    heads = _headers()
    assert [h["run_id"] for h in heads] == ["a", "b"]                         # both headers land
    snaps = [h["run_id"] for h in heads if h["session_snapshot"]]
    assert len(snaps) == 1                                                    # one run per session
    other = ({"a", "b"} - set(snaps)).pop()
    note = next(h["quality_note"] for h in heads if h["run_id"] == other)
    assert f"already recorded by run {snaps[0]}" in note
    assert _rows(SESSION_TABLE, other) == []
    view = rawstore.query("SELECT DISTINCT run_id FROM money_flow_by_session", db="history")
    assert [r["run_id"] for r in view] == snaps                               # never a mix of two runs


def test_two_overlapping_runs_of_a_run_board_both_keep_their_rows(overlap):
    card = {"ticker": "AAA", "direction_score": 1.0}
    _, errors = _together(lambda: _run("a", COMPLETE_1, [card], board=RUN_BOARD, table=RUN_TABLE),
                          lambda: _run("b", COMPLETE_2, [card], board=RUN_BOARD, table=RUN_TABLE))
    assert errors == {}
    assert [h["run_id"] for h in _headers(RUN_BOARD)] == ["a", "b"]
    assert len(_rows(RUN_TABLE, "a")) == 1 and len(_rows(RUN_TABLE, "b")) == 1


def test_the_same_run_recorded_twice_at_once_is_written_once(overlap):
    results, errors = _together(lambda: _run("a", COMPLETE_1, _flow(1, 2)),
                                lambda: _run("a", COMPLETE_1, _flow(1, 2)))
    assert errors == {}
    assert sorted(results.values(), key=len) == [{}, {SESSION_TABLE: 2}]     # the second call writes nothing
    assert [h["run_id"] for h in _headers()] == ["a"] and len(_rows(SESSION_TABLE, "a")) == 2


def test_two_first_runs_on_a_store_without_a_history_file(rawstore_tmp_db):
    assert not rawstore.history_db_path().exists()
    start = threading.Barrier(2, timeout=10)

    def first(run_id, run_at, ranks):
        start.wait()
        return _run(run_id, run_at, _flow(*ranks))

    _, errors = _together(lambda: first("a", COMPLETE_1, (1, 2)), lambda: first("b", COMPLETE_2, (3,)))
    assert errors == {}
    heads = _headers()
    assert [h["run_id"] for h in heads] == ["a", "b"] and sum(h["session_snapshot"] for h in heads) == 1


# ── another process holds history.duckdb ─────────────────────────────────────
# The holder opens the file read-write (DuckDB's single-writer file lock), says "ready", and keeps it until its stdin
# closes. A peer marks itself the way every RevenueChaser process does (``.rc-proc-<pid>`` beside the store,
# ``rawstore._register_presence``); a foreign holder (a DuckDB CLI, a notebook) does not.
_HOLDER = """
import os, pathlib, sys
import duckdb
path, peer = sys.argv[1], sys.argv[2] == "peer"
conn = duckdb.connect(path)
mark = pathlib.Path(path).parent / f".rc-proc-{os.getpid()}"
if peer:
    mark.write_text(str(os.getpid()))
print("ready", flush=True)
sys.stdin.readline()
conn.close()
if peer:
    mark.unlink()
"""


@pytest.fixture
def holder(rawstore_tmp_db):
    procs = []

    def start(kind):
        p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(rawstore.history_db_path()), kind],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        procs.append(p)
        assert p.stdout.readline().strip() == "ready"
        return p

    rawstore.ensure_history()                         # the file exists before anyone holds it
    yield start
    for p in procs:
        release(p)


def release(p):
    if p.poll() is None:
        p.stdin.close()
        p.wait(20)


def test_a_run_waits_for_a_peer_process_that_holds_the_history_file(holder):
    p = holder("peer")
    results = {}
    t = threading.Thread(target=lambda: results.setdefault("r", _run("a", COMPLETE_1, _flow(1, 2))))
    t.start()
    time.sleep(1.0)                                   # longer than the fail-fast budget of a foreign holder
    assert t.is_alive(), "the run gave up on a peer that only holds the file for one operation"
    release(p)
    t.join(30)
    assert not t.is_alive() and results["r"] == {SESSION_TABLE: 2}
    assert [h["run_id"] for h in _headers()] == ["a"] and len(_rows(SESSION_TABLE, "a")) == 2


def test_a_run_that_cannot_get_the_history_file_raises_and_leaves_nothing(holder):
    p = holder("foreign")
    began = time.monotonic()
    with pytest.raises(rawstore.StoreLockedError) as err:
        _run("a", COMPLETE_1, _flow(1, 2))
    assert time.monotonic() - began < 10              # a foreign holder fails fast, the Fetch is not stalled
    assert str(p.pid) in str(err.value)               # the job note names the holder
    assert isinstance(err.value, rawstore.RawStoreError)
    release(p)
    assert _headers() == [] and _rows(SESSION_TABLE, "a") == []               # no header, no half run
    assert _run("a", COMPLETE_1, _flow(1, 2)) == {SESSION_TABLE: 2}           # nothing left behind blocks a retry
