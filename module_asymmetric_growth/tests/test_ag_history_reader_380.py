"""Asymmetric Growth board <- result history: the first board that reads it back (#380, core review slice A2).

The writer (#332) records each AG Fetch through ``core/history.record_run``. This reader goes the other way, through
the generated read views (``result_sessions``, ``ag_scores_by_session``, ``core/rawstore._history_views``) and
``result_runs``, so the view shape is exercised end to end before more writers are built. Offline; the store is a
temp DuckDB.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_asymmetric_growth import engine
from RevenueChaser.module_asymmetric_growth import history as ag_history
from RevenueChaser.module_asymmetric_growth import report

# Tue-Thu 2026-10-06..08: 22:00 UTC = 18:00 ET (session complete); 14:00 UTC = 10:00 ET (intraday).
TUE, TUE_LATER = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc), datetime(2026, 10, 6, 23, 0, tzinfo=timezone.utc)
WED = datetime(2026, 10, 7, 22, 0, tzinfo=timezone.utc)
THU = datetime(2026, 10, 8, 22, 0, tzinfo=timezone.utc)
THU_INTRADAY = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)


def _card(ticker, points):
    return {"ticker": ticker, "name": f"{ticker} Inc", "sector": "Information Technology", "price": 12.5,
            "market_cap": 8.0e8, "total_points": points, "themes": [], "signals": []}


def _record(cards, when, run_id, **kw):
    from RevenueChaser.core import calendar as cal
    from RevenueChaser.core.tests.d01_seed import seed_d01
    stamp = cal.session_for(when.timestamp())
    if stamp.quality == cal.QUALITY_COMPLETE:
        # Daily Bars wrote the session's SPY bar: the freeze anchor and so the session snapshot (#380 S4).
        seed_d01("SPY", [stamp.session_date],
                 (cal.session_close(stamp.session_date) + timedelta(minutes=45)).timestamp())
    r = {"asof": when.isoformat(), "universe_total": len(cards), "universe_generated_at": when.isoformat(),
         "funnel_steps": None, "summary": {"score_errors": 0, "score_error_types": {}}, "cards": cards}
    r.update(kw)
    return ag_history.record(r, run_id=run_id)


@pytest.fixture
def store(rawstore_tmp_db):
    ledger.take_notes()
    return rawstore_tmp_db


def test_no_recorded_run_reads_empty_and_does_not_create_the_history_file(store):
    assert not rawstore.history_db_path().exists()
    assert ag_history.recorded_sessions() == []
    assert not rawstore.history_db_path().exists()        # J3: only the first record_run makes the file


def test_sessions_come_newest_first_from_the_views(store):
    _record([_card("AAA", 9), _card("BBB", 4)], TUE, "tue")
    _record([_card("AAA", 9), _card("BBB", 9), _card("CCC", 9)], TUE_LATER, "tue-2")   # header only (rule B)
    _record([_card("CCC", 7), _card("AAA", 8), _card("BBB", 8)], WED, "wed")
    tue_run = rawstore.query("SELECT run_at FROM result_runs WHERE run_id = 'tue'", db="history")[0]["run_at"]
    wed, tue = ag_history.recorded_sessions()
    assert (wed["session_date"], wed["run_id"], wed["runs"], wed["names"]) == (date(2026, 10, 7), "wed", 1, 3)
    assert wed["top"] == ["AAA", "BBB", "CCC"]           # points desc, then ticker
    assert wed["avg_points"] == pytest.approx(23 / 3)
    # the second Tuesday Fetch is counted as a run but its rows are not the session's: the snapshot is the first
    assert (tue["session_date"], tue["run_id"], tue["runs"], tue["names"]) == (date(2026, 10, 6), "tue", 2, 2)
    assert tue["run_at"] == tue_run and tue["top"] == ["AAA", "BBB"] and tue["avg_points"] == pytest.approx(6.5)


def test_rows_come_through_the_session_view_not_every_run(store):
    """Only the snapshot run's rows are the session's, even if another run of that session has rows."""
    _record([_card("AAA", 9)], TUE, "tue")
    _record([_card("AAA", 9)], TUE_LATER, "tue-2")                                    # header only
    rawstore.write_rows("ag_scores_history", [{"run_id": "tue-2", "ticker": "ZZZ", "total_points": 9}])
    (tue,) = ag_history.recorded_sessions()
    assert (tue["run_id"], tue["names"], tue["top"]) == ("tue", 1, ["AAA"])


def test_a_session_with_no_snapshot_is_not_listed(store):
    _record([_card("AAA", 9)], THU_INTRADAY, "early")           # intraday: header only, never the snapshot
    assert ag_history.recorded_sessions() == []
    _record([_card("AAA", 9)], THU, "thu")
    (thu,) = ag_history.recorded_sessions()
    assert (thu["run_id"], thu["runs"], thu["names"]) == ("thu", 2, 1)


def test_limit_keeps_the_newest_sessions_and_top_is_capped(store):
    _record([_card("AAA", 1)], TUE, "tue")
    _record([_card("AAA", 1)], WED, "wed")
    _record([_card(t, 9 - i) for i, t in enumerate(["F", "E", "D", "C", "B", "A", "G"])], THU, "thu")
    rows = ag_history.recorded_sessions(limit=2)
    assert [r["run_id"] for r in rows] == ["thu", "wed"]
    assert rows[0]["top"] == ["F", "E", "D", "C", "B"] and len(rows[0]["top"]) == ag_history.TOP_N


def test_another_boards_sessions_are_not_read(store):
    from RevenueChaser.core import history
    history.record_run("module_swing_potential", run_id="swing", run_at=TUE.timestamp(), model_version=None,
                       rows={"swing_scores_history": []})
    assert ag_history.recorded_sessions() == []


def _result():
    return {"asof": THU.isoformat(), "cards": [], "summary": {}, "thresholds": engine.THRESHOLDS}


def test_the_board_shows_the_recorded_sessions(store):
    _record([_card("AAA", 9), _card("BBB", 4)], TUE, "tue")
    _record([_card("ZZZ", 8)], WED, "wed")
    html = report.render_asymmetric_growth_hunter_html(_result())
    panel = html[html.index('id="history-panel"'):]
    panel = panel[:panel.index("</details>")]
    assert panel.index("2026-10-07") < panel.index("2026-10-06")
    assert "ZZZ" in panel and "AAA, BBB" in panel and "6.5" in panel


def test_the_board_says_when_nothing_is_recorded(store):
    html = report.render_asymmetric_growth_hunter_html(_result())
    assert 'id="history-panel"' in html and "No session recorded yet" in html
    assert not rawstore.history_db_path().exists()


def test_a_history_read_failure_is_shown_not_raised(store, monkeypatch):
    def boom(limit=ag_history.SESSIONS_SHOWN):
        raise rawstore.RawStoreError("could not open history.duckdb: held by another process")
    monkeypatch.setattr(ag_history, "recorded_sessions", boom)
    html = report.render_asymmetric_growth_hunter_html(_result())
    assert "History could not be read: RawStoreError: could not open history.duckdb" in html


def test_stored_text_is_escaped_in_the_panel():
    html = report._history_panel([{"session_date": date(2026, 10, 6), "run_id": "r", "run_at": TUE.timestamp(),
                                   "runs": 1, "names": 1, "avg_points": 9.0, "top": ["<b>X"]}])
    assert "<b>X" not in html and "&lt;b&gt;X" in html
