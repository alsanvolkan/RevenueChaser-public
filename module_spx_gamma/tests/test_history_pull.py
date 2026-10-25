"""The 2-year SPX Gamma history pull (D1, 25 Oct 2026): the loop over sessions, its budget and its stops.

The UW client is a fake (or a real ``UWClient`` over an ``httpx.MockTransport``), candles are a fake store read and
the clock is the calendar seam, so nothing here touches the network, the real store or today's date.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from RevenueChaser.core import calendar as nyse_calendar
from RevenueChaser.core import universe_fetch
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.module_spx_gamma import history_pull as hp
from RevenueChaser.module_spx_gamma import snapshot as snap
from RevenueChaser.module_spx_gamma.snapshot import UWApiError

from .conftest import FakeUW

# Three NYSE sessions (Mon-Wed); the Thursday is a session the store holds no close for.
DAYS = ["2025-03-10", "2025-03-11", "2025-03-12"]
NO_CLOSE = "2025-03-13"
NOW = datetime(2025, 3, 15, 15, 0, tzinfo=timezone.utc)      # a Saturday: no session open


def _candles(asof=None, ticker=None):
    asof = str(asof)
    return [{"date": d, "open": 1.0, "high": 2.0, "low": 0.5, "close": 5600.0 + i, "volume": 1.0}
            for i, d in enumerate(DAYS) if d <= asof]


def _ok(path, params):
    if path.endswith("/greek-exposure/expiry"):
        return {"data": [{"expiry": params["date"], "dte": 0}]}
    if path.endswith("/flow-per-strike"):
        return []
    if path.endswith("/iv-rank"):
        return {"data": [{"date": "2024-10-25", "iv_rank_1y": "10"}, {"date": params["date"], "iv_rank_1y": "20"}]}
    if path.endswith("/interpolated-iv"):
        return {"data": [{"date": params["date"], "days": 1}, {"date": params["date"], "days": 7}]}
    return {"data": [{"date": params.get("date"), "strike": "5600"}]}


class Recorder(FakeUW):
    def __init__(self, handler=_ok):
        super().__init__(handler)
        self.calls: list[tuple[str, dict]] = []

    def get_result(self, ep, params=None):
        self.calls.append((ep, dict(params or {})))
        return super().get_result(ep, params)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(snap, "stored_spx_candles", _candles)
    monkeypatch.setattr(snap.stored_inputs, "vol_index_rows", lambda day=None: ({}, None))
    monkeypatch.setattr(snap.stored_inputs, "cot_row", lambda day=None: ({}, None))
    monkeypatch.setattr(nyse_calendar, "_now", lambda: NOW)


def _pull(tmp_path, uw, start=DAYS[0], end=NO_CLOSE, **kw):
    kw.setdefault("sleep", lambda s: None)
    return hp.pull_history(uw, start, end, data_root=tmp_path, **kw)["counts"]


def _days(tmp_path):
    raw = tmp_path / "raw"
    return sorted(d.name for d in raw.iterdir() if (d / "manifest.json").exists()) if raw.exists() else []


def test_pulls_each_session_once_and_counts_every_call(tmp_path):
    uw = Recorder()
    counts = _pull(tmp_path, uw)
    assert _days(tmp_path) == DAYS
    assert counts["written"] == 3 and counts["sessions"] == 4
    assert counts["skipped_no_close"] == 1                     # the Thursday cost no request
    assert counts["calls"] == len(uw.calls) == 1 + 3 * snap.HISTORY_CALLS_PER_SESSION
    assert counts["status"] == {"200": len(uw.calls)}
    assert counts["stop_reason"] is None
    for day in DAYS:
        manifest = json.loads((tmp_path / "raw" / day / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["origin"] == "history_pull"


def test_iv_rank_is_asked_once_as_a_series(tmp_path):
    uw = Recorder()
    counts = _pull(tmp_path, uw)
    rank_calls = [c for c in uw.calls if c[0].endswith("/iv-rank")]
    assert rank_calls == [("/stock/SPX/iv-rank", {"date": NO_CLOSE, "timespan": "2y"})]
    stored = tmp_path / counts["iv_rank_file"]
    assert json.loads(stored.read_text(encoding="utf-8"))["data"][0]["date"] == "2024-10-25"
    assert counts["shapes"]["iv_rank"]["rows"] == 2
    # a second run over the same range finds the series stored and asks for nothing at all
    again = Recorder()
    assert _pull(tmp_path, again)["calls"] == 0 and again.calls == []


def test_a_stored_day_costs_no_request(tmp_path):
    first = tmp_path / "raw" / DAYS[0]
    first.mkdir(parents=True)
    (first / "manifest.json").write_text('{"origin": "fetch"}', encoding="utf-8")
    uw = Recorder()
    counts = _pull(tmp_path, uw)
    assert counts["skipped_existing"] == 1
    assert not [c for c in uw.calls if c[1].get("date") == DAYS[0]]
    assert json.loads((first / "manifest.json").read_text(encoding="utf-8")) == {"origin": "fetch"}


def test_the_shape_of_the_dated_volatility_answers_is_reported(tmp_path):
    counts = _pull(tmp_path, Recorder(), shape_sessions=2)
    assert sorted(counts["shapes"]["sessions"]) == DAYS[:2]            # only the first N sessions
    iv = counts["shapes"]["sessions"][DAYS[0]]["interpolated_iv"]
    assert iv == {"type": "list", "rows": 2, "keys": ["date", "days"], "dates": [DAYS[0], DAYS[0]]}
    assert set(counts["shapes"]["sessions"][DAYS[0]]) == set(hp.SHAPE_ENDPOINTS)


def test_the_streak_constant_mirrors_the_universe_fetch():
    assert hp.RATE_LIMIT_STREAK_STOP == universe_fetch._RATE_LIMIT_STREAK_STOP == 5


def test_stops_after_a_run_of_429s_and_keeps_no_partial_day(tmp_path):
    state = {"n": 0}

    def throttled_after_one_day(path, params):
        state["n"] += 1
        if state["n"] > 1 + snap.HISTORY_CALLS_PER_SESSION + 3:      # iv-rank, one day, then 3 calls of day 2
            raise UWApiError(path, 429, "HTTP 429")
        return _ok(path, params)
    uw = Recorder(throttled_after_one_day)
    counts = _pull(tmp_path, uw)
    assert counts["stop_reason"] == "rate_limit_streak"
    assert counts["status"]["429"] == hp.RATE_LIMIT_STREAK_STOP
    assert _days(tmp_path) == DAYS[:1]
    assert not list((tmp_path / "raw").glob(".partial-*"))
    assert len(uw.calls) == 1 + snap.HISTORY_CALLS_PER_SESSION + 3 + hp.RATE_LIMIT_STREAK_STOP


def test_respects_max_calls_and_never_starts_a_day_it_cannot_finish(tmp_path):
    uw = Recorder()
    budget = 1 + 2 * snap.HISTORY_CALLS_PER_SESSION + 5               # room for two days, not three
    counts = _pull(tmp_path, uw, max_calls=budget)
    assert counts["stop_reason"] == "max_calls"
    assert counts["calls"] == len(uw.calls) == 1 + 2 * snap.HISTORY_CALLS_PER_SESSION <= budget
    assert _days(tmp_path) == DAYS[:2]


def test_sleeps_between_calls(tmp_path):
    slept: list[float] = []
    uw = Recorder()
    _pull(tmp_path, uw, end=DAYS[0], sleep_s=0.4, sleep=slept.append)
    assert slept == [0.4] * (len(uw.calls) - 1)


def test_refuses_to_start_during_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr(nyse_calendar, "in_us_session_window", lambda now=None: True)
    uw = Recorder()
    with pytest.raises(hp.HistoryPullRefused, match="session"):
        _pull(tmp_path, uw)
    assert uw.calls == []


def test_stops_when_the_session_opens_mid_run(tmp_path, monkeypatch):
    uw = Recorder()
    monkeypatch.setattr(nyse_calendar, "in_us_session_window",
                        lambda now=None: len(uw.calls) >= 1 + snap.HISTORY_CALLS_PER_SESSION)
    counts = _pull(tmp_path, uw)
    assert counts["stop_reason"] == "session_open"
    assert _days(tmp_path) == DAYS[:1]


def test_stops_after_a_run_of_rejected_days(tmp_path, monkeypatch):
    """A 403 per day costs one call (no fallback); five in a row is a dead key or a range past the edge."""
    days = [f"2025-02-{d:02d}" for d in (3, 4, 5, 6, 7, 10, 11)]

    def candles(asof=None, ticker=None):
        return [{"date": d, "close": 5600.0} for d in days if d <= str(asof)]

    def rejected(path, params):
        if path.endswith("/gex-levels"):
            raise UWApiError(path, 403, "HTTP 403")
        return _ok(path, params)
    monkeypatch.setattr(snap, "stored_spx_candles", candles)
    uw = Recorder(rejected)
    counts = _pull(tmp_path, uw, start=days[0], end=days[-1])
    assert counts["stop_reason"] == "auth_streak"
    assert counts["status"]["403"] == hp.AUTH_STREAK_STOP == 5
    assert counts["failed"] == 5 and counts["failures"][0]["day"] == days[0]
    assert len(uw.calls) == 1 + 5


def _real_client(monkeypatch, handler):
    """A real ``UWClient`` (retries, breaker, accounting) over an in-memory transport."""
    monkeypatch.setattr(unusual_whales, "BASE", "http://uw.test/api")
    uw = unusual_whales.UWClient("test-key", sleep=lambda s: None)
    uw._client = httpx.Client(transport=httpx.MockTransport(handler), headers=uw.headers)
    return uw


FEB = [f"2025-02-{d:02d}" for d in (3, 4, 5, 6, 7, 10, 11, 12, 13, 14)]


def _feb_candles(asof=None, ticker=None):
    return [{"date": d, "close": 5600.0} for d in FEB if d <= str(asof)]


def test_stops_when_the_client_breaker_opens_on_failed_days(tmp_path, monkeypatch):
    """The real path: every day fails (503), so the breaker opens on a failed day, not a written one."""
    monkeypatch.setattr(snap, "stored_spx_candles", _feb_candles)
    seen = []

    def down(request):
        seen.append(request.url.path)
        return httpx.Response(503)
    uw = _real_client(monkeypatch, down)
    counts = _pull(tmp_path, uw, start=FEB[0], end=FEB[-1])
    # iv-rank and four days each fail once after 3 attempts: the fifth final failure opens the breaker
    assert counts["stop_reason"] == "breaker_open"
    assert counts["failed"] == unusual_whales.BREAKER_THRESHOLD - 1 and counts["written"] == 0
    assert counts["calls"] == unusual_whales.BREAKER_THRESHOLD
    assert counts["http_requests"] == len(seen) == unusual_whales.BREAKER_THRESHOLD * unusual_whales.MAX_ATTEMPTS


@pytest.mark.parametrize("start,end", [("2025-03-12", "2025-03-10"), ("2025-03-10", "2025-03-15"),
                                        ("2025-03-10", "2025-03-20"), ("not-a-date", "2025-03-10")])
def test_refuses_a_bad_range(tmp_path, start, end):
    uw = Recorder()
    with pytest.raises(ValueError):
        _pull(tmp_path, uw, start=start, end=end)
    assert uw.calls == []


def test_counts_http_requests_including_the_clients_retries(tmp_path, monkeypatch):
    """UW's budget counts requests: a 429 retried by the client is three of them, one logical call."""
    monkeypatch.setattr(unusual_whales, "BASE", "http://uw.test/api")
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/max-pain") and len([p for p in seen if p.endswith("/max-pain")]) <= 2:
            return httpx.Response(429)
        path = request.url.path.removeprefix("/api")
        return httpx.Response(200, json=_ok(path, dict(request.url.params)))
    uw = unusual_whales.UWClient("test-key", sleep=lambda s: None)
    uw._client = httpx.Client(transport=httpx.MockTransport(handler), headers=uw.headers)
    counts = _pull(tmp_path, uw, end=DAYS[0])
    assert counts["calls"] == 1 + snap.HISTORY_CALLS_PER_SESSION
    assert counts["http_requests"] == len(seen) == counts["calls"] + 2
    assert _days(tmp_path) == DAYS[:1]
    assert uw._client.event_hooks["request"] == [], "the counting hook is removed after the run"


