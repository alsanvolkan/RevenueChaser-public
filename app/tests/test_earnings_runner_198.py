"""#198 E-3: the dashboard Fetch augments the calendar with watchlist names (E15, one helper
shared with the CLI) and a run that scores 0 cards says so (E23). Offline.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage


@pytest.fixture
def latest(monkeypatch):
    path = runners.OUTPUT_DIR / "t198" / "earnings_radar_latest.html"   # OUTPUT_DIR is the test redirect
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runners, "EARNINGS_LATEST", path)
    return path


class _UW:
    """screener rows for the watchlist names; records the batch it was asked for."""

    def __init__(self, screener):
        self.screener = screener
        self.asked = []

    def screener_batch(self, tickers):
        self.asked.append(list(tickers))
        return {t: self.screener[t] for t in tickers if t in self.screener}


class _StubPipeline:
    last = None

    def __init__(self, tickers, uw, raw_cal, etf_quadrant_map=None):
        _StubPipeline.last = self
        self.tickers, self.raw_cal = tickers, raw_cal
        self.input_sources = {"calendar": {"stored": 0, "live": 0, "session": None}}
        self._filter_stats = {"total": len(tickers), "passed": 0, "bypassed": 0, "filtered": len(tickers),
                              "by_reason": {"OI": len(tickers)}, "excluded": []}

    def run(self, use_preselect=True):
        return []


def _patch_calendar(monkeypatch, events, raw_cal):
    monkeypatch.setattr(runners, "_fetch_calendar_range", lambda uw, s, e: (list(events), dict(raw_cal)))
    monkeypatch.setattr(runners, "_load_watchlist", lambda: {"QXO", "AAA"})
    monkeypatch.setattr(runners, "EarningsPipeline", _StubPipeline)
    monkeypatch.setattr("RevenueChaser.module_sector_rotation.engine.rotation_quadrant_map",
                        lambda uw: {})
    _stub_export(monkeypatch)


def _stub_export(monkeypatch):
    def fake_export(cards, events, nrc, filename=None, filter_stats=None, provider_health=None, notice=None, run_at=None):
        runners.EARNINGS_LATEST.write_text("<html>board</html>", encoding="utf-8")

    monkeypatch.setattr("RevenueChaser.module_earnings_radar.report.export_html", fake_export)


def _event(t):
    return {"ticker": t, "earnings_date": date(2026, 9, 8), "earnings_time": "BMO", "source": "UW"}


class TestDashboardWatchlistAugmentation:
    def test_watchlist_name_missing_from_the_calendar_joins_the_run(self, latest, monkeypatch):
        _patch_calendar(monkeypatch, [_event("AAA")], {"AAA": _event("AAA")})
        uw = _UW({"QXO": {"next_earnings_date": "2026-09-08", "er_time": "premarket", "sector": "Industrials",
                          "full_name": "QXO Inc", "implied_move_perc": 0.06}})
        with pytest.raises(JobMessage):
            runners.run_earnings_range(uw, date(2026, 9, 8), date(2026, 9, 8))
        assert uw.asked == [["QXO"]]                          # only the name the calendar lacks
        assert "QXO" in _StubPipeline.last.tickers
        assert _StubPipeline.last.raw_cal["QXO"]["source"] == "screener"

    def test_a_watchlist_name_reporting_outside_the_range_stays_out(self, latest, monkeypatch):
        _patch_calendar(monkeypatch, [_event("AAA")], {"AAA": _event("AAA")})
        uw = _UW({"QXO": {"next_earnings_date": "2026-10-20", "er_time": "premarket"}})
        with pytest.raises(JobMessage):
            runners.run_earnings_range(uw, date(2026, 9, 8), date(2026, 9, 8))
        assert "QXO" not in _StubPipeline.last.tickers

    def test_sparse_days_only_accept_names_reporting_on_those_days(self, latest, monkeypatch):
        monkeypatch.setattr(runners, "_fetch_calendar_days", lambda uw, days: ([_event("AAA")], {"AAA": _event("AAA")}))
        monkeypatch.setattr(runners, "_load_watchlist", lambda: {"QXO", "AAA"})
        monkeypatch.setattr(runners, "EarningsPipeline", _StubPipeline)
        monkeypatch.setattr("RevenueChaser.module_sector_rotation.engine.rotation_quadrant_map", lambda uw: {})
        _stub_export(monkeypatch)
        uw = _UW({"QXO": {"next_earnings_date": "2026-09-09", "er_time": "premarket"}})   # between the two days
        with pytest.raises(JobMessage):
            runners.run_earnings_range(uw, dates=[date(2026, 9, 8), date(2026, 9, 10)])
        assert "QXO" not in _StubPipeline.last.tickers

    def test_a_failed_augmentation_does_not_stop_the_run(self, latest, monkeypatch):
        _patch_calendar(monkeypatch, [_event("AAA")], {"AAA": _event("AAA")})

        class Boom(_UW):
            def screener_batch(self, tickers):
                raise RuntimeError("UW down")

        with pytest.raises(JobMessage):
            runners.run_earnings_range(Boom({}), date(2026, 9, 8), date(2026, 9, 8))
        assert _StubPipeline.last.tickers == ["AAA"]

    def test_watchlist_name_alone_makes_a_run_when_the_calendar_is_empty(self, latest, monkeypatch):
        _patch_calendar(monkeypatch, [], {})
        uw = _UW({"QXO": {"next_earnings_date": "2026-09-08", "er_time": "premarket"}})
        with pytest.raises(JobMessage) as exc:
            runners.run_earnings_range(uw, date(2026, 9, 8), date(2026, 9, 8))
        assert _StubPipeline.last.tickers == ["QXO"]
        assert "No earnings events" not in exc.value.message


class TestZeroCardsSayWhy:
    def test_all_filtered_run_raises_a_message_and_writes_the_board(self, latest, monkeypatch):
        _patch_calendar(monkeypatch, [_event("AAA"), _event("BBB")], {"AAA": _event("AAA"), "BBB": _event("BBB")})
        with pytest.raises(JobMessage) as exc:
            runners.run_earnings_range(_UW({}), date(2026, 9, 8), date(2026, 9, 8))
        assert "2" in exc.value.message and "filter" in exc.value.message.lower()
        assert "OI" in exc.value.message
        assert latest.exists()

    def test_scoring_failures_are_not_blamed_on_the_filters(self, latest, monkeypatch):
        class AllPassedNoneScored(_StubPipeline):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self._filter_stats = {"total": 2, "passed": 2, "bypassed": 0, "filtered": 0,
                                      "by_reason": {}, "excluded": []}

        _patch_calendar(monkeypatch, [_event("AAA"), _event("BBB")], {"AAA": _event("AAA"), "BBB": _event("BBB")})
        monkeypatch.setattr(runners, "EarningsPipeline", AllPassedNoneScored)
        with pytest.raises(JobMessage) as exc:
            runners.run_earnings_range(_UW({}), date(2026, 9, 8), date(2026, 9, 8))
        assert "could not be scored" in exc.value.message


class _CountingUW(_UW):
    """Adds the live calendar read E5a must not call when the store covers the day."""

    def __init__(self, screener, calendar_rows=None):
        super().__init__(screener)
        self.calendar_rows = calendar_rows or {}
        self.calendar_calls = []

    def calendar_day_raw(self, d, session):
        self.calendar_calls.append((d, session))
        return list(self.calendar_rows.get((d, session), []))


@pytest.fixture
def recorded(monkeypatch):
    """Capture what ``history.record`` was handed, without touching the history file."""
    from RevenueChaser.module_earnings_radar import history as er_history
    calls = []
    monkeypatch.setattr(er_history, "record", lambda *a, **k: calls.append((a, k)))
    return calls


class TestInputSources:
    """E5a (#372): the run says what it read from the store and what it asked UW for."""

    def _seed_calendar(self, day, swept=None):
        from RevenueChaser.core import rawstore
        rawstore.ensure_all_tables()
        for session in ("premarket", "afterhours"):
            rawstore.write_rows("earnings_calendar", [{
                "session_date": swept or day, "earnings_date": day, "ticker": "AAA", "report_session": session,
                "item": {"symbol": "AAA", "sector": "Technology", "full_name": "AAA Inc"}, "source": "uw"}])

    def test_the_calendar_slot_names_the_stored_session_and_zero_live_pairs(self, latest, monkeypatch, recorded):
        from datetime import datetime, timezone
        from RevenueChaser.core import calendar
        day = date(2026, 9, 11)       # Friday: past today and the next trading day, which always read live (S4)
        monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc))
        self._seed_calendar(day, swept=date(2026, 9, 8))
        monkeypatch.setattr(runners, "_load_watchlist", lambda: set())
        monkeypatch.setattr(runners, "EarningsPipeline", _StubPipeline)
        monkeypatch.setattr("RevenueChaser.module_sector_rotation.engine.rotation_quadrant_map", lambda uw: {})
        _stub_export(monkeypatch)
        uw = _CountingUW({})
        with pytest.raises(JobMessage):
            runners.run_earnings_range(uw, day, day)
        assert uw.calendar_calls == []                       # the store covered both sessions
        assert recorded[-1][1]["input_sources"]["calendar"] == {
            "stored": 2, "live": 0, "session": "2026-09-08"}

    def test_an_uncovered_pair_is_reported_live(self, latest, monkeypatch, recorded):
        from datetime import datetime, timezone
        from RevenueChaser.core import calendar
        day = date(2026, 9, 8)
        monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc))
        monkeypatch.setattr(runners, "_load_watchlist", lambda: set())
        monkeypatch.setattr(runners, "EarningsPipeline", _StubPipeline)
        monkeypatch.setattr("RevenueChaser.module_sector_rotation.engine.rotation_quadrant_map", lambda uw: {})
        _stub_export(monkeypatch)
        uw = _CountingUW({}, {(day, "premarket"): [{"symbol": "AAA"}]})

        with pytest.raises(JobMessage):
            runners.run_earnings_range(uw, day, day)

        assert uw.calendar_calls == [(day, "premarket"), (day, "afterhours")]
        assert recorded[-1][1]["input_sources"]["calendar"] == {
            "stored": 0, "live": 2, "session": None}

    def test_an_empty_calendar_run_still_records_its_sources(self, latest, monkeypatch, recorded):
        _patch_calendar(monkeypatch, [], {})
        monkeypatch.setattr(runners, "_load_watchlist", lambda: set())
        with pytest.raises(JobMessage):
            runners.run_earnings_range(_UW({}), date(2026, 9, 8), date(2026, 9, 8))
        assert recorded[-1][1]["input_sources"] == {"calendar": {"stored": 0, "live": 0, "session": None}}

