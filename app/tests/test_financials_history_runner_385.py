"""#385 Financials writer: the dashboard Fetch records its report in result history. Offline, temp stores.

Fetch only (a Refresh, ``run_quick_cached``, never records); the history header and the vendor rows carry the job's
one id; a history failure is a job note and never blocks the Fetch.
"""
from __future__ import annotations

import json

import pytest

import RevenueChaser.module_financials.run as fm
from RevenueChaser.app import runners
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_financials import history as fin_history

BOARD = "module_financials"
RESULTS = {"ticker": "MSFT", "financials": {"sector": "Technology", "fetched_at_iso": "2026-10-06T21:30:00+00:00"},
           "modules": {"dcf": {"assumptions": {"tax_rate": 0.21}, "valuation": {"ev_gordon": 1.0}}},
           "warnings": [], "quick_remarks": {"ticker": "MSFT", "source": "ticker_scores"}}


@pytest.fixture
def fetch(monkeypatch, rawstore_tmp_db, tmp_path):
    ledger.take_notes()
    seen = {"order": []}

    def fake_run(ticker, modules=None, output="html", use_cache=True, no_fetch=False, assumptions=None, peers=None,
                 report_path=None, run_id=None):
        seen.update(ticker=ticker, run_id=run_id, no_fetch=no_fetch, modules=modules)
        seen["order"].append("run")
        path = tmp_path / f"{ticker}_report.html"
        path.write_text("<h1>report</h1>", encoding="utf-8")
        return {"paths": {"html": str(path)}, "results": json.loads(json.dumps(RESULTS))}

    real_copy = runners._copy_quick_artifact

    def copy(html):
        seen["order"].append("board")
        return real_copy(html)

    monkeypatch.setattr(fm, "run", fake_run)
    monkeypatch.setattr(runners, "QUICK_LATEST", tmp_path / "financials_latest.html")
    monkeypatch.setattr(runners, "QUICK_STATE", tmp_path / "financials" / "financials_latest.json")
    monkeypatch.setattr(runners, "_copy_quick_artifact", copy)
    monkeypatch.setattr(runners, "run_id", lambda: "job-q")
    return seen


def _headers():
    return rawstore.query("SELECT * FROM result_runs WHERE board = ?", [BOARD], db="history")


def test_a_fetch_records_its_report_under_the_job_id(fetch):
    runners.run_quick(None, "MSFT")
    (head,) = _headers()
    assert head["run_id"] == fetch["run_id"] == "job-q" and head["quality"] in ("complete", "intraday")
    (row,) = rawstore.query("SELECT * FROM financials_report_history", db="history")
    assert row["run_id"] == "job-q" and row["ticker"] == "MSFT" and row["sector"] == "Technology"
    assert json.loads(head["params"])["modules"] == fetch["modules"]


def test_the_history_is_recorded_after_the_board_file_is_written(fetch, monkeypatch):
    monkeypatch.setattr(fin_history, "record", lambda *a, **k: fetch["order"].append("history"))
    runners.run_quick(None, "MSFT")
    assert fetch["order"] == ["run", "board", "history"]


def test_a_fetch_asks_for_the_job_id_once(fetch, monkeypatch):
    ids = iter(["first", "second"])
    monkeypatch.setattr(runners, "run_id", ids.__next__)
    runners.run_quick(None, "MSFT")
    (head,) = _headers()
    assert fetch["run_id"] == head["run_id"] == "first"


def test_a_report_that_was_not_copied_to_the_board_is_partial(fetch, monkeypatch):
    monkeypatch.setattr(runners, "_copy_quick_artifact", lambda html: False)
    runners.run_quick(None, "MSFT")
    (head,) = _headers()
    assert head["quality"] == "partial" and "report file was not written" in head["quality_note"]


def test_a_history_failure_is_a_job_note_and_the_fetch_still_returns_the_board(fetch, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("history file locked")

    monkeypatch.setattr(fin_history, "record", boom)
    assert runners.run_quick(None, "MSFT") == runners.QUICK_LATEST
    notes = ledger.take_notes()
    assert any("Financials result history was not recorded" in n and "history file locked" in n for n in notes)


def test_a_refresh_never_records(fetch, monkeypatch):
    from RevenueChaser.core.providers import cache as cache_manager
    assert cache_manager.save("MSFT", {"ticker": "MSFT", "annual": [{"fiscal_year": "2025", "revenue": 1.0}]})
    runners.run_quick_cached(ticker="MSFT")
    assert fetch["no_fetch"] is True
    assert not rawstore.history_db_path().exists() or _headers() == []


def test_a_refresh_with_vendor_rows_but_no_raw_cache_still_renders(fetch):
    """Review nit: the Refresh gate is "nothing stored anywhere", not "no ticker_payload_cache row".

    A Financials Fetch stores its statements in ``vendor_financials`` and, for a closed session, its ``.info``
    in ``vendor_valuation`` (FIN-5, #385); ``fm_run(no_fetch=True)`` builds its model inputs from those rows.
    With no raw payload cache row at all, the gate used to answer "nothing stored" and serve a stale board.
    """
    from datetime import datetime, timezone
    from RevenueChaser.core import vendor_data

    vendor_data.record(valuations=[vendor_data.valuation_row_yfinance("MSFT", {"marketCap": 1.0e9})],
                       run_id="job-1", run_at=datetime(2026, 10, 8, 20, 35, tzinfo=timezone.utc).timestamp())
    assert runners.run_quick_cached(ticker="MSFT") == runners.QUICK_LATEST
    assert fetch["no_fetch"] is True and fetch["ticker"] == "MSFT"
