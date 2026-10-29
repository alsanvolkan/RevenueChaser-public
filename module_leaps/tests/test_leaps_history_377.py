"""LEAPS board -> result history (#377, A3). Offline; the store is a temp DuckDB.

A LEAPS Fetch records every watchlist footprint through ``core/history.record_run``: one ``result_runs`` header plus
one ``leaps_footprint_history`` row per watchlist ticker, whatever its label, with ``fetch_status`` and the inputs it
was classified from. LEAPS is a session board: the first complete, whole run of a session keeps its rows; a later,
intraday or partial run keeps its header only. Since G9 the footprints come from the options store
(``options_store.py``): a pinned name on the option watch list with no stored chain is ``missing``.
"""
from __future__ import annotations

import json
import zlib
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import config, ledger, rawstore
from RevenueChaser.module_leaps import engine, history as leaps_history
from RevenueChaser.module_leaps.scan import scan_leaps
from RevenueChaser.module_leaps.tests.options_store import pin, store_footprint
from RevenueChaser.module_leaps.tests.test_leaps_l3 import seed_session
from RevenueChaser.module_sec_13f.tests.ownership_store import _write_spine

BOARD, TABLE = "module_leaps", "leaps_footprint_history"
TODAY = date(2026, 10, 6)
# 2026-10-06 is a Tuesday: 22:00 UTC = 18:00 ET (session complete); 14:00 UTC = 10:00 ET (intraday).
COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()
INTRADAY = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc).timestamp()
FAR = "2027-12-17"                       # > 270 days after TODAY


@pytest.fixture
def store(rawstore_tmp_db):
    ledger.take_notes()
    pin("AAA", "BBB")
    config.MARKET_DIR.mkdir(parents=True, exist_ok=True)
    _write_spine(config.MARKET_DIR, {"AAA": ("AAA Inc", "Technology"), "BBB": ("BBB Inc", "Energy")})
    return rawstore_tmp_db


def _scan(oi: dict, **kw):
    """Store each ticker's far-dated ``(call_oi, put_oi)`` for TODAY's session, then run the Fetch's scan."""
    for ticker, (call, put) in oi.items():
        store_footprint(ticker, TODAY, {FAR: (call, put)})
    footprints: list = []
    watched, unwatched = scan_leaps(None, today=TODAY, footprints=footprints, **kw)
    return footprints, watched, unwatched


def _record(footprints, run_id="r1", run_at=COMPLETE, **kw):
    return leaps_history.record(footprints, run_id=run_id, run_at=run_at, **kw)


def _headers():
    return rawstore.query("SELECT * FROM result_runs WHERE board = ? ORDER BY run_at, run_id", [BOARD], db="history")


def _rows(run_id):
    return rawstore.query(f"SELECT * FROM {TABLE} WHERE run_id = ? ORDER BY ticker", [run_id], db="history")


def _unpack(cell):
    if isinstance(cell, (bytes, bytearray)):
        return json.loads(zlib.decompress(cell))
    return cell if not isinstance(cell, str) else json.loads(cell)


# ── the scan's footprint sink ──

def test_the_scan_hands_back_every_watchlist_footprint_whatever_its_label(store):
    # Seed yesterday's snapshot for AAA so its OI grew: Accumulating. BBB has no baseline: Quiet.
    seed_session(date(2026, 10, 5), {"AAA": [{"expiry": FAR, "call_oi": 500, "put_oi": 0}]})
    footprints, watched, _ = _scan({"AAA": (1000, 200), "BBB": (300, 300)})
    assert [w["ticker"] for w in watched] == ["AAA", "BBB"]         # every pinned name, whatever its label (#377 L2)
    assert [f["ticker"] for f in footprints] == ["AAA", "BBB"]        # the history gets both
    assert {f["snapshot"]["label"] for f in footprints} == {"Accumulating", "Quiet"}
    assert all(f["fetch_status"] == "ok" for f in footprints)


