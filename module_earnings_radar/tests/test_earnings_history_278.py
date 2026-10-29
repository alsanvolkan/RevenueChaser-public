"""Earnings Radar board -> result history (#278 G1 writer). Offline; the store is a temp DuckDB.

An Earnings Fetch records its run through ``core/history.record_run``: one ``result_runs`` header plus one
``earnings_cards_history`` row per scored card, in one transaction on history.duckdb. Earnings is a run board: every
run's rows are kept, unless the run's inputs are stale (B1): then the header is kept and says why, and no rows.
"""
from __future__ import annotations

import json
import zlib
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_earnings_radar.models import TradeCard
from RevenueChaser.module_earnings_radar import history as er_history
from RevenueChaser.module_earnings_radar.pipeline import nrc_from_card

BOARD, TABLE = "module_earnings_radar", "earnings_cards_history"
# 2026-10-06 is a Tuesday: 22:00 UTC = 18:00 ET (session complete); 14:00 UTC = 10:00 ET (intraday).
COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()
INTRADAY = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc).timestamp()
HEALTHY = {"calls": 400, "failed": 0, "failures": [], "auth_error": None, "breaker_open": False, "skipped_calls": 0}


def _card(ticker, **kw):
    c = TradeCard(ticker=ticker, earnings_date=date(2026, 10, 8), earnings_time="AMC", fiscal_quarter="Q3 2026",
                  final_action="BUY_CALL_SPREAD", direction_score=40.0, r2_score=30.0, r3_score=20.0,
                  r4_score=10.0, r7_score=5.0, r8_score=-3.0, volatility_edge_score=12.5, confidence_score=62.0,
                  expected_move_pct=7.5, data_completeness=0.8, pre_score=14.2, filter_status="passed",
                  trade_read="Lean long", attention_score=55.0, edge_ratio=1.4, key_thesis="strong beat history",
                  rotation_quadrant="LEADING",
                  rule_audit={"rule2_guidance": {"available": True, "contribution": 6.0},
                              "rule7_accounting": {"available": False, "contribution": None}},
                  raw_packet_json=json.dumps({"screener": {"close": 50}, "ohlc": [1, 2], "current_price": 50.0}))
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _record(cards, run_id="r1", run_at=COMPLETE, **kw):
    nrc = {c.ticker: nrc_from_card(c, {"AAA"}) for c in cards}
    kw.setdefault("uw_health", HEALTHY)
    kw.setdefault("filter_stats", {"total": len(cards), "passed": len(cards), "bypassed": 0, "filtered": 0,
                                   "by_reason": {}, "excluded": []})
    return er_history.record(cards, nrc, run_id=run_id, run_at=run_at, **kw)


def _headers():
    return rawstore.query("SELECT * FROM result_runs WHERE board = ? ORDER BY run_at, run_id", [BOARD], db="history")


def _unpack(cell):
    """A compressed JSON cell as the raw query returns it (zlib bytes) or already decoded."""
    if isinstance(cell, (bytes, bytearray)):
        return json.loads(zlib.decompress(cell))
    return cell if not isinstance(cell, str) else json.loads(cell)


def _rows(run_id):
    return rawstore.query(f"SELECT * FROM {TABLE} WHERE run_id = ? ORDER BY ticker", [run_id], db="history")


@pytest.fixture
def store(rawstore_tmp_db):
    ledger.take_notes()
    return rawstore_tmp_db


def test_a_run_records_a_header_and_one_row_per_card(store):
    _record([_card("AAA"), _card("BBB", final_action="WATCH_ONLY")])
    (head,) = _headers()
    assert head["quality"] == "complete" and head["error"] is None
    assert json.loads(head["row_counts"]) == {TABLE: 2}
    rows = _rows("r1")
    assert [r["ticker"] for r in rows] == ["AAA", "BBB"]
    a = rows[0]
    assert a["earnings_date"].isoformat() == "2026-10-08" and a["earnings_time"] == "AMC"
    assert a["final_action"] == "BUY_CALL_SPREAD" and a["direction_score"] == 40.0
    assert (a["r2_score"], a["r3_score"], a["r4_score"], a["r5_score"], a["r7_score"], a["r8_score"]) == \
        (30.0, 20.0, 10.0, 0.0, 5.0, -3.0)
    assert a["expected_move_pct"] == 7.5 and a["pre_score"] == 14.2 and a["filter_status"] == "passed"
    assert a["trade_read"] == "Lean long" and a["edge_ratio"] == 1.4 and a["macro_veto"] in (False, 0)


