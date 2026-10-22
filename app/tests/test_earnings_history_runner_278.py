"""#278 Earnings Radar writer: the dashboard Fetch records its run in result history. Offline, temp stores.

Fetch only (a Refresh never records: Earnings has none, it is live-only); a history failure is a job note and never
blocks the Fetch; a Fetch that ends in a JobMessage (no events, no cards) still gets its header.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.core.models.events import TradeCard
from RevenueChaser.module_earnings_radar import history as er_history

BOARD = "module_earnings_radar"


@pytest.fixture
def run(monkeypatch, rawstore_tmp_db):
    ledger.take_notes()
    path = runners.OUTPUT_DIR / "t278" / "earnings_radar_latest.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runners, "EARNINGS_LATEST", path)
    monkeypatch.setattr(runners, "_load_watchlist", lambda: {"AAA"})
    monkeypatch.setattr(runners, "_freeze_run_id", lambda: "job42")
    monkeypatch.setattr("RevenueChaser.module_sector_rotation.engine.rotation_quadrant_map",
                        lambda uw, **kw: {"XLK": "LEADING"})
    order = []

    def fake_export(cards, events, nrc, filename=None, filter_stats=None, provider_health=None, notice=None, run_at=None):
        order.append("board")
        runners.EARNINGS_LATEST.write_text("<html>board</html>", encoding="utf-8")

    monkeypatch.setattr("RevenueChaser.module_earnings_radar.report.export_html", fake_export)
    state = {"cards": [], "stats": None, "order": order}

    class Pipe:
        def __init__(self, tickers, uw, raw_cal, etf_quadrant_map=None):
            self.tickers = tickers
            self._filter_stats = state["stats"] or {"total": len(tickers), "passed": len(state["cards"]),
                                                    "bypassed": 0, "filtered": 0, "by_reason": {}, "excluded": []}

        def run(self, use_preselect=True):
            return list(state["cards"])

    monkeypatch.setattr(runners, "EarningsPipeline", Pipe)
    state["events"] = [{"ticker": "AAA", "earnings_date": date(2026, 10, 8), "earnings_time": "AMC", "source": "UW"}]
    monkeypatch.setattr(runners, "_fetch_calendar_range", lambda uw, s, e: (list(state["events"]), {}))
    return state


class _UW:
    def __init__(self, **health):
        self._h = {"calls": 100, "failed": 0, "failures": [], "auth_error": None, "breaker_open": False,
                   "skipped_calls": 0, **health}
        self.auth_error = self._h["auth_error"]

    def health(self):
        return self._h

    def screener_batch(self, tickers):
        return {}


def _card(t="AAA"):
    return TradeCard(ticker=t, earnings_date=date(2026, 10, 8), earnings_time="AMC", final_action="WATCH_ONLY",
                     raw_packet_json="{}")


def _headers():
    return rawstore.query("SELECT * FROM result_runs WHERE board = ?", [BOARD], db="history")


def _fetch(uw=None):
    return runners.run_earnings_range(uw or _UW(), date(2026, 10, 8), date(2026, 10, 8))


def test_a_fetch_records_history_with_the_job_run_id_after_the_board_is_written(run, monkeypatch):
    run["cards"] = [_card("AAA"), _card("BBB")]
    seen = []
    real = er_history.record
    monkeypatch.setattr(er_history, "record", lambda *a, **k: (seen.append(list(run["order"])), real(*a, **k))[1])
    assert _fetch() == runners.EARNINGS_LATEST
    (head,) = _headers()
    assert head["run_id"] == "job42" and json.loads(head["row_counts"]) == {"earnings_cards_history": 2}
    assert seen == [["board"]]                                   # the board file existed before the history call
    assert json.loads(head["context"])["events"] == 1


def test_a_history_failure_is_a_job_note_and_the_fetch_still_returns(run, monkeypatch):
    run["cards"] = [_card()]
    monkeypatch.setattr(er_history, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    assert _fetch() == runners.EARNINGS_LATEST
    assert any("disk full" in n and "history" in n for n in ledger.take_notes())
    assert runners.EARNINGS_LATEST.exists()


def test_a_zero_card_fetch_records_a_header_and_still_raises_its_message(run):
    run["cards"] = []
    run["stats"] = {"total": 1, "passed": 0, "bypassed": 0, "filtered": 1, "by_reason": {"OI": 1}, "excluded": []}
    with pytest.raises(JobMessage):
        _fetch()
    (head,) = _headers()
    assert head["quality"] == "partial" and "filter" in head["quality_note"].lower()
    assert json.loads(head["funnel"])["by_reason"] == {"OI": 1}


def test_a_history_failure_does_not_replace_the_zero_card_message(run, monkeypatch):
    run["cards"] = []
    monkeypatch.setattr(er_history, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    with pytest.raises(JobMessage) as exc:
        _fetch()
    assert "disk full" not in exc.value.message and any("disk full" in n for n in ledger.take_notes())


def test_an_empty_calendar_records_a_header_and_still_raises(run):
    run["events"] = []
    with pytest.raises(JobMessage) as exc:
        _fetch()
    assert "No earnings events" in exc.value.message
    (head,) = _headers()
    assert head["quality"] == "partial" and "No earnings events" in head["quality_note"]


def test_an_empty_calendar_after_a_rejected_key_says_so_in_the_header(run):
    run["events"] = []
    with pytest.raises(JobMessage):
        _fetch(_UW(auth_error={"status": 401}))
    (head,) = _headers()
    assert "rejected the API key" in head["quality_note"]


def test_a_failed_rotation_map_is_recorded_partial_with_rows_kept(run, monkeypatch):
    run["cards"] = [_card()]
    monkeypatch.setattr("RevenueChaser.module_sector_rotation.engine.rotation_quadrant_map",
                        lambda uw, **kw: (_ for _ in ()).throw(RuntimeError("no rotation")))
    _fetch()
    (head,) = _headers()
    assert head["quality"] == "partial" and "rotation" in head["quality_note"]
    assert json.loads(head["row_counts"]) == {"earnings_cards_history": 1}


def test_an_empty_rotation_map_is_recorded_partial_with_rows_kept(run, monkeypatch):
    run["cards"] = [_card()]
    monkeypatch.setattr("RevenueChaser.module_sector_rotation.engine.rotation_quadrant_map", lambda uw, **kw: {})
    _fetch()
    (head,) = _headers()
    assert head["quality"] == "partial" and "rotation" in head["quality_note"]


def test_a_healthy_fetch_is_recorded_with_a_session_and_no_note(run):
    run["cards"] = [_card()]
    _fetch()
    (head,) = _headers()
    assert head["quality"] in ("complete", "intraday") and head["session_date"] is not None
    assert head["quality_note"] is None


def test_uw_failures_make_the_run_partial_and_keep_the_rows(run):
    run["cards"] = [_card()]
    _fetch(_UW(failed=50))
    (head,) = _headers()
    assert json.loads(head["row_counts"]) == {"earnings_cards_history": 1} and "50 of 100" in head["quality_note"]


def test_nothing_but_the_fetch_runner_calls_the_writer():
    import pathlib
    root = pathlib.Path(runners.__file__).resolve().parents[1]
    # Posix relative paths and path parts, so the check holds on Windows too; .cache (local research copies) is
    # not the app.
    users = sorted(rel.as_posix() for p in root.rglob("*.py")
                   for rel in [p.relative_to(root)]
                   if "tests" not in rel.parts and ".cache" not in rel.parts and p.name != "history.py"
                   and "er_history" in p.read_text("utf-8", errors="replace"))
    assert users == ["app/runners.py"]


def test_the_earnings_board_has_no_refresh_path():
    from RevenueChaser.app import dashboard
    assert BOARD not in dashboard._STORE_REFRESHABLE