def test_the_scan_names_the_inputs_a_footprint_was_classified_from(store):
    seed_session(date(2026, 10, 5), {"AAA": [{"expiry": FAR, "call_oi": 500, "put_oi": 0}]})
    footprints, _, _ = _scan({"AAA": (1000, 200), "BBB": (300, 300)})
    inputs = footprints[0]["inputs"]
    assert inputs["oi_per_expiry"] == [{"expiry": FAR, "call_oi": 1000, "put_oi": 200}]
    assert inputs["iv_rank"] == [{"date": TODAY.isoformat(), "iv_rank_1y": 55.0}] and inputs["flow_per_expiry"] == []
    assert inputs["session"] == TODAY.isoformat()
    assert inputs["baseline_1d"]["far_call_oi"] == 500 and inputs["flow_candidate"] is None


def test_a_pinned_list_name_with_no_stored_chain_is_missing(store):
    footprints, _, _ = _scan({"AAA": (1000, 200)})
    status = {f["ticker"]: f["fetch_status"] for f in footprints}
    assert status == {"AAA": "ok", "BBB": "missing"}


def test_a_pinned_name_off_the_option_list_is_not_on_list(store, monkeypatch):
    from RevenueChaser.rcdata import ops
    ops.watch_remove("option", "BBB", by="test")
    footprints, _, _ = _scan({"AAA": (1000, 200), "BBB": (300, 300)})
    assert {f["ticker"]: f["fetch_status"] for f in footprints} == {"AAA": "ok", "BBB": "not_on_list"}


def test_a_scan_without_the_sink_is_unchanged(store):
    store_footprint("AAA", TODAY, {FAR: (1000, 200)})
    watched, unwatched = scan_leaps(None, today=TODAY)
    assert [w["ticker"] for w in watched] == ["AAA", "BBB"] and unwatched == []   # the sink is optional (#377 L2)


# ── the record ──

def test_a_run_records_a_header_and_one_row_per_watchlist_ticker(store):
    seed_session(date(2026, 10, 5), {"AAA": [{"expiry": FAR, "call_oi": 500, "put_oi": 0}]})
    footprints, _, _ = _scan({"AAA": (1000, 200), "BBB": (300, 300)})
    assert _record(footprints) == {TABLE: 2}
    (head,) = [h for h in _headers() if h["run_id"] == "r1"]      # the seeded session has its own header
    assert head["quality"] == "complete" and head["session_snapshot"] is True and head["quality_note"] is None
    a, b = _rows("r1")
    assert (a["ticker"], a["name"], a["label"], a["fetch_status"]) == ("AAA", "AAA Inc", "Accumulating", "ok")
    assert a["far_call_oi"] == 1000 and a["far_put_oi"] == 200 and a["far_oi_growth_1d_pct"] == 100.0
    assert a["as_of"] == TODAY and a["iv_rank"] == 55.0 and a["score"] == engine._score(footprints[0]["snapshot"])
    assert a["frequency"] == 0 and a["concentration_mult"] == 1.0 and a["flow_score"] is None
    assert b["label"] == "Quiet"
    assert _unpack(a["inputs"])["baseline_1d"]["far_call_oi"] == 500


def test_the_header_carries_model_version_params_and_context(store):
    footprints, _, _ = _scan({"AAA": (1000, 200), "BBB": (300, 300)})
    _record(footprints, shown={"watched": 0, "unwatched": 0}, warnings=["The print scan hit its cap"])
    (head,) = _headers()
    assert head["model_version"] == leaps_history.model_version() and head["model_version"].startswith("3+")
    assert json.loads(head["params"])["CALL_SHARE_MIN"] == engine.CALL_SHARE_MIN
    assert json.loads(head["params"])["ASK_SHARE_MIN"] == engine.ASK_SHARE_MIN
    assert json.loads(head["context"]) == {"watchlist": 2, "shown": {"watched": 0, "unwatched": 0}}
    assert json.loads(head["warnings"]) == ["The print scan hit its cap"]
    assert head["quality"] == "complete"                    # a capped print scan is a warning, not a reason


