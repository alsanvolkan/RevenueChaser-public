"""L1 (#377 LP-A / LP-1 = DF92, plus DF95): a failed UW read is not saved as a zero record. Offline; temp DuckDB.

LP-A (avo, 2026-10-10): when a footprint read of a LEAPS Fetch failed or raised, ``fetch_and_classify`` skips
``classify`` and ``save_cache`` and returns an ``N/A`` snapshot with its ``fetch_status``. The empty answer used to
become a "Quiet" snapshot that evicted a real entry from the 60-run ``leaps_cache`` list and became the next run's
baseline. An ``ok`` read with no data is real and is still saved as today.

DF95: a print the tape served without ``size``, ``ask_vol`` or ``bid_vol`` is stored NULL, not 0, so the ask-side
share skips it instead of reading it as all bid.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import ledger, rawstore
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.core.providers.unusual_whales import UWClient
from RevenueChaser.module_leaps import engine, history as leaps_history
from RevenueChaser.module_leaps.scan import scan_leaps

TODAY = date(2026, 10, 6)
COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()
FAR = "2027-12-17"
REAL = {"ticker": "BBB", "label": "Accumulating", "far_call_oi": 800, "far_put_oi": 100, "as_of": "2026-10-05"}


class _Resp:
    def __init__(self, status: int, payload=None):
        self.status_code, self._payload, self.text, self.headers = status, payload or {"data": []}, "", {}

    def json(self):
        return self._payload


def _real_uw(bbb_oi_status: int = 200, bbb_oi_rows=None) -> UWClient:
    """The real UW client, offline: AAA answers far-dated OI; BBB's oi-per-expiry answers ``bbb_oi_status``."""
    c = UWClient(key="k", sleep=lambda s: None)

    def _get(url, params=None):
        if "/stock/BBB/oi-per-expiry" in url:
            if bbb_oi_status != 200:
                return _Resp(bbb_oi_status, {"message": "not found"})
            return _Resp(200, {"data": bbb_oi_rows or []})
        if "/oi-per-expiry" in url:
            return _Resp(200, {"data": [{"expiry": FAR, "call_oi": 1000, "put_oi": 200}]})
        return _Resp(200)

    c._client.get = _get  # type: ignore[method-assign]
    return c


class _Raising:
    """Every footprint read raises (a socket error inside the client)."""
    auth_error = None

    def health(self):
        return {"calls": 0, "failed": 0, "auth_error": None, "failures": [], "breaker_open": False,
                "skipped_calls": 0}

    def option_trades(self, **kw):
        return []

    def screener_batch(self, tickers):
        return {}

    def _boom(self, *a, **kw):
        raise RuntimeError("socket closed")

    oi_per_expiry_result = flow_per_expiry_result = iv_rank_result = _boom


@pytest.fixture
def store(rawstore_tmp_db):
    ledger.take_notes()
    rawstore.save_watchlist({"BBB"})
    engine.save_cache("BBB", dict(REAL))
    return rawstore_tmp_db


def _cache_row():
    return rawstore.query("SELECT payload FROM leaps_cache WHERE ticker = 'BBB'")


def _spy_save(monkeypatch):
    calls = []
    real = engine.save_cache
    monkeypatch.setattr(engine, "save_cache", lambda t, s: (calls.append(t), real(t, s)))
    return calls


@pytest.mark.parametrize("status", [404, 500])
def test_a_failed_oi_read_returns_na_and_does_not_save(store, monkeypatch, status):
    before = _cache_row()
    calls = _spy_save(monkeypatch)
    inputs: dict = {}
    snap = engine.fetch_and_classify("BBB", _real_uw(status), today=TODAY, inputs=inputs)
    assert snap["label"] == "N/A" and snap["fetch_status"] == "provider_failed"
    assert calls == []
    assert _cache_row() == before                                   # leaps_cache untouched, byte for byte
    assert inputs["read_status"]["oi_per_expiry"] == "failed"


