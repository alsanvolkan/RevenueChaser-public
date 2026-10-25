"""LEAPS board -> result history (#377, A3). Offline; the store is a temp DuckDB.

A LEAPS Fetch records every watchlist footprint through ``core/history.record_run``: one ``result_runs`` header plus
one ``leaps_footprint_history`` row per watchlist ticker, whatever its label, with ``fetch_status`` and the inputs it
was classified from. LEAPS is a session board: the first complete, whole run of a session keeps its rows; a later,
intraday or partial run keeps its header only.
"""
from __future__ import annotations

import json
import zlib
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import ledger, rawstore
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.core.providers.unusual_whales import UWClient
from RevenueChaser.module_leaps import engine, history as leaps_history
from RevenueChaser.module_leaps.scan import scan_leaps
from RevenueChaser.module_leaps.tests.test_leaps_l3 import seed_session

BOARD, TABLE = "module_leaps", "leaps_footprint_history"
TODAY = date(2026, 10, 6)
# 2026-10-06 is a Tuesday: 22:00 UTC = 18:00 ET (session complete); 14:00 UTC = 10:00 ET (intraday).
COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()
INTRADAY = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc).timestamp()
FAR = "2027-12-17"                       # > 270 days after TODAY


class _UW:
    """A UW double: far-dated OI per ticker, no tape, a failure counter the scan reads per ticker."""
    auth_error = None

    def __init__(self, oi: dict, fail: set = frozenset(), raise_for: set = frozenset()):
        self.oi, self.fail, self.raise_for = oi, set(fail), set(raise_for)
        self.calls = self.failed = 0

    def health(self):
        return {"calls": self.calls, "failed": self.failed, "auth_error": None, "failures": [],
                "breaker_open": False, "skipped_calls": 0}

    def option_trades(self, **kw):
        return []

    def screener_batch(self, tickers):
        return {t: {"full_name": f"{t} Inc"} for t in tickers}

    def oi_per_expiry(self, ticker, date=None):
        self.calls += 1
        if ticker in self.raise_for:
            raise RuntimeError("socket closed")
        if ticker in self.fail:
            self.failed += 1
            return []
        call, put = self.oi.get(ticker, (0, 0))
        return [{"expiry": FAR, "call_oi": call, "put_oi": put}]

    def flow_per_expiry(self, ticker):
        self.calls += 1
        return []

    def iv_rank(self, ticker):
        self.calls += 1
        return [{"iv_rank_1y": "55.0"}]

    # The real client's ``*_result`` reads (the scan reads through them); this double fails only by its counter.
    def oi_per_expiry_result(self, ticker, date=None):
        return ProviderResult("ok", self.oi_per_expiry(ticker, date))

    def flow_per_expiry_result(self, ticker):
        return ProviderResult("ok", self.flow_per_expiry(ticker))

    def iv_rank_result(self, ticker):
        return ProviderResult("ok", self.iv_rank(ticker))


class _Resp:
    def __init__(self, status: int, payload=None):
        self.status_code, self._payload, self.text, self.headers = status, payload or {"data": []}, "", {}

    def json(self):
        return self._payload


def _real_uw(bbb_status: int) -> UWClient:
    """The real UW client, offline: AAA answers far-dated OI, BBB's oi-per-expiry answers ``bbb_status``."""
    c = UWClient(key="k", sleep=lambda s: None)

    def _get(url, params=None):
        if "/stock/BBB/oi-per-expiry" in url:
            return _Resp(bbb_status, None if bbb_status == 200 else {"message": "not found"})
        if "/oi-per-expiry" in url:
            return _Resp(200, {"data": [{"expiry": FAR, "call_oi": 1000, "put_oi": 200}]})
        return _Resp(200)

    c._client.get = _get  # type: ignore[method-assign]
    return c


@pytest.fixture
def store(rawstore_tmp_db):
    ledger.take_notes()
    rawstore.save_watchlist({"AAA", "BBB"})
    return rawstore_tmp_db


