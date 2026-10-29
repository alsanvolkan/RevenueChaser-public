"""#375 13F writer: the 13F Fetch records the board's rows in result history. Offline, temp stores.

Fetch only (a Refresh and the dashboard's self-heal never record); the header carries the job's id; the history is
recorded after the board file; a history failure is a job note and never blocks the Fetch. Since G9 the board's rows
come from the ownership store (``module_sec_13f/tests/ownership_store.py``), and the header's ``map`` names that
store and the newest complete quarter's coverage instead of the old CUSIP map.
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.app import runners
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_sec_13f import history as sec13f_history
from RevenueChaser.module_sec_13f.tests.ownership_store import _store, market  # noqa: F401 - the fixture

BOARD, TABLE = "module_sec_13f", "sec13f_metrics_history"


@pytest.fixture
def fetch(market, rawstore_tmp_db, monkeypatch):  # noqa: F811
    ledger.take_notes()
    monkeypatch.setattr(runners, "run_id", lambda: "job-13f")
    _store(market)
    return market


def _headers():
    return rawstore.query("SELECT * FROM result_runs WHERE board = ? ORDER BY run_at, run_id", [BOARD], db="history")


def _rows(run_id):
    return rawstore.query(f"SELECT * FROM {TABLE} WHERE run_id = ?", [run_id], db="history")


def _none_recorded():
    return not rawstore.history_db_path().exists() or _headers() == []


def test_a_fetch_records_the_board_rows_under_the_job_id(fetch):
    runners.build_sec13f(None)
    (head,) = _headers()
    assert head["run_id"] == "job-13f" and head["error"] is None
    context = json.loads(head["context"])
    assert context["map"]["source"] == "rcdata:ownership" and context["map"]["coverage"] == pytest.approx(0.99)
    rows = {r["ticker"]: r for r in _rows("job-13f")}
    assert rows["AAA"]["shares"] == 200.0 and rows["AAA"]["name"] == "Alpha Inc"     # the complete quarter's
    assert str(rows["AAA"]["quarter"]) == "2025-06-30"     # the snapshot quarter: Q3 is still filing


def test_the_board_file_is_written_before_the_history(fetch, monkeypatch):
    order = []
    real = runners.run_sec_13f_latest
    monkeypatch.setattr(runners, "run_sec_13f_latest", lambda *a, **k: (order.append("board"), real(*a, **k))[1])
    monkeypatch.setattr(sec13f_history, "record", lambda *a, **k: order.append("history"))
    runners.build_sec13f(None)
    assert order == ["board", "history"]


def test_a_history_failure_is_a_job_note_and_the_fetch_still_returns_the_board(fetch, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("history file locked")

    monkeypatch.setattr(sec13f_history, "record", boom)
    assert runners.build_sec13f(None) == runners.SEC_13F_LATEST
    assert any("13F result history was not recorded" in n and "history file locked" in n
               for n in ledger.take_notes())


def test_a_refresh_never_records(fetch):
    runners.run_sec_13f_latest(None)
    assert _none_recorded()
