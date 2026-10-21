"""E2 (#372): the Earnings runner hands the screener outcome to history and the board notice.

A partial or failed UW screener answer must reach both the recorded run (a partial note) and the
board (the amber notice), and a board with only "no screener row" exclusions must say so.
Offline; the store is a temp DuckDB.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.app import runners
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_earnings_radar import history as er_history

BOARD = "module_earnings_radar"


class _UW:
    auth_error = None

    def health(self):
        return {"calls": 1, "failed": 0, "failures": [], "auth_error": None, "breaker_open": False,
                "skipped_calls": 0}

    def screener_batch(self, tickers):
        return {}


def test_no_cards_text_blames_the_missing_screener_row():
    stats = {"total": 3, "passed": 0, "bypassed": 0, "filtered": 3,
             "by_reason": {"no screener row": 3},
             "excluded": [{"ticker": t, "reasons": ["no screener row"]} for t in ("A", "B", "C")]}
    text = runners._no_cards_text(3, stats, "gap.")
    assert "No UW screener row for any of the 3 calendar tickers; nothing was scored." in text
    assert text.endswith("gap.")


def test_no_cards_text_still_blames_the_gates_when_that_is_the_reason():
    stats = {"total": 2, "passed": 0, "bypassed": 0, "filtered": 2, "by_reason": {"OI": 2}, "excluded": []}
    assert "failed the filter gates" in runners._no_cards_text(2, stats, None)


@pytest.fixture
def run(monkeypatch, rawstore_tmp_db):
    ledger.take_notes()
    path = runners.OUTPUT_DIR / "te2" / "earnings_radar_latest.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runners, "EARNINGS_LATEST", path)
    monkeypatch.setattr(runners, "_load_watchlist", lambda: set())
    monkeypatch.setattr(runners, "_freeze_run_id", lambda: "job-e2")
    monkeypatch.setattr("RevenueChaser.module_sector_rotation.engine.rotation_quadrant_map",
                        lambda uw: {"XLK": "LEADING"})
    seen = {}

    def fake_export(cards, events, nrc, filename=None, filter_stats=None, provider_health=None, notice=None, run_at=None):
        seen["notice"] = notice
        runners.EARNINGS_LATEST.write_text("<html>board</html>", encoding="utf-8")

    monkeypatch.setattr("RevenueChaser.module_earnings_radar.report.export_html", fake_export)

    class Pipe:
        screener_result = {"status": "partial", "error": {"failed_chunks": 1, "chunks": 2}}

        def __init__(self, tickers, uw, raw_cal, etf_quadrant_map=None):
            self.tickers = tickers
            self._filter_stats = {"total": len(tickers), "passed": 1, "bypassed": 0, "filtered": 0,
                                  "by_reason": {}, "excluded": []}

        def run(self, use_preselect=True):
            from RevenueChaser.core.models.events import TradeCard
            return [TradeCard(ticker="AAA", earnings_date=date(2026, 10, 8), earnings_time="AMC")]

    monkeypatch.setattr(runners, "EarningsPipeline", Pipe)
    monkeypatch.setattr(runners, "_compute_nrc_results", lambda cards: {})
    events = [{"ticker": "AAA", "earnings_date": date(2026, 10, 8), "earnings_time": "AMC", "source": "UW"}]
    monkeypatch.setattr(runners, "_fetch_calendar_range", lambda uw, s, e: (list(events), {}))
    return seen


def test_the_runner_shows_the_notice_and_records_the_screener_reason(run):
    runners.run_earnings_range(_UW(), date(2026, 10, 8), date(2026, 10, 8))
    expected = er_history.screener_problem({"status": "partial", "error": {"failed_chunks": 1, "chunks": 2}})
    assert run["notice"] == expected and "chunks failed" in run["notice"]
    (head,) = rawstore.query("SELECT * FROM result_runs WHERE board = ?", [BOARD], db="history")
    assert head["quality"] == "partial" and "screener" in head["quality_note"]