def test_every_read_raising_leaves_the_cache_and_keeps_no_session_rows(store):
    before = _cache_row()
    footprints: list = []
    scan_leaps(_Raising(), today=TODAY, footprints=footprints)
    (bbb,) = footprints
    assert bbb["snapshot"]["label"] == "N/A" and bbb["fetch_status"] == "provider_failed"
    assert bbb["inputs"]["error"] == "RuntimeError: socket closed"
    assert _cache_row() == before
    assert engine.load_cache("BBB")["latest"] == REAL                # the real entry is not evicted
    leaps_history.record(footprints, run_id="r1", run_at=COMPLETE)
    (head,) = rawstore.query("SELECT * FROM result_runs WHERE board = 'module_leaps'", db="history")
    assert head["quality"] == "partial" and head["session_snapshot"] is False
    assert rawstore.query("SELECT * FROM leaps_footprint_history", db="history") == []


def test_the_history_row_of_a_failed_read_says_na_not_quiet(store):
    footprints: list = []
    scan_leaps(_real_uw(404), today=TODAY, footprints=footprints)
    (row,) = leaps_history.rows_from_footprints(footprints)
    assert row["label"] == "N/A" and row["fetch_status"] == "provider_failed" and row["score"] is None


def test_an_ok_read_with_empty_data_still_saves_the_quiet_snapshot(store, monkeypatch):
    calls = _spy_save(monkeypatch)
    snap = engine.fetch_and_classify("BBB", _real_uw(200), today=TODAY, inputs={})
    assert snap["label"] == "Quiet" and "fetch_status" not in snap
    assert calls == ["BBB"]
    assert engine.load_cache("BBB")["latest"]["label"] == "Quiet"


def test_a_failed_flow_read_with_a_good_oi_read_is_not_saved(store, monkeypatch):
    class _FlowFails(UWClient):
        def flow_per_expiry_result(self, ticker):
            return ProviderResult("failed", [], {"status": 404})
    uw = _FlowFails(key="k", sleep=lambda s: None)
    uw._client.get = lambda url, params=None: _Resp(200, {"data": [{"expiry": FAR, "call_oi": 1000, "put_oi": 1}]})
    calls = _spy_save(monkeypatch)
    snap = engine.fetch_and_classify("BBB", uw, today=TODAY, inputs={})
    assert snap["label"] == "N/A" and calls == []


# ── DF95: missing print fields are NULL, not 0 ──

def _tape_row(pid, **over):
    row = {"id": pid, "underlying_symbol": "ZZZZ", "strike": "200", "expiry": "2027-06-18", "premium": "100000",
           "size": 20, "option_type": "call", "delta": "0.8", "next_earnings_date": None,
           "executed_at": "2026-10-05T14:00:00Z", "ask_vol": 80, "bid_vol": 20, "tags": []}
    row.update(over)
    return {k: v for k, v in row.items() if v is not ...}


def test_a_print_without_size_or_sides_is_stored_null(rawstore_tmp_db):
    p = engine._normalize_print(_tape_row("p1", size=..., ask_vol=..., bid_vol=None))
    assert p["size"] is None and p["ask_vol"] is None and p["bid_vol"] is None
    engine.store_prints([p], today=TODAY, run_at=COMPLETE)
    (got,) = rawstore.query("SELECT size, ask_vol, bid_vol FROM leaps_flow_prints WHERE id = 'p1'")
    assert got == {"size": None, "ask_vol": None, "bid_vol": None}


def test_a_print_with_zero_sides_keeps_its_zeros(rawstore_tmp_db):
    p = engine._normalize_print(_tape_row("p1", ask_vol=0, bid_vol="0", size="0"))
    assert (p["size"], p["ask_vol"], p["bid_vol"]) == (0, 0, 0)


def test_the_ask_share_skips_a_print_without_ask_vol(rawstore_tmp_db):
    prints = [engine._normalize_print(_tape_row("p1")), engine._normalize_print(_tape_row("p2", ask_vol=...))]
    engine.store_prints(prints, today=TODAY, run_at=COMPLETE)
    agg = engine.aggregate_flow_prints(engine.load_recent_prints(today=TODAY), TODAY)
    assert agg["ZZZZ"]["ask_share"] == 0.8                          # p2 was read as all bid (0.4) before
