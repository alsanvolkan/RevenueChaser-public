"""Swing board -> result history (#278 G1, second writer after AG #332). Offline; the store is a temp DuckDB.

A Swing Fetch records its run through ``core/history.record_run``: one ``result_runs`` header plus the rows it gave
``swing_scores`` and ``swing_gates`` (``swing_scores_history`` / ``swing_gates_history``, mirrors), in one transaction
on history.duckdb.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.app import runners
from RevenueChaser.core import calendar as cal
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.core.tests.d01_seed import seed_d01
from RevenueChaser.module_swing_potential import freeze, pipeline, scores
from RevenueChaser.module_swing_potential import history as sw_history

BOARD, SCORES, GATES = "module_swing_potential", "swing_scores_history", "swing_gates_history"
# 2026-10-06 is a Tuesday: 22:00 UTC = 18:00 ET (session complete); 14:00 UTC = 10:00 ET (intraday).
D = date(2026, 10, 6)
COMPLETE_1 = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc)
COMPLETE_2 = datetime(2026, 10, 6, 23, 0, tzinfo=timezone.utc)
INTRADAY = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)


def _ind(price=100.0, rs=0.30):
    return {
        "price": price, "sma50": 98.0, "sma150": 95.0, "sma200": 90.0,
        "sma200_prev_1m": 88.0, "sma150_prev_1m": 93.0, "ema10": 99.0,
        "ema20": 97.0, "ema10_prev": 98.0, "ema20_prev": 96.0,
        "above_52w_low_pct": 1.5, "within_52w_high_pct": 0.99, "vol_ratio": 1.6,
        "rs_vs_spy_3m": 0.05, "rs_vs_spy_12m": rs, "mom_12_1": 0.3,
        "consistency_6m": 0.8, "move_max_1_3m": 0.4, "adr_pct": 0.04, "ret_3m": 0.10,
        "atr14": 3.0, "change_pct": 0.01, "change_1w": 0.02, "change_1m": 0.05,
    }


def _results(*tickers):
    return [{"ticker": t, "ind": _ind(price=100.0 - i, rs=0.30 - i * 0.05)} for i, t in enumerate(tickers)]


def _rows(results):
    """What ``scores.persist`` hands the latest tables for these results."""
    sink: dict = {}
    scores.persist(results, sink=sink)
    return sink


def _payload(when=COMPLETE_1, *tickers, **kw):
    p = {"generated_at": when.isoformat(), "data_as_of": "2026-10-06", "regime": "RISK-ON", "universe_total": 900,
         "universe_generated_at": when.isoformat(), "universe_refreshed_by": "Swing Potential",
         "universe_partial": False, "passing": {"minervini": 1, "weinstein": 1, "qullamaggie": 1},
         "avg_quality": 55.0, "is_fixture": False, "no_data": False,
         "data_quality": {"stale_ohlc": 0, "spy_stale": False, "dropped": {}},
         "summary": {"filtered_universe": 400, "hygiene": 100, "data_short": 5, "top_n": len(tickers)},
         "candidates": [{"ticker": t, "multiplier": 3, "quality": 60.0} for t in tickers]}
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
    assert out == {"scores": 2, "gates": 42}
    assert [r["ticker"] for r in sink["scores"]] == ["AAA", "BBB"] and len(sink["gates"]) == 42
    latest = {r["ticker"]: r for r in rawstore.query("SELECT * FROM swing_scores")}
    assert {r["ticker"]: r for r in sink["scores"]} == latest


def test_persist_without_a_sink_is_unchanged(store):
    assert scores.persist(_results("AAA")) == {"scores": 1, "gates": 21}


# ── the writer ───────────────────────────────────────────────────────────────

def test_a_run_records_a_header_and_the_rows_the_latest_tables_got(store):
    rows = _rows(_results("AAA", "BBB"))
    sw_history.record(_payload(COMPLETE_1, "AAA"), rows=rows, run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "complete" and head["session_snapshot"] is True and head["error"] is None
    assert json.loads(head["row_counts"]) == {SCORES: 2, GATES: 42}
    got = _score_rows("r1")
    assert [r["ticker"] for r in got] == ["AAA", "BBB"]
    latest = {r["ticker"]: r for r in rawstore.query("SELECT * FROM swing_scores")}
    for r in got:
        assert {k: v for k, v in r.items() if k != "run_id"} == latest[r["ticker"]]
    latest_gates = rawstore.query("SELECT * FROM swing_gates ORDER BY ticker, gate, rule_id")
    assert [{k: v for k, v in r.items() if k != "run_id"} for r in _gate_rows("r1")] == latest_gates


def test_the_header_carries_model_version_params_and_context(store):
    sw_history.record(_payload(COMPLETE_1, "AAA"), rows=_rows(_results("AAA")), run_id="r1")
    (head,) = _headers()
    assert head["model_version"] == freeze.model_version()
    assert json.loads(head["params"]) == freeze.effective_config()
    ctx = json.loads(head["context"])
    assert ctx["regime"] == "RISK-ON" and ctx["data_as_of"] == "2026-10-06" and ctx["universe_total"] == 900
    assert ctx["summary"]["filtered_universe"] == 400 and ctx["passing"]["minervini"] == 1
    assert ctx["data_quality"] == {"stale_ohlc": 0, "spy_stale": False, "dropped": {}}      # the stale count lives here


def test_a_second_complete_run_of_the_session_keeps_its_header_only(store):
    sw_history.record(_payload(COMPLETE_1, "AAA"), rows=_rows(_results("AAA")), run_id="r1")
    sw_history.record(_payload(COMPLETE_2, "AAA"), rows=_rows(_results("AAA", "BBB")), run_id="r2")
    assert [h["session_snapshot"] for h in _headers()] == [True, False]
    assert len(_score_rows("r1")) == 1 and _score_rows("r2") == [] and _gate_rows("r2") == []


def test_recording_the_same_run_id_twice_writes_nothing_more(store):
    sw_history.record(_payload(COMPLETE_1, "AAA"), rows=_rows(_results("AAA")), run_id="r1")
    sw_history.record(_payload(COMPLETE_1, "AAA"), rows=_rows(_results("AAA", "BBB")), run_id="r1")
    assert len(_headers()) == 1 and len(_score_rows("r1")) == 1 and len(_gate_rows("r1")) == 21


def test_an_intraday_run_is_not_a_snapshot(store):
    sw_history.record(_payload(INTRADAY, "AAA"), rows=_rows(_results("AAA")), run_id="early")
    (head,) = _headers()
    assert head["quality"] == "intraday" and head["session_snapshot"] is False and _score_rows("early") == []


def test_a_partial_universe_is_recorded_partial_and_keeps_no_rows(store):
    sw_history.record(_payload(COMPLETE_1, "AAA", universe_partial=True), rows=_rows(_results("AAA")), run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "partial" and "universe" in head["quality_note"] and _score_rows("r1") == []


def test_stale_spy_bars_make_the_run_partial(store):
    p = _payload(COMPLETE_1, "AAA", data_quality={"stale_ohlc": 0, "spy_stale": True, "dropped": {}})
    sw_history.record(p, rows=_rows(_results("AAA")), run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "partial" and "SPY" in head["quality_note"] and _score_rows("r1") == []


def test_a_fetch_with_no_data_still_gets_a_header_and_is_never_the_snapshot(store):
    empty = _payload(COMPLETE_1, no_data=True, no_data_message="No universe stored.", candidates=[])
    sw_history.record(empty, rows={}, run_id="empty")
    (head,) = _headers()
    assert head["quality"] == "partial" and head["session_snapshot"] is False
    assert head["quality_note"].startswith("rows not kept (0 given)") and "No universe stored." in head["quality_note"]
    sw_history.record(_payload(COMPLETE_2, "AAA"), rows=_rows(_results("AAA")), run_id="real")
    assert [h["session_snapshot"] for h in _headers()] == [False, True] and len(_score_rows("real")) == 1


def test_an_intraday_run_stays_intraday_even_when_its_bars_are_old(rawstore_tmp_db):
    """The bar anchor belongs to complete sessions only: an intraday run says 'intraday', not a bar problem."""
    ledger.take_notes()
    for t in ("SPY", "AAA"):
        _seed(t, cal.session_close(D).timestamp() - 3 * 3600)
    sw_history.record(_payload(INTRADAY, "AAA"), rows=_rows(_results("AAA")), run_id="early")
    (head,) = _headers()
    assert head["quality"] == "intraday" and "cached before" not in (head["quality_note"] or "")


def test_a_run_with_nothing_scored_is_partial(store):
    sw_history.record(_payload(COMPLETE_1), rows={}, run_id="none")
    (head,) = _headers()
    assert head["quality"] == "partial" and "scored" in head["quality_note"] and _score_rows("none") == []


def test_a_zero_candidate_run_with_scored_tickers_is_still_a_snapshot(store):
    """No pick passed a gate, yet every scanned ticker has a score row: that is a whole, correct result."""
    sw_history.record(_payload(COMPLETE_1), rows=_rows(_results("AAA", "BBB")), run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "complete" and head["session_snapshot"] is True and len(_score_rows("r1")) == 2


# the freeze's own anchor (B1 of the AG review): a post-close scan on bars cached before close + 30 min must not
# become the session's permanent snapshot

def test_bars_cached_intraday_make_a_post_close_scan_partial_and_a_fresh_one_the_snapshot(rawstore_tmp_db):
    ledger.take_notes()
    stale_at = cal.session_close(D).timestamp() - 3 * 3600
    for t in ("SPY", "AAA"):
        _seed(t, stale_at)
    sw_history.record(_payload(COMPLETE_1, "AAA"), rows=_rows(_results("AAA")), run_id="stale")
    (head,) = _headers()
    assert head["quality"] == "partial" and head["session_snapshot"] is False
    assert "cached before the close + 30 min" in head["quality_note"] and _score_rows("stale") == []
    for t in ("SPY", "AAA"):
        _seed(t, _fresh())                                   # a later Fetch refetches the bars
    sw_history.record(_payload(COMPLETE_2, "AAA"), rows=_rows(_results("AAA")), run_id="fresh")
    assert [h["session_snapshot"] for h in _headers()] == [False, True] and len(_score_rows("fresh")) == 1


def test_spy_without_the_session_bar_makes_the_run_partial(rawstore_tmp_db):
    ledger.take_notes()
    _seed("SPY", _fresh(), last_day="2026-10-05")
    _seed("AAA", _fresh())
    sw_history.record(_payload(COMPLETE_1, "AAA"), rows=_rows(_results("AAA")), run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "partial" and "SPY's last bar is 2026-10-05" in head["quality_note"]


def test_a_shown_candidate_without_a_complete_bar_makes_the_run_partial(store):
    _seed("BBB", cal.session_close(D).timestamp() - 3 * 3600)               # BBB cached intraday
    sw_history.record(_payload(COMPLETE_1, "AAA", "BBB"), rows=_rows(_results("AAA", "BBB")), run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "partial" and "BBB" in head["quality_note"] and _score_rows("r1") == []


def test_a_naive_generated_at_is_read_as_utc(store):
    sw_history.record(_payload(COMPLETE_1, "AAA", generated_at="2026-10-06T22:00:00"),
                      rows=_rows(_results("AAA")), run_id="r1")
    (head,) = _headers()
    assert head["session_date"].isoformat() == "2026-10-06" and head["quality"] == "complete"


# ── the Fetch runner ─────────────────────────────────────────────────────────

@pytest.fixture
def runner(monkeypatch, store):
    from RevenueChaser.module_swing_potential import report as sw_report
    payload = _payload(COMPLETE_1, "AAA")

    def scan(uw=None, **kw):
        if not kw.get("no_fetch"):
            scores.persist(_results("AAA", "BBB"), sink=kw.get("rows_sink"))
        return payload

    monkeypatch.setattr(pipeline, "run_scan", scan)
    monkeypatch.setattr(pipeline, "save_payload", lambda p: "x")
    monkeypatch.setattr(sw_report, "render_swing_board", lambda p: ("<html/>", None))
    monkeypatch.setattr(runners, "_write_board", lambda *a, **k: None)
    monkeypatch.setattr(freeze, "freeze_payload", lambda *a, **k: None)
    return payload


def test_a_fetch_records_history_with_the_job_run_id(runner, monkeypatch):
    monkeypatch.setattr(runners, "_freeze_run_id", lambda: "job42")
    runners.run_swing(None)
    assert [h["run_id"] for h in _headers()] == ["job42"]
    assert len(_score_rows("job42")) == 2 and len(_gate_rows("job42")) == 42


def test_a_refresh_never_records_history(runner):
    runners.run_swing(None, no_fetch=True)
    assert not rawstore.history_db_path().exists()


def test_a_history_failure_is_a_job_note_and_the_fetch_still_freezes(runner, monkeypatch):
    seen = []
    monkeypatch.setattr(sw_history, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    monkeypatch.setattr(freeze, "freeze_payload", lambda *a, **k: seen.append("freeze"))
    runners.run_swing(None)
    assert seen == ["freeze"]
    assert any("disk full" in n and "history" in n for n in ledger.take_notes())
    assert rawstore.count("swing_scores") == 2                          # the latest table stayed written


def test_the_history_runs_before_the_freeze_with_the_same_run_id(runner, monkeypatch):
    order = []
    monkeypatch.setattr(sw_history, "record", lambda p, rows, run_id: order.append(("history", run_id)))
    monkeypatch.setattr(freeze, "freeze_payload", lambda p, run_id=None, **k: order.append(("freeze", run_id)))
    monkeypatch.setattr(runners, "_freeze_run_id", iter(["a", "b"]).__next__)             # a second call would give "b"
    runners.run_swing(None)
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
    assert [r["ticker"] for r in sink["scores"]] == ["AAA", "BBB"] and len(sink["gates"]) == 42
    refresh_sink: dict = {}
    pipeline.run_scan(None, no_fetch=True, rows_sink=refresh_sink)
    assert refresh_sink == {}


def test_run_scan_without_a_sink_calls_persist_with_the_results_only(monkeypatch, store):
    """A stand-in of ``scores.persist`` that takes one argument keeps working when no sink is asked for."""
    from RevenueChaser.core import screen_common
    seen = []
    monkeypatch.setattr(scores, "persist", lambda r: seen.append(len(r)))
    monkeypatch.setattr(screen_common, "run_screen",
                        lambda **kw: kw["persist"](_results("AAA", "BBB")) or {"candidates": []})
    pipeline.run_scan(None)
    assert seen == [2]