def test_max_calls_also_caps_the_http_requests_the_client_retries(tmp_path, monkeypatch):
    """UW counts requests, and the client retries a 429 up to 3 times: the cap binds on requests (#194 review)."""
    attempts: dict[str, int] = {}
    sent = []

    def throttled_max_pain(request):
        sent.append(request.url.path)
        key = f"{request.url.path}?{request.url.params.get('date')}"
        if request.url.path.endswith("/max-pain"):
            attempts[key] = attempts.get(key, 0) + 1
            if attempts[key] <= 2:
                return httpx.Response(429)
        return httpx.Response(200, json=_ok(request.url.path.removeprefix("/api"), dict(request.url.params)))
    uw = _real_client(monkeypatch, throttled_max_pain)
    counts = _pull(tmp_path, uw, max_calls=30)
    assert counts["http_requests"] == len(sent) <= 30
    assert counts["stop_reason"] == "max_calls"
    assert _days(tmp_path) == DAYS[:1]                      # the second day was cut short and not kept
    assert not list((tmp_path / "raw").glob(".partial-*"))


def test_a_day_that_no_longer_fits_the_request_count_is_not_started(tmp_path, monkeypatch):
    """After day 1 the calls (14) leave room for a day under 28, the requests (16, two retried) do not."""
    tries: dict[str, int] = {}

    def two_retries_on_day_one(request):
        key = f"{request.url.path}?{request.url.params.get('date')}"
        tries[key] = tries.get(key, 0) + 1
        if key.endswith(f"/max-pain?{DAYS[0]}") and tries[key] <= 2:
            return httpx.Response(429)
        return httpx.Response(200, json=_ok(request.url.path.removeprefix("/api"), dict(request.url.params)))
    counts = _pull(tmp_path, _real_client(monkeypatch, two_retries_on_day_one), max_calls=28)
    assert (counts["calls"], counts["http_requests"]) == (14, 16)
    assert counts["stop_reason"] == "max_calls" and _days(tmp_path) == DAYS[:1]
