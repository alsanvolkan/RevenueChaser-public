"""#243 (PL4): the Asymmetric Growth pool honours a cancel or a passed deadline.

The hunter scores the universe on a ``ThreadPoolExecutor`` in batches of ``PRELOAD_BATCH``. A stop seen after a
ticker cancels the futures not yet started and raises out of the scan, so nothing is returned for a board write
or a freeze.
"""
from __future__ import annotations

import threading
import time

import pytest

from RevenueChaser.core import job_control
from RevenueChaser.module_asymmetric_growth import engine

N_ROWS = 1200          # three preload batches


@pytest.fixture
def scored(monkeypatch):
    calls: list[str] = []
    lock = threading.Lock()
    rows = [{"ticker": f"T{i:04d}"} for i in range(N_ROWS)]
    monkeypatch.setattr(engine, "fetch_universe_stats", lambda *a, **k: (rows, {}))
    monkeypatch.setattr(engine._spine, "excluded_tickers", lambda: set())
    monkeypatch.setattr(engine._spine, "spine_gics", lambda: {})
    preloads = []
    monkeypatch.setattr(engine, "_preload", lambda tickers, no_fetch, session=None: preloads.append(len(tickers)) or {})
    hook = {"on_call": lambda n: None}

    def score(ticker, meta, no_fetch, spine, preload):
        with lock:
            calls.append(ticker)
            n = len(calls)
        hook["on_call"](n)
        time.sleep(0.002)
        return None

    monkeypatch.setattr(engine, "enrich_and_score", score)
    return calls, preloads, hook


def test_a_cancel_stops_the_pool_and_the_scan_returns_nothing(scored):
    calls, preloads, hook = scored
    token = job_control.StopToken()
    hook["on_call"] = lambda n: token.cancel() if n == 5 else None
    with job_control.bound(token), pytest.raises(job_control.JobCancelled):
        engine.run_asymmetric_growth_hunter(None)
    assert len(calls) < engine.PRELOAD_BATCH, f"kept scoring after the cancel: {len(calls)}"
    assert preloads == [engine.PRELOAD_BATCH], "no later batch is loaded after the cancel"


def test_a_passed_deadline_stops_the_pool(scored):
    calls, preloads, hook = scored
    token = job_control.StopToken(deadline_s=0.05)
    with job_control.bound(token), pytest.raises(job_control.JobDeadline):
        engine.run_asymmetric_growth_hunter(None)
    assert len(calls) < N_ROWS


def test_without_a_stop_every_ticker_is_scored(scored, monkeypatch):
    calls, preloads, _ = scored
    from RevenueChaser.module_sec_13f import engine as sec_13f
    monkeypatch.setattr(sec_13f, "load_totals", lambda: {})
    monkeypatch.setattr("RevenueChaser.core.index_registry.annotate_cards", lambda cards: None)
    with job_control.bound(job_control.StopToken()):
        engine.run_asymmetric_growth_hunter(None)
    assert len(calls) == N_ROWS and len(preloads) == 3