@pytest.mark.parametrize("name, value", [("FAR_DTE", 365), ("CALL_SHARE_MIN", 0.7), ("MIN_PREMIUM", 100_000),
                                         ("ITM_DELTA", (0.5, 0.9)), ("NEAR_EVENT_DAYS", 3), ("ASK_SHARE_MIN", 0.8)])
def test_model_version_changes_with_a_threshold(monkeypatch, name, value):
    before = leaps_history.model_version()
    monkeypatch.setattr(engine, name, value)
    assert leaps_history.model_version() != before


def test_a_second_complete_run_of_the_session_keeps_its_header_only(store):
    footprints, _, _ = _scan({"AAA": (1000, 200), "BBB": (300, 300)})
    _record(footprints, run_id="r1")
    assert _record(footprints, run_id="r2", run_at=COMPLETE + 3600) == {TABLE: 0}
    first, second = _headers()
    assert first["session_snapshot"] is True and second["session_snapshot"] is False
    assert "already recorded by run r1" in second["quality_note"]
    assert len(_rows("r1")) == 2 and _rows("r2") == []


def test_an_intraday_run_keeps_its_header_only(store):
    footprints, _, _ = _scan({"AAA": (1000, 200), "BBB": (300, 300)})
    _record(footprints, run_id="early", run_at=INTRADAY)
    (head,) = _headers()
    assert head["quality"] == "intraday" and _rows("early") == []


def test_a_missing_footprint_makes_the_run_partial_and_keeps_no_rows(store):
    footprints, _, _ = _scan({"AAA": (1000, 200)})
    _record(footprints)
    (head,) = _headers()
    assert head["quality"] == "partial" and head["session_snapshot"] is False and _rows("r1") == []
    assert "1 of 2 footprints have no stored option chain: BBB" in head["quality_note"]


def test_a_name_off_the_option_list_keeps_the_run_whole(store):
    from RevenueChaser.rcdata import ops
    ops.watch_remove("option", "BBB", by="test")
    footprints, _, _ = _scan({"AAA": (1000, 200)})
    assert _record(footprints) == {TABLE: 2}
    (head,) = _headers()
    assert head["quality"] == "complete" and head["quality_note"] is None


def test_a_rejected_key_makes_the_run_partial(store):
    footprints, _, _ = _scan({"AAA": (1000, 200), "BBB": (300, 300)})
    _record(footprints, uw_health={"calls": 1, "failed": 1, "auth_error": {"status": 401}})
    (head,) = _headers()
    assert head["quality"] == "partial" and "rejected the API key (HTTP 401)" in head["quality_note"]


def test_an_empty_watchlist_records_a_partial_header(store):
    from RevenueChaser.rcdata import ops
    for t in ("AAA", "BBB"):
        ops.watch_remove("stock", t, by="test")
    footprints, _, _ = _scan({})
    assert _record(footprints) == {TABLE: 0}
    (head,) = _headers()
    assert head["quality"] == "partial" and "watchlist is empty" in head["quality_note"]


def test_a_partial_run_does_not_shut_out_a_later_whole_run(store):
    bad, _, _ = _scan({"AAA": (1000, 200)})                    # BBB missing: partial
    _record(bad, run_id="r1")
    good, _, _ = _scan({"BBB": (300, 300)})
    assert _record(good, run_id="r2", run_at=COMPLETE + 600) == {TABLE: 2}


def test_recording_the_same_run_id_twice_writes_nothing_more(store):
    footprints, _, _ = _scan({"AAA": (1000, 200), "BBB": (300, 300)})
    _record(footprints)
    _record(footprints)
    assert len(_headers()) == 1 and len(_rows("r1")) == 2


def test_the_session_view_reads_the_snapshot_back(store):
    footprints, _, _ = _scan({"AAA": (1000, 200), "BBB": (300, 300)})
    _record(footprints)
    got = rawstore.query("SELECT session_date, ticker FROM leaps_footprint_by_session ORDER BY ticker", db="history")
    assert [(r["session_date"], r["ticker"]) for r in got] == [(TODAY, "AAA"), (TODAY, "BBB")]