def test_nrc_columns_and_the_watchlist_flag_come_from_the_nrc_result(store):
    cards = [_card("AAA"), _card("BBB")]
    _record(cards)
    a, b = _rows("r1")
    want = nrc_from_card(cards[0], {"AAA"})
    assert a["nrc"] == want["nrc"] and a["nrc_tier"] == want["tier"] and a["nrc_growth"] == want["growth"]
    assert a["nrc_beat"] == want["beat"] and a["watchlist"] in (True, 1) and b["watchlist"] in (False, 0)


def test_card_inputs_and_slots_are_stored_and_the_raw_packet_is_not_inside_the_card(store):
    _record([_card("AAA")])
    (row,) = _rows("r1")
    card = _unpack(row["card"])
    assert card["ticker"] == "AAA" and card["key_thesis"] == "strong beat history"
    assert "raw_packet_json" not in card and card["earnings_date"] == "2026-10-08"
    inputs = _unpack(row["inputs"])
    assert inputs["screener"] == {"close": 50} and inputs["current_price"] == 50.0
    slots = json.loads(row["slots"])
    assert slots["rules"] == {"rule2_guidance": True, "rule7_accounting": False}
    assert slots["contributions"] == {"rule2_guidance": 6.0}
    assert slots["inputs"] == ["current_price", "ohlc", "screener"]
    assert set(slots["nrc_live"]) <= {"growth", "acceleration", "quality", "beat"} and "growth" in slots["nrc_live"]


def test_a_card_without_a_packet_stores_null_inputs(store):
    _record([_card("AAA", raw_packet_json="")])
    (row,) = _rows("r1")
    assert row["inputs"] is None


def test_the_header_carries_model_version_params_funnel_and_context(store):
    stats = {"total": 3, "passed": 1, "bypassed": 0, "filtered": 2, "by_reason": {"OI": 2}, "excluded": []}
    _record([_card("AAA")], filter_stats=stats, span=(date(2026, 10, 8), date(2026, 10, 9)), n_events=3)
    (head,) = _headers()
    assert head["model_version"] == er_history.model_version() and head["model_version"].startswith("3+")
    assert json.loads(head["funnel"]) == stats
    assert json.loads(head["params"])["MAX_DEEP_CHECKS_PER_DAY"] == 500
    ctx = json.loads(head["context"])
    assert ctx["span"] == ["2026-10-08", "2026-10-09"] and ctx["events"] == 3 and ctx["cards"] == 1


def test_model_version_changes_with_a_tunable(store, monkeypatch):
    before = er_history.model_version()
    from RevenueChaser.core import config
    monkeypatch.setitem(config.TUNABLES, "R8_RETAIL_FADE", 0.9)
    assert er_history.model_version() != before


def test_every_run_keeps_its_rows_two_runs_of_a_session_both_have_them(store):
    _record([_card("AAA")], run_id="r1")
    _record([_card("AAA"), _card("BBB")], run_id="r2", run_at=COMPLETE + 3600)
    assert len(_rows("r1")) == 1 and len(_rows("r2")) == 2


def test_an_intraday_run_keeps_its_rows_and_says_intraday(store):
    _record([_card("AAA")], run_id="early", run_at=INTRADAY)
    (head,) = _headers()
    assert head["quality"] == "intraday" and len(_rows("early")) == 1


def test_recording_the_same_run_id_twice_writes_nothing_more(store):
    _record([_card("AAA")])
    _record([_card("AAA"), _card("BBB")])
    assert len(_headers()) == 1 and len(_rows("r1")) == 1


# ── degraded inputs: the run is kept, partial, with its rows (design C rule E) ──

def _health(**kw):
    return {**HEALTHY, **kw}


def test_a_rejected_uw_key_is_partial_with_the_rows_kept(store):
    _record([_card("AAA")], uw_health=_health(auth_error={"status": 401}, failed=400))
    (head,) = _headers()
    assert head["quality"] == "partial" and "rejected" in head["quality_note"] and len(_rows("r1")) == 1
    assert not head["quality_note"].startswith("rows not kept") and json.loads(head["row_counts"]) == {TABLE: 1}


def test_many_failed_uw_calls_are_partial_with_the_rows_kept(store):
    _record([_card("AAA")], uw_health=_health(failed=300))
    (head,) = _headers()
    assert head["quality"] == "partial" and "300 of 400" in head["quality_note"] and len(_rows("r1")) == 1


