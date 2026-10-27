"""``ops.enqueue_once``: the check and the insert are one ``BEGIN IMMEDIATE``, so a dataset is never queued twice."""
from __future__ import annotations

import threading

from RevenueChaser.rcdata import ops

MS, UW = "market_series", "uw/spot_expiry_strike"


def _queue() -> list[dict]:
    with ops.connect() as con:
        return [dict(r) for r in con.execute("SELECT dataset, status FROM build_requests ORDER BY id")]


def test_enqueue_once_queues_a_dataset_only_while_none_is_queued_or_running():
    rid = ops.enqueue_once(MS, None, by="schedule")
    assert rid is not None
    assert ops.enqueue_once(MS, None, by="schedule") is None           # queued
    assert ops.next_request()["id"] == rid
    assert ops.enqueue_once(MS, None, by="schedule") is None           # running
    assert ops.enqueue_once(UW, None, by="schedule") is not None       # another dataset is its own
    ops.finish_request(rid, "done")
    assert ops.enqueue_once(MS, None, by="schedule") is not None       # finished: due again may queue


def test_enqueue_once_lets_one_of_many_racing_writers_through():
    ops.ensure()
    n = 8
    gate, got = threading.Barrier(n), []

    def one():
        gate.wait()
        got.append(ops.enqueue_once(MS, None, by="schedule"))
    threads = [threading.Thread(target=one) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in got) == 1
    assert len(_queue()) == 1