def _scan(uw, **kw):
    footprints: list = []
    watched, unwatched = scan_leaps(uw, today=TODAY, footprints=footprints, **kw)
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
    footprints, watched, _ = _scan(_UW({"AAA": (1000, 200), "BBB": (300, 300)}))
    assert [w["ticker"] for w in watched] == ["AAA", "BBB"]         # every pinned name, whatever its label (#377 L2)
    assert [f["ticker"] for f in footprints] == ["AAA", "BBB"]        # the history gets both
    assert {f["snapshot"]["label"] for f in footprints} == {"Accumulating", "Quiet"}
    assert all(f["fetch_status"] == "ok" for f in footprints)


def test_the_scan_names_the_inputs_a_footprint_was_classified_from(store):
    seed_session(date(2026, 10, 5), {"AAA": [{"expiry": FAR, "call_oi": 500, "put_oi": 0}]})
    footprints, _, _ = _scan(_UW({"AAA": (1000, 200), "BBB": (300, 300)}))
    inputs = footprints[0]["inputs"]
    assert inputs["oi_per_expiry"] == [{"expiry": FAR, "call_oi": 1000, "put_oi": 200}]
    assert inputs["iv_rank"] == [{"iv_rank_1y": "55.0"}] and inputs["flow_per_expiry"] == []
    assert inputs["baseline_1d"]["far_call_oi"] == 500 and inputs["flow_candidate"] is None


def test_a_ticker_whose_uw_call_failed_is_provider_failed(store):
    footprints, _, _ = _scan(_UW({"AAA": (1000, 200)}, fail={"BBB"}))
    status = {f["ticker"]: f["fetch_status"] for f in footprints}
    assert status == {"AAA": "ok", "BBB": "provider_failed"}


@pytest.mark.parametrize("status", [400, 404, 422])
def test_a_ticker_whose_uw_read_failed_uncounted_is_provider_failed_and_keeps_no_session_rows(store, status):
    # A 4xx other than 401/403/429 is a failed result that health() does not count: the read status must say so,
    # or the empty answer becomes a Quiet footprint in the session snapshot (#403 review R1).
    uw = _real_uw(status)
    footprints, _, _ = _scan(uw)
    assert uw.health()["failed"] == 0
    status_of = {f["ticker"]: f["fetch_status"] for f in footprints}
    assert status_of == {"AAA": "ok", "BBB": "provider_failed"}
    assert footprints[1]["inputs"]["read_status"]["oi_per_expiry"] == "failed"
    _record(footprints, uw_health=uw.health())
    (head,) = _headers()
    assert head["quality"] == "partial" and head["session_snapshot"] is False and _rows("r1") == []


def test_a_ticker_whose_uw_read_raised_is_provider_failed_with_its_error(store):
    footprints, _, _ = _scan(_UW({"AAA": (1000, 200)}, raise_for={"BBB"}))
    (bbb,) = [f for f in footprints if f["ticker"] == "BBB"]
    assert bbb["fetch_status"] == "provider_failed" and bbb["inputs"]["error"] == "RuntimeError"
    _record(footprints)
    (head,) = _headers()
    assert head["quality"] == "partial" and _rows("r1") == []


def test_an_ok_read_with_empty_data_stays_ok(store):
    footprints, _, _ = _scan(_real_uw(200))                 # BBB: 200 with no rows is real, empty data
    assert {f["ticker"]: f["fetch_status"] for f in footprints} == {"AAA": "ok", "BBB": "ok"}


def test_a_ticker_whose_fetch_raised_is_kept_with_its_error(store, monkeypatch):
    real = engine.fetch_and_classify

    def boom(ticker, uw, today=None, inputs=None, near_term_event=False, **kw):
        if ticker == "BBB":
            raise ValueError("bad payload")
        return real(ticker, uw, today=today, inputs=inputs, near_term_event=near_term_event, **kw)

    monkeypatch.setattr(engine, "fetch_and_classify", boom)
    footprints, _, _ = _scan(_UW({"AAA": (1000, 200)}))
    (bbb,) = [f for f in footprints if f["ticker"] == "BBB"]
    assert bbb["snapshot"] is None and bbb["fetch_status"] == "error: ValueError"


def test_a_scan_without_the_sink_is_unchanged(store):
    watched, unwatched = scan_leaps(_UW({"AAA": (1000, 200)}), today=TODAY)
    assert [w["ticker"] for w in watched] == ["AAA", "BBB"] and unwatched == []   # the sink is optional (#377 L2)


# ── the record ──

