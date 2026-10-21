"""#297 (D12): the three jobs that store vendor statements/valuation stamp them with the job's run id.

- ``universe`` / AG Fetch: ``run_asymmetric_growth`` passes the job's id to the scan; a Refresh runs no scan and asks for no id (K4).
- ``quick``: ``run_quick`` passes the job's id to Financials' ``run``.
- ``earnings``: ``run_earnings_range`` flushes the pipeline's capture with the job's id.
Offline: the scans are spies, temp stores."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.app import runners
from RevenueChaser.core import ledger, rawstore, vendor_data
from RevenueChaser.core.tests.test_vendor_data import COMPLETE, _uw_fin

AG_RESULT = {"cards": [], "summary": {}, "asof": "2026-10-08T20:35:00+00:00", "funnel_steps": []}


@pytest.fixture
def ag(monkeypatch, rawstore_tmp_db):
    from RevenueChaser.module_asymmetric_growth import engine, freeze, history, report
    from RevenueChaser.core import ticker_store
    seen = {}
    monkeypatch.setattr(engine, "run_asymmetric_growth_hunter", lambda uw, **k: seen.update(k) or dict(AG_RESULT))
    monkeypatch.setattr(report, "render_asymmetric_growth_hunter_html", lambda r: "<html/>")
    monkeypatch.setattr(ticker_store, "write_from_cards", lambda *a, **k: None)
    monkeypatch.setattr(history, "record", lambda *a, **k: None)
    monkeypatch.setattr(freeze, "freeze_result", lambda *a, **k: None)
    monkeypatch.setattr(runners, "_write_output", lambda *a, **k: None)
    monkeypatch.setattr(runners, "_freeze_run_id", lambda: "job-ag")
    return seen


def test_an_ag_fetch_hands_the_job_id_to_the_scan(ag):
    runners.run_asymmetric_growth(None)
    assert ag["run_id"] == "job-ag" and ag["no_fetch"] is False


def test_an_ag_fetch_asks_for_the_job_id_once(ag, monkeypatch):
    # jobs.py:97: the vendor rows, the history header and the freeze carry one id; a second call could differ.
    from RevenueChaser.module_asymmetric_growth import freeze, history
    ids = {}
    monkeypatch.setattr(runners, "_freeze_run_id", iter(["first", "second"]).__next__)
    monkeypatch.setattr(history, "record", lambda r, run_id: ids.update(history=run_id))
    monkeypatch.setattr(freeze, "freeze_result", lambda r, run_id=None, **k: ids.update(freeze=run_id))
    runners.run_asymmetric_growth(None)
    assert (ag["run_id"], ids["history"], ids["freeze"]) == ("first", "first", "first")


def test_an_ag_refresh_hands_no_id(ag, monkeypatch):
    # K4 (#383 R1): a Refresh reads the stored cards back and runs no scan, so it neither asks for nor hands an id.
    asked = []
    monkeypatch.setattr(runners, "_freeze_run_id", lambda: asked.append(1) or "job-ag")
    runners.run_asymmetric_growth(None, no_fetch=True)
    assert ag == {} and asked == []


def test_quick_hands_the_job_id_to_financials(monkeypatch, rawstore_tmp_db):
    import RevenueChaser.module_financials.run as fm
    seen = {}
    monkeypatch.setattr(fm, "run", lambda ticker, **k: seen.update(k) or {"paths": {}})
    monkeypatch.setattr(runners, "run_id", lambda: "job-q")
    monkeypatch.setattr(runners, "_copy_quick_artifact", lambda html: False)
    runners.run_quick(None, "MSFT")
    assert seen["run_id"] == "job-q"


@pytest.fixture
def earnings(monkeypatch, rawstore_tmp_db):
    ledger.take_notes()
    path = runners.OUTPUT_DIR / "t297" / "earnings_radar_latest.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runners, "EARNINGS_LATEST", path)
    monkeypatch.setattr(runners, "_load_watchlist", lambda: set())
    monkeypatch.setattr(runners, "_freeze_run_id", lambda: "job-er")
    monkeypatch.setattr(runners, "_record_earnings_history", lambda *a, **k: None)
    monkeypatch.setattr("RevenueChaser.module_sector_rotation.engine.rotation_quadrant_map", lambda uw: {})
    monkeypatch.setattr("RevenueChaser.module_earnings_radar.report.export_html",
                        lambda *a, **k: runners.EARNINGS_LATEST.write_text("<html/>", encoding="utf-8"))
    monkeypatch.setattr(vendor_data, "_clock", lambda: COMPLETE)

    class Pipe:
        def __init__(self, tickers, uw, raw_cal, etf_quadrant_map=None):
            self.tickers = tickers
            self._filter_stats = {"total": 1, "passed": 1, "bypassed": 0, "filtered": 0, "by_reason": {},
                                  "excluded": []}
            self.vendor = vendor_data.Capture()

        def run(self, use_preselect=True):
            self.vendor.add_uw("AAA", _uw_fin())
            return [object()]

    monkeypatch.setattr(runners, "EarningsPipeline", Pipe)
    monkeypatch.setattr(runners, "_compute_nrc_results", lambda cards: {})
    events = [{"ticker": "AAA", "earnings_date": date(2026, 10, 8), "earnings_time": "AMC", "source": "UW"}]
    monkeypatch.setattr(runners, "_fetch_calendar_range", lambda uw, s, e: (list(events), {}))


class _UW:
    auth_error = None

    def health(self):
        return {"calls": 1, "failed": 0, "failures": [], "auth_error": None, "breaker_open": False,
                "skipped_calls": 0}

    def screener_batch(self, tickers):
        return {}


def test_an_earnings_fetch_stores_the_uw_statements_with_the_job_id(earnings):
    runners.run_earnings_range(_UW(), date(2026, 10, 8), date(2026, 10, 8))
    rows = rawstore.query("SELECT DISTINCT ticker, source, run_id FROM vendor_financials")
    assert rows == [{"ticker": "AAA", "source": "uw", "run_id": "job-er"}]
