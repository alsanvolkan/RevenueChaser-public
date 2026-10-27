"""Fundamentals board -> result history (#278 G1, third writer after AG #332 and Swing). Offline; temp DuckDB store.

A Fundamentals Fetch records its run through ``core/history.record_run``: one ``result_runs`` header plus the rows it
gave ``fundamentals_scores`` and ``fundamentals_gates`` (``fundamentals_scores_history`` / ``..._gates_history``,
mirrors), in one transaction on history.duckdb.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from RevenueChaser.app import runners
from RevenueChaser.core import calendar as cal
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.core.tests.d01_seed import seed_d01
from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_fundamentals import freeze, pipeline, scores
from RevenueChaser.module_fundamentals import history as fu_history

BOARD, SCORES, GATES = "module_fundamentals", "fundamentals_scores_history", "fundamentals_gates_history"
CUR = sec_edgar.EXTRACT_VERSION
# 2026-10-06 is a Tuesday: 22:00 UTC = 18:00 ET (session complete); 14:00 UTC = 10:00 ET (intraday).
D = date(2026, 10, 6)
COMPLETE_1 = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc)
COMPLETE_2 = datetime(2026, 10, 6, 23, 0, tzinfo=timezone.utc)
INTRADAY = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)


def _metrics(i=0):
    return {"ro40": 50.0 - i, "ro40_basis": "TTM", "rev_growth_ttm": 30.0, "fcf_margin": 20.0, "ttm_revenue": 1e9,
            "gross_margin": 0.5, "roic": 0.2, "net_debt_ebitda": 1.0, "pe": 20.0, "pb": 3.0,
            "dist_52w_pct": 0.9, "rs_6m": 0.1, "above_200d": True}


def _results(*tickers, version=CUR):
    return [{"ticker": t, "period": "Q2 vs Q2", "metrics": _metrics(i), "edgar_extract_version": version}
            for i, t in enumerate(tickers)]


def _rows(results):
    """What ``scores.persist`` hands the latest tables for these results."""
    sink: dict = {}
    scores.persist(results, sink=sink)
    return sink


def _payload(when=COMPLETE_1, *tickers, **kw):
    p = {"generated_at": when.isoformat(), "data_as_of": "2026-10-06", "regime": "RISK-ON", "universe_total": 900,
         "universe_generated_at": when.isoformat(), "universe_refreshed_by": "Fundamentals",
         "universe_partial": False, "passing": {"rule_of_40": 1, "quality": 1, "momentum": 1},
         "tiers": {"triple": 1, "dual": 0, "ro40": 0, "single_other": 0}, "distorted_count": 0,
         "no_data": False,
         "data_quality": {"stale_ohlc": 0, "spy_stale": False, "dropped": {}},
         "summary": {"filtered_universe": 400, "hygiene": 100, "data_short": 5, "no_edgar": 2, "top_n": len(tickers)},
         "candidates": [{"ticker": t, "multiplier": 3, "ro40": 50.0} for t in tickers]}
    p.update(kw)
    return p


def _seed(ticker, cached_at, last_day="2026-10-06"):
    """The bars in D01 (the picks and SPY, read first; #383 R3 C12/K8b) and in ``swing_ohlc`` (SPY's fallback)."""
    rawstore.write_rows("swing_ohlc", [
        {"ticker": ticker, "ts": f"{d}T00:00:00", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
         "volume": 100.0, "cached_at": cached_at} for d in ("2026-10-02", "2026-10-05", last_day)])
    seed_d01(ticker, ("2026-10-02", "2026-10-05", last_day), cached_at)


def _fresh():
    return cal.session_close(D).timestamp() + 45 * 60        # cached 45 min after the close


def _headers():
    return rawstore.query("SELECT * FROM result_runs WHERE board = ? ORDER BY run_at", [BOARD], db="history")


def _score_rows(run_id):
    return rawstore.query(f"SELECT * FROM {SCORES} WHERE run_id = ? ORDER BY ticker", [run_id], db="history")


def _gate_rows(run_id):
    return rawstore.query(f"SELECT * FROM {GATES} WHERE run_id = ? ORDER BY ticker, gate, rule_id", [run_id],
                          db="history")


@pytest.fixture
def store(rawstore_tmp_db):
    ledger.take_notes()
    for t in ("SPY", "AAA", "BBB"):
        _seed(t, _fresh())
    return rawstore_tmp_db


# ── the sink: the writer gets exactly the rows the latest tables got ─────────

def test_persist_hands_its_rows_to_the_sink_and_writes_them_to_the_latest_tables(store):
    sink: dict = {}
    out = scores.persist(_results("AAA", "BBB"), sink=sink)
    assert out == {"scores": 2, "gates": 14}
    assert [r["ticker"] for r in sink["scores"]] == ["AAA", "BBB"] and len(sink["gates"]) == 14
    latest = {r["ticker"]: r for r in rawstore.query("SELECT * FROM fundamentals_scores")}
    assert {r["ticker"]: r for r in sink["scores"]} == latest


def test_persist_without_a_sink_is_unchanged(store):
    assert scores.persist(_results("AAA")) == {"scores": 1, "gates": 7}


def test_the_sink_counts_the_edgar_extract_versions_of_the_scored_tickers(store):
    results = _results("AAA", "BBB", "CCC") + _results("DDD", version=None)
    results[2]["edgar_extract_version"] = 1
    sink = _rows(results)
    assert sink["edgar_versions"] == {str(CUR): 2, "1": 1, "none": 1}


# ── _compute_row carries the version of the EDGAR row behind the metrics ─────

@pytest.mark.parametrize("meta, want", [({"extract_version": CUR}, CUR), ({"extract_version": 1}, 1), ({}, None)])
def test_compute_row_carries_the_extract_version_of_the_edgar_row(monkeypatch, meta, want):
    monkeypatch.setattr(pipeline.sec_edgar, "get_fundamentals", lambda t, no_fetch=False: ([{"x": 1}], [], meta))
    frame = pd.DataFrame({"Close": [1.0]})
    monkeypatch.setattr(pipeline.screen_common, "load_bars", lambda *a, **k: (frame, frame))     # D01 (C12)
    monkeypatch.setattr(pipeline, "load_ticker_info", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "ohlc_drop_reason", lambda df: None)
    monkeypatch.setattr(pipeline, "compute_indicators", lambda df, spy, **k: {"price": 10.0, "sma200": 5.0,
                                                                              "within_52w_high_pct": 0.9})
    monkeypatch.setattr(pipeline, "_ttm_metrics", lambda q, a: {k: None for k in (
        "rev_growth_ttm", "fcf_margin", "ro40", "ro40_basis", "ttm_revenue", "ttm_net_income")})
    monkeypatch.setattr(pipeline, "_quality_metrics", lambda a: {k: None for k in (
        "gross_margin", "roic", "net_debt_ebitda", "total_equity")})
    row = pipeline._compute_row({"ticker": "AAA", "market_cap": 1e9}, None, spine={})
    assert row["edgar_extract_version"] == want


# ── the writer ───────────────────────────────────────────────────────────────

def test_a_run_records_a_header_and_the_rows_the_latest_tables_got(store):
    rows = _rows(_results("AAA", "BBB"))
    fu_history.record(_payload(COMPLETE_1, "AAA"), rows=rows, run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "complete" and head["session_snapshot"] is True and head["error"] is None
    assert json.loads(head["row_counts"]) == {SCORES: 2, GATES: 14}
    got = _score_rows("r1")
    assert [r["ticker"] for r in got] == ["AAA", "BBB"]
    latest = {r["ticker"]: r for r in rawstore.query("SELECT * FROM fundamentals_scores")}
    for r in got:
        assert {k: v for k, v in r.items() if k != "run_id"} == latest[r["ticker"]]
    latest_gates = rawstore.query("SELECT * FROM fundamentals_gates ORDER BY ticker, gate, rule_id")
    assert [{k: v for k, v in r.items() if k != "run_id"} for r in _gate_rows("r1")] == latest_gates


def test_the_header_carries_model_version_params_and_context(store):
    fu_history.record(_payload(COMPLETE_1, "AAA"), rows=_rows(_results("AAA")), run_id="r1")
    (head,) = _headers()
    assert head["model_version"] == freeze.model_version()
    assert json.loads(head["params"]) == freeze.effective_config()
    ctx = json.loads(head["context"])
    assert ctx["regime"] == "RISK-ON" and ctx["data_as_of"] == "2026-10-06" and ctx["universe_total"] == 900
    assert ctx["summary"]["filtered_universe"] == 400 and ctx["passing"]["rule_of_40"] == 1
    assert ctx["edgar_versions"] == {str(CUR): 1} and ctx["edgar_extract_version"] == CUR


def test_a_second_complete_run_of_the_session_keeps_its_header_only(store):
    fu_history.record(_payload(COMPLETE_1, "AAA"), rows=_rows(_results("AAA")), run_id="r1")
    fu_history.record(_payload(COMPLETE_2, "AAA"), rows=_rows(_results("AAA", "BBB")), run_id="r2")
    assert [h["session_snapshot"] for h in _headers()] == [True, False]
    assert len(_score_rows("r1")) == 1 and _score_rows("r2") == [] and _gate_rows("r2") == []


def test_recording_the_same_run_id_twice_writes_nothing_more(store):
    fu_history.record(_payload(COMPLETE_1, "AAA"), rows=_rows(_results("AAA")), run_id="r1")
    fu_history.record(_payload(COMPLETE_1, "AAA"), rows=_rows(_results("AAA", "BBB")), run_id="r1")
    assert len(_headers()) == 1 and len(_score_rows("r1")) == 1 and len(_gate_rows("r1")) == 7


def test_an_intraday_run_is_not_a_snapshot(store):
    fu_history.record(_payload(INTRADAY, "AAA"), rows=_rows(_results("AAA")), run_id="early")
    (head,) = _headers()
    assert head["quality"] == "intraday" and head["session_snapshot"] is False and _score_rows("early") == []


def test_a_partial_universe_is_recorded_partial_and_keeps_no_rows(store):
    fu_history.record(_payload(COMPLETE_1, "AAA", universe_partial=True), rows=_rows(_results("AAA")), run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "partial" and "universe" in head["quality_note"] and _score_rows("r1") == []


def test_stale_spy_bars_make_the_run_partial(store):
    p = _payload(COMPLETE_1, "AAA", data_quality={"stale_ohlc": 0, "spy_stale": True, "dropped": {}})
    fu_history.record(p, rows=_rows(_results("AAA")), run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "partial" and "SPY" in head["quality_note"] and _score_rows("r1") == []


def test_a_fetch_with_no_data_still_gets_a_header_and_is_never_the_snapshot(store):
    empty = _payload(COMPLETE_1, no_data=True, no_data_message="No universe stored.", candidates=[])
    fu_history.record(empty, rows={}, run_id="empty")
    (head,) = _headers()
    assert head["quality"] == "partial" and head["session_snapshot"] is False
    assert head["quality_note"].startswith("rows not kept (0 given)") and "No universe stored." in head["quality_note"]
    fu_history.record(_payload(COMPLETE_2, "AAA"), rows=_rows(_results("AAA")), run_id="real")
    assert [h["session_snapshot"] for h in _headers()] == [False, True] and len(_score_rows("real")) == 1


def test_an_intraday_run_stays_intraday_even_when_its_bars_are_old(rawstore_tmp_db):
    """The bar anchor belongs to complete sessions only: an intraday run says 'intraday', not a bar problem."""
    ledger.take_notes()
    for t in ("SPY", "AAA"):
        _seed(t, cal.session_close(D).timestamp() - 3 * 3600)
    fu_history.record(_payload(INTRADAY, "AAA"), rows=_rows(_results("AAA")), run_id="early")
    (head,) = _headers()
    assert head["quality"] == "intraday" and "cached before" not in (head["quality_note"] or "")


def test_a_run_with_nothing_scored_is_partial(store):
    fu_history.record(_payload(COMPLETE_1), rows={}, run_id="none")
    (head,) = _headers()
    assert head["quality"] == "partial" and "scored" in head["quality_note"] and _score_rows("none") == []


def test_a_zero_candidate_run_with_scored_tickers_is_still_a_snapshot(store):
    """No pick passed a gate, yet every scanned ticker has a score row: that is a whole, correct result."""
    fu_history.record(_payload(COMPLETE_1), rows=_rows(_results("AAA", "BBB")), run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "complete" and head["session_snapshot"] is True and len(_score_rows("r1")) == 2


# the freeze's own anchor (B1 of the AG review)

def test_bars_cached_intraday_make_a_post_close_scan_partial_and_a_fresh_one_the_snapshot(rawstore_tmp_db):
    ledger.take_notes()
    stale_at = cal.session_close(D).timestamp() - 3 * 3600
    for t in ("SPY", "AAA"):
        _seed(t, stale_at)
    fu_history.record(_payload(COMPLETE_1, "AAA"), rows=_rows(_results("AAA")), run_id="stale")
    (head,) = _headers()
    assert head["quality"] == "partial" and head["session_snapshot"] is False
    assert "cached before the close + 30 min" in head["quality_note"] and _score_rows("stale") == []
    for t in ("SPY", "AAA"):
        _seed(t, _fresh())
    fu_history.record(_payload(COMPLETE_2, "AAA"), rows=_rows(_results("AAA")), run_id="fresh")
    assert [h["session_snapshot"] for h in _headers()] == [False, True] and len(_score_rows("fresh")) == 1


def test_spy_without_the_session_bar_makes_the_run_partial(rawstore_tmp_db):
    ledger.take_notes()
    _seed("SPY", _fresh(), last_day="2026-10-05")
    _seed("AAA", _fresh())
    fu_history.record(_payload(COMPLETE_1, "AAA"), rows=_rows(_results("AAA")), run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "partial" and "SPY's last bar is 2026-10-05" in head["quality_note"]


def test_a_shown_candidate_without_a_complete_bar_makes_the_run_partial(store):
    _seed("BBB", cal.session_close(D).timestamp() - 3 * 3600)
    fu_history.record(_payload(COMPLETE_1, "AAA", "BBB"), rows=_rows(_results("AAA", "BBB")), run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "partial" and "BBB" in head["quality_note"] and _score_rows("r1") == []


def test_a_naive_generated_at_is_read_as_utc(store):
    fu_history.record(_payload(COMPLETE_1, "AAA", generated_at="2026-10-06T22:00:00"),
                      rows=_rows(_results("AAA")), run_id="r1")
    (head,) = _headers()
    assert head["session_date"].isoformat() == "2026-10-06" and head["quality"] == "complete"


# EDGAR re-extraction (extract_version 2, #158): a snapshot must not mix quarter bases

def test_a_scored_ticker_on_an_older_extract_version_makes_the_run_partial(store):
    results = _results("AAA") + _results("BBB", version=1)
    fu_history.record(_payload(COMPLETE_1, "AAA"), rows=_rows(results), run_id="mixed")
    (head,) = _headers()
    assert head["quality"] == "partial" and head["session_snapshot"] is False and _score_rows("mixed") == []
    assert "extract_version" in head["quality_note"] and "1 of 2" in head["quality_note"]
    assert json.loads(head["context"])["edgar_versions"] == {str(CUR): 1, "1": 1}


def test_an_edgar_row_without_a_version_counts_as_older(store):
    fu_history.record(_payload(COMPLETE_1, "AAA"), rows=_rows(_results("AAA", version=None)), run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "partial" and "1 of 1" in head["quality_note"]


def test_after_the_older_rows_are_re_extracted_the_next_run_takes_the_snapshot(store):
    fu_history.record(_payload(COMPLETE_1, "AAA"), rows=_rows(_results("AAA", version=1)), run_id="old")
    fu_history.record(_payload(COMPLETE_2, "AAA"), rows=_rows(_results("AAA")), run_id="new")
    assert [h["session_snapshot"] for h in _headers()] == [False, True] and len(_score_rows("new")) == 1


def test_an_intraday_run_with_old_edgar_rows_says_partial_not_intraday(store):
    """The input problem is the stronger statement: the rows would be wrong in any session."""
    fu_history.record(_payload(INTRADAY, "AAA"), rows=_rows(_results("AAA", version=1)), run_id="early")
    (head,) = _headers()
    assert head["quality"] == "partial" and "extract_version" in head["quality_note"]


# ── the Fetch runner ─────────────────────────────────────────────────────────

@pytest.fixture
def runner(monkeypatch, store):
    from RevenueChaser.module_fundamentals import report as fu_report
    payload = _payload(COMPLETE_1, "AAA")

    def scan(uw=None, **kw):
        if not kw.get("no_fetch"):
            scores.persist(_results("AAA", "BBB"), sink=kw.get("rows_sink"))
        return payload

    monkeypatch.setattr(pipeline, "run_scan", scan)
    monkeypatch.setattr(pipeline, "save_payload", lambda p: "x")
    monkeypatch.setattr(fu_report, "render_fundamental_board", lambda p: ("<html/>", None))
    monkeypatch.setattr(runners, "_write_board", lambda *a, **k: None)
    monkeypatch.setattr(freeze, "freeze_payload", lambda *a, **k: None)
    return payload


def test_a_fetch_records_history_with_the_job_run_id(runner, monkeypatch):
    monkeypatch.setattr(runners, "_freeze_run_id", lambda: "job42")
    runners.run_fundamentals(None)
    assert [h["run_id"] for h in _headers()] == ["job42"]
    assert len(_score_rows("job42")) == 2 and len(_gate_rows("job42")) == 14


def test_a_refresh_never_records_history(runner):
    runners.run_fundamentals(None, no_fetch=True)
    assert not rawstore.history_db_path().exists()


def test_a_history_failure_is_a_job_note_and_the_fetch_still_freezes(runner, monkeypatch):
    seen = []
    monkeypatch.setattr(fu_history, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    monkeypatch.setattr(freeze, "freeze_payload", lambda *a, **k: seen.append("freeze"))
    runners.run_fundamentals(None)
    assert seen == ["freeze"]
    assert any("disk full" in n and "history" in n for n in ledger.take_notes())
    assert rawstore.count("fundamentals_scores") == 2                   # the latest table stayed written


def test_the_history_runs_before_the_freeze_with_the_same_run_id(runner, monkeypatch):
    order = []
    monkeypatch.setattr(fu_history, "record", lambda p, rows, run_id: order.append(("history", run_id)))
    monkeypatch.setattr(freeze, "freeze_payload", lambda p, run_id=None, **k: order.append(("freeze", run_id)))
    monkeypatch.setattr(runners, "_freeze_run_id", iter(["a", "b"]).__next__)             # a second call would give "b"
    runners.run_fundamentals(None)
    assert order == [("history", "a"), ("freeze", "a")]


def test_run_scan_gives_the_persisted_rows_to_the_sink(monkeypatch, store):
    """The real ``run_scan`` path: the sink holds the rows ``persist`` wrote, and a Refresh leaves it empty."""
    from RevenueChaser.core import screen_common

    def fake_screen(**kw):
        if not kw["no_fetch"]:
            kw["persist"](_results("AAA", "BBB"))
        return {"candidates": []}

    monkeypatch.setattr(screen_common, "run_screen", fake_screen)
    sink: dict = {}
    pipeline.run_scan(None, rows_sink=sink)
    assert [r["ticker"] for r in sink["scores"]] == ["AAA", "BBB"] and len(sink["gates"]) == 14
    refresh_sink: dict = {}
    pipeline.run_scan(None, no_fetch=True, rows_sink=refresh_sink)
    assert refresh_sink == {}
