"""E1 (#298, D13): the Earnings Fetch flushes the pipeline's prints capture with the job's id, once, after the run.

A store failure is a job note and the Fetch still finishes with its board. Offline: the pipeline is a stand-in, temp
store."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.app import runners
from RevenueChaser.core import ledger, rawstore, vendor_data
from RevenueChaser.core.tests.test_vendor_data import COMPLETE
from RevenueChaser.module_earnings_radar import earnings_store

PRINT = {"report_date": "2026-07-30", "actual_eps": "1.64", "street_mean_est": "1.60"}


@pytest.fixture
def earnings(monkeypatch, rawstore_tmp_db):
    ledger.take_notes()
    path = runners.OUTPUT_DIR / "t298" / "earnings_radar_latest.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runners, "EARNINGS_LATEST", path)
    monkeypatch.setattr(runners, "_load_watchlist", lambda: set())
    monkeypatch.setattr(runners, "_freeze_run_id", iter(["job-er", "second-id"]).__next__)
    monkeypatch.setattr(runners, "_record_earnings_history", lambda *a, **k: None)
    monkeypatch.setattr("RevenueChaser.module_sector_rotation.engine.rotation_quadrant_map", lambda uw: {})
    monkeypatch.setattr("RevenueChaser.module_earnings_radar.report.export_html",
                        lambda *a, **k: runners.EARNINGS_LATEST.write_text("<html/>", encoding="utf-8"))
    monkeypatch.setattr(earnings_store, "_clock", lambda: COMPLETE)
    seen = {}

    class Pipe:
        def __init__(self, tickers, uw, raw_cal, etf_quadrant_map=None):
            self._filter_stats = {"total": 1, "passed": 1, "bypassed": 0, "filtered": 0, "by_reason": {},
                                  "excluded": []}
            self.vendor = vendor_data.Capture()
            self.earnings_capture = earnings_store.Capture()

        def run(self, use_preselect=True):
            self.earnings_capture.add("AAA", earnings_store.EARNINGS, [PRINT])
            seen["rows_during_run"] = rawstore.query(f"SELECT * FROM {earnings_store.TABLE}")
            return [object()]

    monkeypatch.setattr(runners, "EarningsPipeline", Pipe)
    monkeypatch.setattr(runners, "_compute_nrc_results", lambda cards: {})
    events = [{"ticker": "AAA", "earnings_date": date(2026, 10, 8), "earnings_time": "AMC", "source": "UW"}]
    monkeypatch.setattr(runners, "_fetch_calendar_range", lambda uw, s, e: (list(events), {}))
    return seen


class _UW:
    auth_error = None

    def health(self):
        return {"calls": 1, "failed": 0, "failures": [], "auth_error": None, "breaker_open": False,
                "skipped_calls": 0}

    def screener_batch(self, tickers):
        return {}


def test_an_earnings_fetch_stores_the_prints_after_the_run_with_the_job_id(earnings):
    runners.run_earnings_range(_UW(), date(2026, 10, 8), date(2026, 10, 8))
    assert earnings["rows_during_run"] == []
    rows = rawstore.query(f"SELECT ticker, endpoint, actual_eps, run_id FROM {earnings_store.TABLE}")
    assert rows == [{"ticker": "AAA", "endpoint": "/earnings/{ticker}", "actual_eps": 1.64, "run_id": "job-er"}]


def test_the_prints_and_the_vendor_rows_carry_one_job_id(earnings, monkeypatch):
    seen = {}
    monkeypatch.setattr(vendor_data.Capture, "flush", lambda self, run_id, label="": seen.update(vendor=run_id))
    runners.run_earnings_range(_UW(), date(2026, 10, 8), date(2026, 10, 8))
    (row,) = rawstore.query(f"SELECT run_id FROM {earnings_store.TABLE}")
    assert seen["vendor"] == row["run_id"] == "job-er"


def test_a_store_failure_is_a_job_note_and_the_fetch_finishes(earnings, monkeypatch):
    def boom(fn):
        raise RuntimeError("store locked")

    monkeypatch.setattr(earnings_store.rawstore, "atomic", boom)
    assert runners.run_earnings_range(_UW(), date(2026, 10, 8), date(2026, 10, 8)) == runners.EARNINGS_LATEST
    assert any("Earnings prints were not stored: RuntimeError: store locked" in n for n in ledger.take_notes())