def test_an_open_breaker_is_partial_with_the_rows_kept(store):
    _record([_card("AAA")], uw_health=_health(breaker_open=True, skipped_calls=30, failed=30))
    (head,) = _headers()
    assert head["quality"] == "partial" and "stopped" in head["quality_note"] and len(_rows("r1")) == 1


def test_a_few_failed_calls_make_the_run_partial_but_keep_the_rows(store):
    _record([_card("AAA")], uw_health=_health(failed=3))
    (head,) = _headers()
    assert head["quality"] == "partial" and "3 of 400" in head["quality_note"] and len(_rows("r1")) == 1
    assert json.loads(head["row_counts"]) == {TABLE: 1}


def test_a_failed_rotation_map_makes_the_run_partial_but_keeps_the_rows(store):
    _record([_card("AAA")], rotation_failed=True)
    (head,) = _headers()
    assert head["quality"] == "partial" and "rotation" in head["quality_note"] and len(_rows("r1")) == 1


def _session_events(n, day=date(2026, 10, 8), when="AMC", source="UW"):
    return [{"ticker": f"T{i}", "earnings_date": day, "earnings_time": when, "source": source} for i in range(n)]


def test_a_calendar_session_at_the_500_row_limit_is_partial_but_keeps_the_rows(store):
    _record([_card("AAA")], events=_session_events(500))
    (head,) = _headers()
    assert head["quality"] == "partial" and "2026-10-08 AMC" in head["quality_note"] and len(_rows("r1")) == 1


def test_a_heavy_session_is_not_a_cap_g2_14_measured_157_and_161_rows(store):
    _record([_card("AAA")], run_id="a", events=_session_events(157, date(2026, 7, 29)))
    _record([_card("AAA")], run_id="b", events=_session_events(161, date(2026, 7, 30), "BMO"), run_at=COMPLETE + 60)
    assert [h["quality"] for h in _headers()] == ["complete", "complete"]


def test_499_rows_or_600_watchlist_additions_are_not_a_cap(store):
    _record([_card("AAA")], events=_session_events(499) + _session_events(600, source="screener"))
    assert _headers()[0]["quality"] == "complete"


def test_all_reasons_are_listed_together(store):
    _record([_card("AAA")], uw_health=_health(failed=41), rotation_failed=True)
    (head,) = _headers()
    assert "41 of 400" in head["quality_note"] and "rotation" in head["quality_note"] and len(_rows("r1")) == 1


def test_a_run_without_cards_still_gets_a_header(store):
    _record([], reason="No earnings events scheduled for 2026-10-04.")
    (head,) = _headers()
    assert head["quality"] == "partial" and "No earnings events" in head["quality_note"]
    assert json.loads(head["row_counts"]) == {TABLE: 0}


def test_a_run_without_uw_health_is_recorded_complete(store):
    _record([_card("AAA")], uw_health=None)                              # store-only doubles have no health()
    (head,) = _headers()
    assert head["quality"] == "complete" and len(_rows("r1")) == 1


def test_a_card_without_a_ticker_is_left_out(store):
    _record([_card("AAA"), _card("")])
    assert [r["ticker"] for r in _rows("r1")] == ["AAA"]


def test_a_repeated_ticker_keeps_its_first_card_the_key_is_run_id_and_ticker(store):
    _record([_card("AAA", final_action="BUY"), _card("AAA", final_action="SELL")])
    (row,) = _rows("r1")
    assert row["final_action"] == "BUY"


# ── a card from the real pipeline (not a hand-filled one) ────────────────────

def _real_card(monkeypatch, **uw):
    """A card scored by the real pipeline; its options inputs are a list set (G9: the options store)."""
    from RevenueChaser.module_earnings_radar import store_reads
    from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW, _make_pipeline

    term = [{"expiry": "2026-10-16", "volatility": "0.9", "dte": 8},
            {"expiry": "2026-11-20", "volatility": "0.6", "dte": 43}]
    strikes = [{"strike": "150", "call_gamma_oi": "10", "put_gamma_oi": "-5", "price": "150.0", "time": "t",
                "ticker": "AAPL", "expiry": "x", "call_gamma_ask": "1"}]
    monkeypatch.setattr(store_reads, "card_inputs", lambda ticker, report_date, listed: {
        "set": "list", "session": None, "gex": {}, "strikes": strikes, "term": term, "prints": [], "volume": {}})

    class UW(_FakeUW):
        def stock_earnings(self, ticker):
            return [{"actual_eps": "1.1", "estimate": "1.0", "report_date": "2026-07-30"}]

    scr = {"AAPL": {"close": "150.0", "call_volume": "5000", "put_volume": "4000", "total_open_interest": "90000"}}
    cal = {"AAPL": {"earnings_date": "2026-10-08", "full_name": "Apple", "sector": "Technology",
                    "expected_move_perc": 0.05}}
    bars = [{"close": str(100 + i), "open": "1", "high": "1", "low": "1", "volume": 1, "start_time": f"d{i}"}
            for i in range(270)]
    p = _make_pipeline(UW(etf_ohlc=bars, **uw), scr, cal)
    p._shorts_cache = {"AAPL": {"si_pct": 4.2}}
    return p._score("AAPL")


