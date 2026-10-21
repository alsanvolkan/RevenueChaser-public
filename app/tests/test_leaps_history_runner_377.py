"""#377 LEAPS writer: the dashboard Fetch records its footprints in result history. Offline, temp stores.

Fetch only (a Refresh, ``run_leaps_cached``, never records); the header carries the job's id; a history failure is a
job note and never blocks the Fetch; the history is recorded after the board file and before the empty-board
``JobMessage``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.module_leaps import history as leaps_history

BOARD = "module_leaps"
COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()   # Tuesday 18:00 ET


class _UW:
    auth_error = None

    def health(self):
        return {"calls": 3, "failed": 0, "auth_error": None, "failures": []}

    def option_trades(self, **kw):
        return []

    def screener_batch(self, tickers):
        return {}

    def oi_per_expiry(self, ticker, date=None):
        return [{"expiry": "2030-01-18", "call_oi": 900, "put_oi": 100}]

    def flow_per_expiry(self, ticker):
        return []

    def iv_rank(self, ticker):
        return []

    def oi_per_expiry_result(self, ticker, date=None):
        return ProviderResult("ok", self.oi_per_expiry(ticker, date))

    def flow_per_expiry_result(self, ticker):
        return ProviderResult("ok", self.flow_per_expiry(ticker))

    def iv_rank_result(self, ticker):
        return ProviderResult("ok", self.iv_rank(ticker))


@pytest.fixture
def fetch(rawstore_tmp_db, monkeypatch):
    ledger.take_notes()
    rawstore.save_watchlist({"AAA"})
    monkeypatch.setattr(runners, "run_id", lambda: "job-l")
    return rawstore_tmp_db


def _headers():
    return rawstore.query("SELECT * FROM result_runs WHERE board = ?", [BOARD], db="history")


def test_a_fetch_records_its_footprints_under_the_job_id(fetch, monkeypatch):
    monkeypatch.setattr(runners, "time", SimpleNamespace(time=lambda: COMPLETE))
    runners.run_leaps_latest(_UW())                    # AAA is Quiet and pinned, so the board lists it (#377 L2)
    (head,) = _headers()
    assert head["run_id"] == "job-l" and head["quality"] == "complete" and head["run_at"] == COMPLETE
    rows = rawstore.query("SELECT run_id, ticker, label FROM leaps_footprint_history", db="history")
    assert rows == [{"run_id": "job-l", "ticker": "AAA", "label": "Quiet"}]


def test_the_history_is_recorded_after_the_board_and_before_the_job_message(fetch, monkeypatch):
    rawstore.save_watchlist(set())                     # no pinned name and no print: the empty board and its message
    order = []
    import RevenueChaser.module_leaps.report as report
    real = report.render_leaps_dashboard
    monkeypatch.setattr(report, "render_leaps_dashboard", lambda *a, **k: (order.append("board"), real(*a, **k))[1])
    monkeypatch.setattr(leaps_history, "record", lambda *a, **k: order.append("history"))
    with pytest.raises(JobMessage):
        runners.run_leaps_latest(_UW())
    assert order == ["board", "history"]


def test_the_runner_hands_every_footprint_and_the_uw_health(fetch, monkeypatch):
    seen = {}
    monkeypatch.setattr(leaps_history, "record", lambda fps, **k: seen.update(fps=fps, **k))
    runners.run_leaps_latest(_UW())
    assert [f["ticker"] for f in seen["fps"]] == ["AAA"] and seen["run_id"] == "job-l"
    assert seen["uw_health"]["calls"] == 3 and seen["shown"] == {"watched": 1, "unwatched": 0}


def test_a_history_failure_is_a_job_note_and_the_fetch_still_ends_as_before(fetch, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("history file locked")

    monkeypatch.setattr(leaps_history, "record", boom)
    rawstore.save_watchlist(set())                     # the empty board: its message still ends the Fetch
    with pytest.raises(JobMessage, match="No far-dated"):
        runners.run_leaps_latest(_UW())
    notes = ledger.take_notes()
    assert any("LEAPS result history was not recorded" in n and "history file locked" in n for n in notes)


def test_a_refresh_never_records(fetch):
    runners.run_leaps_cached()
    assert not rawstore.history_db_path().exists() or _headers() == []
