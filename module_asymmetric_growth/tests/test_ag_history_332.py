"""Asymmetric Growth board -> result history (#332, #278 G1 first writer). Offline; the store is a temp DuckDB.

An AG Fetch records its run through ``core/history.record_run``: one ``result_runs`` header plus the rows it gave
``ticker_scores`` (``ag_scores_history``, a mirror), in one transaction on history.duckdb.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import json

import pytest

from RevenueChaser.app import runners
from RevenueChaser.core import ledger, rawstore, ticker_store
from RevenueChaser.module_asymmetric_growth import engine, freeze
from RevenueChaser.module_asymmetric_growth import history as ag_history

BOARD, TABLE = "module_asymmetric_growth", "ag_scores_history"
# 2026-10-06 is a Tuesday: 22:00 UTC = 18:00 ET (session complete); 14:00 UTC = 10:00 ET (intraday).
COMPLETE_1 = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc)
COMPLETE_2 = datetime(2026, 10, 6, 23, 0, tzinfo=timezone.utc)
INTRADAY = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)


def _card(ticker, points=9, **kw):
    c = {"ticker": ticker, "name": f"{ticker} Inc", "sector": "Information Technology", "price": 12.5,
         "market_cap": 8.0e8, "total_points": points, "themes": ["AI"], "signals": ["fcf"], "tech_breakthrough": True}
    c.update(kw)
    return c


def _result(cards, when=COMPLETE_1, **kw):
    r = {"asof": when.isoformat(), "universe_total": len(cards), "universe_generated_at": when.isoformat(),
         "funnel_steps": [{"label": "scanned", "n": len(cards)}],
         "summary": {"excluded_types": 0, "score_errors": 0, "score_error_types": {}}, "cards": cards}
    r.update(kw)
    return r


def _headers():
    return rawstore.query("SELECT * FROM result_runs WHERE board = ? ORDER BY run_at", [BOARD], db="history")


def _rows(run_id):
    return rawstore.query(f"SELECT * FROM {TABLE} WHERE run_id = ? ORDER BY ticker", [run_id], db="history")


def _spy_bar(when=date(2026, 10, 6), minutes=45):
    """SPY's D01 bar for the session: the run is complete for the board when Daily Bars has written it (#380 S4)."""
    from RevenueChaser.core import calendar as cal
    from RevenueChaser.core.tests.d01_seed import seed_d01
    seed_d01("SPY", [when], (cal.session_close(when) + timedelta(minutes=minutes)).timestamp())


@pytest.fixture
def store(rawstore_tmp_db):
    ledger.take_notes()
    _spy_bar()
    return rawstore_tmp_db


