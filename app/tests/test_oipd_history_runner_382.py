"""#382 OIPD writer: the dashboard Fetch records its chain and fits in result history. Offline, temp stores.

Fetch only (a Refresh never records); the header carries the job's id; a history failure is a job note and never
blocks the Fetch; the history is recorded after the board file and before a "nothing fitted" ``JobMessage``. Since G9
the chain is the options store's (``module_oipd/tests/options_store.py``) and the Fetch calls no provider.
"""
from __future__ import annotations

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_oipd import history as oipd_history
from RevenueChaser.module_oipd.tests.test_engine import fixed_today, store_chain  # noqa: F401

BOARD = "module_oipd"


@pytest.fixture
def fetch(rawstore_tmp_db, fixed_today, monkeypatch):  # noqa: F811
    ledger.take_notes()
    monkeypatch.setattr(runners, "run_id", lambda: "job-o")
    store_chain()
    return rawstore_tmp_db


def _headers():
    return rawstore.query("SELECT * FROM result_runs WHERE board = ?", [BOARD], db="history")


def test_a_fetch_records_its_fits_under_the_job_id(fetch):
    runners.run_oipd(None, "TST")
    (head,) = _headers()
    assert head["run_id"] == "job-o"
    fits = rawstore.query("SELECT run_id, ticker FROM oipd_fit_history", db="history")
    assert len(fits) == 4 and {f["run_id"] for f in fits} == {"job-o"}
    assert rawstore.query("SELECT count(*) AS n FROM oipd_chain_history", db="history")[0]["n"] > 0


def test_the_history_is_recorded_after_the_board_file(fetch, monkeypatch):
    order = []
    real = runners._render_oipd
    monkeypatch.setattr(runners, "_render_oipd", lambda p: (order.append("board"), real(p))[1])
    monkeypatch.setattr(oipd_history, "record", lambda *a, **k: order.append("history"))
    runners.run_oipd(None, "TST")
    assert order == ["board", "history"]


def test_the_runner_records_no_provider_health_since_it_calls_no_provider(fetch, monkeypatch):
    seen = {}
    monkeypatch.setattr(oipd_history, "record", lambda payload, capture, **k: seen.update(capture=capture, **k))
    runners.run_oipd(None, "TST")
    assert seen["uw_health"] is None and seen["capture"]["status"] == "ok" and seen["run_id"] == "job-o"


def test_a_fetch_that_fitted_nothing_records_before_its_job_message(fetch):
    with pytest.raises(JobMessage, match="not on the option watch list"):
        runners.run_oipd(None, "APPL")
    (head,) = _headers()
    assert head["quality"] == "partial"


def test_a_history_failure_is_a_job_note_and_the_fetch_still_returns_the_board(fetch, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("history file locked")

    monkeypatch.setattr(oipd_history, "record", boom)
    assert runners.run_oipd(None, "TST") == runners.OIPD_LATEST
    notes = ledger.take_notes()
    assert any("OIPD result history was not recorded" in n and "history file locked" in n for n in notes)


def test_a_refresh_never_records(fetch, monkeypatch):
    runners.run_oipd(None, "TST")
    calls = []
    monkeypatch.setattr(oipd_history, "record", lambda *a, **k: calls.append(k))
    ledger.take_notes()
    runners.run_oipd_cached(ticker="TST")
    runners.run_oipd(None, "TST", no_fetch=True)
    assert calls == [] and len(_headers()) == 1
    assert ledger.take_notes() == []             # nor tries to: no "result history was not recorded" note (R8)