def test_a_run_records_a_header_and_one_row_per_watchlist_ticker(store):
    seed_session(date(2026, 10, 5), {"AAA": [{"expiry": FAR, "call_oi": 500, "put_oi": 0}]})
    footprints, _, _ = _scan(_UW({"AAA": (1000, 200), "BBB": (300, 300)}))
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
    footprints, _, _ = _scan(_UW({"AAA": (1000, 200), "BBB": (300, 300)}))
    _record(footprints, shown={"watched": 0, "unwatched": 0}, warnings=["The print scan hit its cap"])
    (head,) = _headers()
    assert head["model_version"] == leaps_history.model_version() and head["model_version"].startswith("2+")
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
    footprints, _, _ = _scan(_UW({"AAA": (1000, 200), "BBB": (300, 300)}))
    _record(footprints, run_id="r1")
    assert _record(footprints, run_id="r2", run_at=COMPLETE + 3600) == {TABLE: 0}
    first, second = _headers()
    assert first["session_snapshot"] is True and second["session_snapshot"] is False
    assert "already recorded by run r1" in second["quality_note"]
    assert len(_rows("r1")) == 2 and _rows("r2") == []


def test_an_intraday_run_keeps_its_header_only(store):
    footprints, _, _ = _scan(_UW({"AAA": (1000, 200), "BBB": (300, 300)}))
    _record(footprints, run_id="early", run_at=INTRADAY)
    (head,) = _headers()
    assert head["quality"] == "intraday" and _rows("early") == []


def test_a_failed_footprint_makes_the_run_partial_and_keeps_no_rows(store):
    footprints, _, _ = _scan(_UW({"AAA": (1000, 200)}, fail={"BBB"}))
    _record(footprints, uw_health={"calls": 6, "failed": 1})
    (head,) = _headers()
    assert head["quality"] == "partial" and head["session_snapshot"] is False and _rows("r1") == []
    assert "1 of 2 footprints were not fetched whole: BBB" in head["quality_note"]
    assert "1 of 6 Unusual Whales calls failed" in head["quality_note"]


def test_a_rejected_key_makes_the_run_partial(store):
    footprints, _, _ = _scan(_UW({"AAA": (1000, 200), "BBB": (300, 300)}))
    _record(footprints, uw_health={"calls": 1, "failed": 1, "auth_error": {"status": 401}})
    (head,) = _headers()
    assert head["quality"] == "partial" and "rejected the API key (HTTP 401)" in head["quality_note"]


def test_an_empty_watchlist_records_a_partial_header(store):
    rawstore.save_watchlist(set())
    footprints, _, _ = _scan(_UW({}))
    assert _record(footprints) == {TABLE: 0}
    (head,) = _headers()
    assert head["quality"] == "partial" and "watchlist is empty" in head["quality_note"]


def test_a_partial_run_does_not_shut_out_a_later_whole_run(store):
    bad, _, _ = _scan(_UW({"AAA": (1000, 200)}, fail={"BBB"}))
    _record(bad, run_id="r1")
    good, _, _ = _scan(_UW({"AAA": (1000, 200), "BBB": (300, 300)}))
    assert _record(good, run_id="r2", run_at=COMPLETE + 600) == {TABLE: 2}


def test_recording_the_same_run_id_twice_writes_nothing_more(store):
    footprints, _, _ = _scan(_UW({"AAA": (1000, 200), "BBB": (300, 300)}))
    _record(footprints)
    _record(footprints)
    assert len(_headers()) == 1 and len(_rows("r1")) == 2


def test_the_session_view_reads_the_snapshot_back(store):
    footprints, _, _ = _scan(_UW({"AAA": (1000, 200), "BBB": (300, 300)}))
    _record(footprints)
    got = rawstore.query("SELECT session_date, ticker FROM leaps_footprint_by_session ORDER BY ticker", db="history")
    assert [(r["session_date"], r["ticker"]) for r in got] == [(TODAY, "AAA"), (TODAY, "BBB")]


def test_the_rolling_cache_the_classifier_reads_is_unchanged(store):
    footprints, _, _ = _scan(_UW({"AAA": (1000, 200), "BBB": (300, 300)}))
    _record(footprints)
    cached = engine.load_cache("AAA")
    assert set(cached) == {"latest", "history"} and cached["latest"]["far_call_oi"] == 1000
    assert "fetch_status" not in cached["latest"] and "inputs" not in cached["latest"]
