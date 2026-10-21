"""#243 (PL4): cooperative cancel and deadline, and the per-ticker pools that honour them.

``core.job_control`` holds the stop token of the job running on this thread. A runner checks it at its phase
boundaries (``checkpoint``) and the Swing/Fundamentals pool (``core.screen_common.run_screen``) checks it after each
ticker, so a cancel or a passed deadline stops submitting tickers and never reaches ``persist``.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from RevenueChaser.core import job_control, screen_common


# ── the token ─────────────────────────────────────────────────────────────────

def test_checkpoint_outside_a_job_is_a_no_op():
    assert job_control.current() is None
    job_control.checkpoint("Swing")          # nothing bound: never raises
    assert job_control.stop_requested() is False


def test_a_cancel_raises_at_the_next_checkpoint_and_names_the_phases():
    token = job_control.StopToken()
    with job_control.bound(token):
        job_control.checkpoint("Asymmetric Growth")
        token.cancel()
        assert job_control.stop_requested() is True
        with pytest.raises(job_control.JobCancelled) as exc:
            job_control.checkpoint("Swing")
    assert "Asymmetric Growth" in exc.value.message and "Swing" in exc.value.message
    assert job_control.current() is None, "bound() restores the previous token"


def test_a_passed_deadline_raises_and_names_the_deadline_and_the_phase():
    token = job_control.StopToken(deadline_s=0.01)
    with job_control.bound(token):
        job_control.checkpoint("Swing")
        time.sleep(0.03)
        with pytest.raises(job_control.JobDeadline) as exc:
            job_control.checkpoint()
    assert "deadline" in exc.value.message and "Swing" in exc.value.message


def test_no_deadline_never_expires():
    token = job_control.StopToken()
    with job_control.bound(token):
        job_control.checkpoint("x")
    assert token.expired() is False


def test_a_stop_is_not_an_exception_so_a_broad_except_cannot_swallow_it():
    token = job_control.StopToken()
    token.cancel()
    caught = []
    with job_control.bound(token):
        with pytest.raises(job_control.JobStopped):
            try:
                job_control.checkpoint()
            except Exception as exc:  # noqa: BLE001 - the pattern a per-ticker guard uses
                caught.append(exc)
    assert caught == []


def test_the_token_is_per_thread():
    token = job_control.StopToken()
    seen = []
    with job_control.bound(token):
        t = threading.Thread(target=lambda: seen.append(job_control.current()))
        t.start()
        t.join()
    assert seen == [None]


# ── the Swing / Fundamentals pool ─────────────────────────────────────────────

N_ROWS = 400


def _screen(compute_row, persisted, built):
    rows = [{"ticker": f"T{i:03d}"} for i in range(N_ROWS)]
    return screen_common.run_screen(
        board="test", refreshed_by="test", cfg=SimpleNamespace(MIN_PRICE=5.0, MIN_DOLLAR_VOLUME=250_000),
        load_universe=lambda **k: (rows, {}),
        prefilter=lambda rs: screen_common.Prefiltered(list(rs)),
        load_spy=lambda: None,
        compute_row=compute_row,
        build_payload=lambda results, **k: built.append(results) or {"candidates": results},
        persist=persisted.append,
    )


@pytest.fixture
def no_universe_lookups(monkeypatch):
    monkeypatch.setattr(screen_common._universe, "excluded_tickers", lambda: set())
    monkeypatch.setattr(screen_common._universe, "spine_gics", lambda: {})


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_a_stop_in_the_pool_stops_submitting_tickers_and_persists_nothing(no_universe_lookups, stop):
    token = job_control.StopToken(deadline_s=3600 if stop == "cancel" else 0.05)
    calls = []
    lock = threading.Lock()

    def compute_row(row, spy_df, no_fetch, spine, drops):
        with lock:
            calls.append(row["ticker"])
            n = len(calls)
        if stop == "cancel" and n == 3:
            token.cancel()
        time.sleep(0.01)
        return {"ticker": row["ticker"]}

    persisted, built = [], []
    expected = job_control.JobCancelled if stop == "cancel" else job_control.JobDeadline
    with job_control.bound(token), pytest.raises(expected):
        _screen(compute_row, persisted, built)
    assert len(calls) < N_ROWS / 2, f"the pool kept scoring after the stop: {len(calls)} of {N_ROWS}"
    assert persisted == [] and built == []


def test_without_a_stop_the_pool_scores_every_ticker(no_universe_lookups):
    calls = []
    persisted, built = [], []
    with job_control.bound(job_control.StopToken()):
        _screen(lambda row, *a: calls.append(1) or {"ticker": row["ticker"]}, persisted, built)
    assert len(calls) == N_ROWS and len(persisted) == 1