def test_a_real_pipeline_card_stores_its_live_rules_in_slots(store, monkeypatch):
    card = _real_card(monkeypatch)
    assert card.rule_audit
    _record([card])
    (row,) = _rows("r1")
    slots = json.loads(row["slots"])
    assert slots["rules"] == {k: bool(v["available"]) for k, v in card.rule_audit.items()} and slots["rules"]
    assert slots["contributions"] == {k: v["contribution"] for k, v in card.rule_audit.items()
                                      if v["contribution"] is not None}


def test_a_real_pipeline_card_stores_every_uw_answer_it_was_scored_on(store, monkeypatch):
    card = _real_card(monkeypatch)
    _record([card])
    (row,) = _rows("r1")
    inputs = _unpack(row["inputs"])
    from RevenueChaser.module_earnings_radar.history import packet_ohlc, packet_r2_series
    assert inputs["term_structure"][0]["dte"] == 8                       # R4's slope input
    assert inputs["spot_exposures"] == [{"strike": "150", "call_gamma_oi": "10", "put_gamma_oi": "-5"}]  # R4's fields only
    assert packet_r2_series(inputs)[0]["actual_eps"] == "1.1"           # R2's EPS series
    assert inputs["short_interest"] == {"si_pct": 4.2}                  # R5's short row
    # E4: the 60-bar slice is rebuilt from the year, not stored twice.
    assert len(inputs["ohlc_year"]) == 270 and len(packet_ohlc(inputs)) == 60 and "ohlc" not in inputs
    assert set(json.loads(row["slots"])["inputs"]) >= {"term_structure", "spot_exposures", "r2_series",
                                                        "short_interest", "ohlc_year"}


def test_a_ticker_with_no_term_or_exposure_answer_stores_them_as_null(store):
    from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW, _make_pipeline
    scr = {"AAPL": {"close": "150.0"}}
    cal = {"AAPL": {"earnings_date": "2026-10-08", "full_name": "Apple", "sector": "Technology",
                    "expected_move_perc": 0.05}}
    card = _make_pipeline(_FakeUW(), scr, cal)._score("AAPL")
    _record([card])
    inputs = _unpack(_rows("r1")[0]["inputs"])
    assert inputs["term_structure"] is None and inputs["spot_exposures"] is None


def test_a_strike_row_is_cut_to_the_13_fields_the_gex_profile_reads():
    from RevenueChaser.module_earnings_radar.pipeline import _strike_fields
    full = {"strike": "1", **{f"{s}_{g}{b}": "0" for g in ("gamma", "charm", "vanna") for b in ("_oi", "_vol")
                              for s in ("call", "put")}, "extra": "x", "call_gamma_ask": "1"}
    (row,) = _strike_fields({"data": [full, "junk"]})
    assert len(row) == 13 and "extra" not in row and "call_gamma_ask" not in row and row["strike"] == "1"
    assert _strike_fields(None) == [] and _strike_fields([{"strike": "2"}]) == [{"strike": "2"}]


def test_the_input_sources_are_kept_in_the_header_context(store):
    """E5 (#372): what the run read from the store vs asked the provider for, JSON-safe (review S8)."""
    sources = {"calendar": {"stored": 4, "live": 0, "session": "2026-10-08"}, "dgs10": "stored",
               "rate_events": "none", "ohlc": {"stored": 1, "live": 0}}
    _record([_card("AAA")], input_sources=sources)
    (head,) = _headers()
    assert json.loads(head["context"])["inputs"] == sources


def test_a_run_without_input_sources_has_no_inputs_key(store):
    _record([_card("AAA")])
    (head,) = _headers()
    assert "inputs" not in json.loads(head["context"])
