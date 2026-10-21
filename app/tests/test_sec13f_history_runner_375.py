"""#375 13F writer: the 13F Fetch records the board's rows in result history. Offline, temp stores, fake UW.

Fetch only (a Refresh, the dashboard's self-heal and the store-only rebuild never record); the header carries the
job's id; the history is recorded after the board file; an SEC failure or a refused derive is the run's ``error``;
names not refreshed make it partial; a history failure is a job note and never blocks the Fetch.
"""
from __future__ import annotations

import pytest

from RevenueChaser.app import jobs, runners
from RevenueChaser.app.tests.test_sec13f_job import GOOD, _intraday, fake_uw, store  # noqa: F401
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_sec_13f import engine as sec_13f, history as sec13f_history

BOARD, TABLE = "module_sec_13f", "sec13f_metrics_history"


@pytest.fixture
def fetch(store, fake_uw, monkeypatch):  # noqa: F811
    ledger.take_notes()
    monkeypatch.setattr(runners, "run_id", lambda: "job-13f")
    sec_13f.save_sec13f_meta({"AAPL": GOOD})        # meta complete: the UW name step is skipped
    return store


def _headers():
    return rawstore.query("SELECT * FROM result_runs WHERE board = ? ORDER BY run_at, run_id", [BOARD], db="history")


def _rows(run_id):
    return rawstore.query(f"SELECT * FROM {TABLE} WHERE run_id = ?", [run_id], db="history")


def _none_recorded():
    return not rawstore.history_db_path().exists() or _headers() == []


def test_a_fetch_records_the_board_rows_under_the_job_id(fetch):
    runners.build_sec13f(None)
    (head,) = _headers()
    assert head["run_id"] == "job-13f" and head["error"] is None and head["quality_note"] is None
    (row,) = _rows("job-13f")
    assert row["ticker"] == "AAPL" and row["shares"] == 300.0 and row["name"] == "Apple Inc"
    assert str(row["quarter"]) == "2026-03-31" and "111111111" in row["cusips"]


def test_the_shares_step_runs_before_the_board_file_and_the_history_after_it(fetch, monkeypatch):
    """F2 (#375 13F-B): the board reads the shares outstanding the shares step stores, so the step runs first."""
    order = []
    real = runners.run_sec_13f_latest
    monkeypatch.setattr(runners, "run_sec_13f_latest", lambda *a, **k: (order.append("board"), real(*a, **k))[1])
    monkeypatch.setattr(sec13f_history, "record", lambda *a, **k: order.append("history"))
    monkeypatch.setattr(runners, "_store_13f_shares", lambda uw: order.append("shares"))
    runners.build_sec13f(None)
    assert order == ["shares", "board", "history"]


def test_an_sec_failure_is_the_runs_error_and_the_job_still_fails(fetch, monkeypatch):
    def down(*a, **k):
        raise RuntimeError("SEC index down")

    monkeypatch.setattr(sec_13f, "download_missing_zips", down)
    with pytest.raises(RuntimeError, match="SEC index down"):
        runners.build_sec13f(None)
    (head,) = _headers()
    assert head["error"] == "RuntimeError: SEC index down" and len(_rows("job-13f")) == 1


def test_a_refused_derive_is_the_runs_error(fetch, monkeypatch):
    sec_13f.recompute_totals()

    def refuse(*a, **k):
        raise sec_13f.DeriveRefused("orphan holdings")

    monkeypatch.setattr(sec_13f, "recompute_totals", refuse)
    with pytest.raises(sec_13f.DeriveRefused):
        runners.build_sec13f(None)
    (head,) = _headers()
    assert head["error"] == "DeriveRefused: orphan holdings"


def test_names_not_refreshed_make_the_run_partial_and_unmatched_cusips_a_warning(fetch, fake_uw, monkeypatch):  # noqa: F811
    sec_13f.save_sec13f_meta({"AAPL": {"name": "", "sector": "Unclassified"}})  # a placeholder: the UW step runs
    fake_uw.ok = (False, "UNUSUAL_WHALES_API_KEY is not set")
    monkeypatch.setattr(runners, "_13f_unmatched", lambda: ("7 CUSIPs with no ticker are left out", None))
    with pytest.raises(jobs.JobMessage, match="not refreshed"):
        runners.build_sec13f(None)
    (head,) = _headers()
    assert head["quality"] == "partial" and head["quality_note"].startswith("Names and sectors were not refreshed")
    assert "7 CUSIPs with no ticker are left out" in head["warnings"]
    assert "CUSIPs" not in head["quality_note"]


def test_a_history_failure_is_a_job_note_and_the_fetch_still_returns_the_board(fetch, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("history file locked")

    monkeypatch.setattr(sec13f_history, "record", boom)
    assert runners.build_sec13f(None) == runners.SEC_13F_LATEST
    assert any("13F result history was not recorded" in n and "history file locked" in n
               for n in ledger.take_notes())


def test_a_refresh_and_a_rebuild_never_record(fetch):
    sec_13f.recompute_totals()
    runners.run_sec_13f_latest(None)
    with pytest.raises(jobs.JobMessage, match="Rebuilt 13F totals"):
        runners.run_sec13f_rebuild(None)
    assert _none_recorded()