def test_a_run_records_a_header_and_the_rows_the_latest_table_got(store):
    cards = [_card("AAA", 9), _card("BBB", 4, themes=[], signals=[])]
    ag_history.record(_result(cards), run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "complete" and head["session_snapshot"] is True and head["error"] is None
    assert json.loads(head["row_counts"]) == {TABLE: 2}
    rows = _rows("r1")
    assert [r["ticker"] for r in rows] == ["AAA", "BBB"]
    assert rows[0]["total_points"] == 9 and json.loads(rows[0]["themes"]) == ["AI"] and rows[0]["tech_breakthrough"] == 1
    # the same rows ticker_scores takes
    ticker_store.write_from_cards(cards, None)
    latest = {r["ticker"]: r for r in rawstore.query("SELECT * FROM ticker_scores")}
    for r in rows:
        assert {k: v for k, v in r.items() if k != "run_id"} == latest[r["ticker"]]


def test_the_header_carries_model_version_funnel_and_context(store):
    ag_history.record(_result([_card("AAA")], universe_generated_at="2026-10-06T21:00:00+00:00"), run_id="r1")
    (head,) = _headers()
    assert head["model_version"] == freeze.model_version()
    assert json.loads(head["funnel"]) == [{"label": "scanned", "n": 1}]
    assert json.loads(head["params"])["THRESHOLDS"] == engine.THRESHOLDS
    assert json.loads(head["context"])["universe_generated_at"] == "2026-10-06T21:00:00+00:00"


def test_the_header_carries_the_price_session(store):
    """#380 S4: the header records the D01 session the Fetch priced from, so a Refresh can say it."""
    ag_history.record(_result([_card("AAA")], data_as_of="2026-10-06"), run_id="r1")
    (head,) = _headers()
    assert json.loads(head["context"])["data_as_of"] == "2026-10-06"


def test_a_second_complete_run_of_the_session_keeps_its_header_only(store):
    ag_history.record(_result([_card("AAA")], COMPLETE_1), run_id="r1")
    ag_history.record(_result([_card("AAA"), _card("BBB")], COMPLETE_2), run_id="r2")
    assert [h["session_snapshot"] for h in _headers()] == [True, False]
    assert len(_rows("r1")) == 1 and _rows("r2") == []


def test_recording_the_same_run_id_twice_writes_nothing_more(store):
    ag_history.record(_result([_card("AAA")]), run_id="r1")
    ag_history.record(_result([_card("AAA"), _card("BBB")]), run_id="r1")
    assert len(_headers()) == 1 and len(_rows("r1")) == 1


def test_an_intraday_run_is_not_a_snapshot(store):
    ag_history.record(_result([_card("AAA")], INTRADAY), run_id="early")
    (head,) = _headers()
    assert head["quality"] == "intraday" and head["session_snapshot"] is False and _rows("early") == []


def test_a_partial_universe_is_recorded_partial_and_keeps_no_rows(store):
    ag_history.record(_result([_card("AAA")], universe_partial=True), run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "partial" and "universe" in head["quality_note"] and _rows("r1") == []


def test_too_many_scoring_errors_make_the_run_partial(store):
    res = _result([_card("AAA")], universe_total=101)
    res["summary"] = {"excluded_types": 0, "score_errors": 100, "score_error_types": {"KeyError": 100}}
    ag_history.record(res, run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "partial" and "failed to score" in head["quality_note"]
    assert json.loads(head["warnings"]) == [{"score_error_types": {"KeyError": 100}}] and _rows("r1") == []


def test_a_fetch_with_no_cards_still_gets_a_header_and_is_never_the_snapshot(store):
    ag_history.record(_result([], no_data_message="No universe stored."), run_id="empty")
    (head,) = _headers()
    assert head["quality"] == "partial" and head["session_snapshot"] is False
    assert head["quality_note"].startswith("rows not kept (0 given)") and "No universe stored." in head["quality_note"]
    ag_history.record(_result([_card("AAA")], COMPLETE_2), run_id="real")          # the real run still takes the session
    assert [h["session_snapshot"] for h in _headers()] == [False, True] and len(_rows("real")) == 1


def test_a_post_close_scan_without_spy_s_d01_bar_is_not_the_snapshot_a_later_one_is(store):
    """#380 S4: the session's snapshot needs Daily Bars' SPY bar; a run before it reaches the session header only."""
    from RevenueChaser.core import rawstore as rs
    with rs.connect(db="prices") as conn:
        conn.execute("DELETE FROM price_bars_daily")                 # Daily Bars has not run for the session
    ag_history.record(_result([_card("AAA")]), run_id="early")
    (head,) = _headers()
    assert head["quality"] == "partial" and head["session_snapshot"] is False
    assert "no SPY D01 bar for the 2026-10-06 session" in head["quality_note"] and _rows("early") == []
    _spy_bar()                                                       # Daily Bars wrote it: the next run takes the day
    ag_history.record(_result([_card("AAA"), _card("BBB")]), run_id="fresh")
    assert [h["session_snapshot"] for h in _headers()] == [False, True] and len(_rows("fresh")) == 2


def test_a_naive_asof_is_read_as_utc(store):
    ag_history.record(_result([_card("AAA")], universe_generated_at=COMPLETE_1.isoformat(),
                              asof="2026-10-06T22:00:00"), run_id="r1")
    (head,) = _headers()
    assert head["session_date"].isoformat() == "2026-10-06" and head["quality"] == "complete"


def test_a_card_without_a_ticker_is_left_out_like_ticker_scores_does(store):
    ag_history.record(_result([_card("AAA"), {"total_points": 3}]), run_id="r1")
    assert [r["ticker"] for r in _rows("r1")] == ["AAA"]


# ── the Fetch runner ─────────────────────────────────────────────────────────

@pytest.fixture
def runner(monkeypatch, store):
    from RevenueChaser.module_asymmetric_growth import report as ag_report
    result = _result([_card("AAA")])
    monkeypatch.setattr(engine, "run_asymmetric_growth_hunter", lambda *a, **k: result)
    monkeypatch.setattr(ag_report, "render_asymmetric_growth_hunter_html", lambda r: "<html/>")
    monkeypatch.setattr(runners, "_write_output", lambda *a, **k: None)
    monkeypatch.setattr(freeze, "freeze_result", lambda *a, **k: None)
    return result


def test_a_fetch_records_history_with_the_job_run_id(runner, monkeypatch):
    monkeypatch.setattr(runners, "_freeze_run_id", lambda: "job42")
    runners.run_asymmetric_growth(None)
    assert [h["run_id"] for h in _headers()] == ["job42"] and len(_rows("job42")) == 1


def test_a_refresh_never_records_history(runner):
    runners.run_asymmetric_growth(None, no_fetch=True)
    assert not rawstore.history_db_path().exists()


def test_a_history_failure_is_a_job_note_and_the_fetch_still_freezes(runner, monkeypatch):
    seen = []
    monkeypatch.setattr(ag_history, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    monkeypatch.setattr(freeze, "freeze_result", lambda *a, **k: seen.append("freeze"))
    runners.run_asymmetric_growth(None)
    assert seen == ["freeze"]
    assert any("disk full" in n and "history" in n for n in ledger.take_notes())
    assert rawstore.count("ticker_scores") == 1                          # the latest table stayed written


def test_the_history_runs_before_the_freeze_with_the_same_run_id(runner, monkeypatch):
    order = []
    monkeypatch.setattr(ag_history, "record", lambda r, run_id: order.append(("history", run_id)))
    monkeypatch.setattr(freeze, "freeze_result", lambda r, run_id=None, **k: order.append(("freeze", run_id)))
    monkeypatch.setattr(runners, "_freeze_run_id", iter(["a", "b"]).__next__)             # a second call would give "b"
    runners.run_asymmetric_growth(None)
    assert order == [("history", "a"), ("freeze", "a")]
