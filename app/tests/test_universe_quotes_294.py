"""The universe job writes UW's quote rows (#294): after the universe pull, before Swing and Fundamentals.

The UW side runs only when the job hands ``run_universe_refresh`` a client (``quotes_uw``). The dashboard job builds
one when a UW key verifies (``run_universe_job``); without one the run goes on and says why in a note. No network.
"""
from __future__ import annotations

import pytest

import RevenueChaser.app.dashboard  # noqa: F401 - registers the jobs
from RevenueChaser.app import jobs, runners
from RevenueChaser.core import ledger, quotes

# dashboard.py's registration, captured at import: some test files clear the registry (test_jobs.py).
_REGISTERED = (jobs._RUNNERS.get("universe"), jobs._REQUIRES_UW.get("universe"))


@pytest.fixture
def steps(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(runners, "run_asymmetric_growth", lambda uw, **kw: order.append("ag"))
    monkeypatch.setattr(runners, "run_swing", lambda uw, **kw: order.append("swing"))
    monkeypatch.setattr(runners, "run_fundamentals", lambda uw, **kw: order.append("fund"))

    def ensure(tickers=None, *, uw, now=None, run_id=None):
        order.append(("quotes", uw, run_id))
        return {"session": "2026-10-08", "skipped": None, "rows_inserted": 3, "uw_calls": 1, "no_cap": 0,
                "not_returned": 0, "errors": []}
    monkeypatch.setattr(quotes, "ensure_quote_snapshot", ensure)
    ledger.take_notes()
    yield order
    ledger.take_notes()


def test_the_uw_quote_rows_come_after_the_pull_and_before_the_other_boards(steps):
    uw = object()
    runners.run_universe_refresh(None, quotes_uw=uw)
    assert steps[0] == "ag" and steps[2:] == ["swing", "fund"]
    assert steps[1][:2] == ("quotes", uw) and steps[1][2]
    assert ledger.take_notes() == ["Quote snapshot 2026-10-08: UW 3 new rows (1 calls, 0 without a cap, "
                                   "0 not returned)."]


def test_without_a_client_there_is_no_uw_step_and_no_note(steps):
    runners.run_universe_refresh(None)
    assert steps == ["ag", "swing", "fund"]
    assert ledger.take_notes() == []


def test_a_failing_uw_step_is_a_note_and_the_boards_still_run(steps, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store locked")
    monkeypatch.setattr(quotes, "ensure_quote_snapshot", boom)
    runners.run_universe_refresh(None, quotes_uw=object())
    assert steps == ["ag", "swing", "fund"]
    assert ledger.take_notes() == ["Quote snapshot (UW) was not written: RuntimeError: store locked"]


class _Client:
    def __init__(self, key="k", ok=True):
        self.key = key
        self.ok = ok
        self.closed = False

    def verify(self):
        return (self.ok, None if self.ok else "UW key was rejected")

    def close(self):
        self.closed = True


@pytest.fixture
def lock(monkeypatch):
    """The universe lock as a recorder: the order of the lock, the client build and the steps."""
    from contextlib import contextmanager

    from RevenueChaser.core import universe_fetch
    events: list = []
    state = {"holder": None}

    @contextmanager
    def fake_lock():
        events.append("lock")
        yield state["holder"]
        events.append("unlock")
    monkeypatch.setattr(universe_fetch, "universe_run_lock", fake_lock)
    monkeypatch.setattr(universe_fetch, "universe_run_holder", lambda: None)
    return events, state


def test_the_job_builds_its_client_under_the_lock_and_closes_it_after_the_step(steps, monkeypatch, lock):
    events, _ = lock
    client = _Client()

    def build():
        events.append("client")
        return client
    monkeypatch.setattr(runners, "UWClient", build)
    runners.run_universe_job(None)
    assert events == ["lock", "client", "unlock"]
    assert steps[1][:2] == ("quotes", client)
    assert client.closed, "the client's HTTP pool is closed after the quote step"


def test_a_busy_universe_run_builds_no_client(monkeypatch, lock):
    _, state = lock
    state["holder"] = 4242
    built = []
    monkeypatch.setattr(runners, "UWClient", lambda: built.append(1) or _Client())
    with pytest.raises(runners.UniverseBusy):
        runners.run_universe_job(None)
    assert built == []


def test_the_client_is_closed_when_the_quote_step_fails(steps, monkeypatch, lock):
    client = _Client()
    monkeypatch.setattr(runners, "UWClient", lambda: client)

    def boom(*a, **k):
        raise RuntimeError("store locked")
    monkeypatch.setattr(quotes, "ensure_quote_snapshot", boom)
    runners.run_universe_job(None)
    assert client.closed
    assert steps == ["ag", "swing", "fund"]


@pytest.mark.parametrize("client,note", [
    (_Client(key=""), "UW quote rows were not written: no UW key."),
    (_Client(ok=False), "UW quote rows were not written: UW key was rejected"),
])
def test_without_a_usable_key_the_job_runs_without_uw_and_says_why(steps, monkeypatch, lock, client, note):
    monkeypatch.setattr(runners, "UWClient", lambda: client)
    runners.run_universe_job(None)
    assert steps == ["ag", "swing", "fund"]
    assert ledger.take_notes() == [note]
    assert client.closed


def test_a_client_the_caller_hands_over_is_not_closed_by_the_run(steps, lock):
    client = _Client()
    runners.run_universe_job(client)
    assert steps[1][:2] == ("quotes", client)
    assert not client.closed, "the job queue closes the client it built"


def test_the_dashboard_registers_the_job_runner():
    assert _REGISTERED[0] is runners.run_universe_job
    assert _REGISTERED[1] is False, "a missing UW key must never stop the universe pull"
