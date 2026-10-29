"""#198 E-3: runner behaviour — preselect applied (500 cap), one /earnings call per ticker,
bounded scoring pool (E14), shared watchlist augmentation (E15), zero cards say so (E23).

Offline: fake UW client, ``rawstore_tmp_db`` keeps LEAPS reads/writes off the real store.
"""
import threading
import time
from datetime import date

import pytest

from RevenueChaser.module_earnings_radar import pipeline as pl
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW


@pytest.fixture(autouse=True)
def _isolate(rawstore_tmp_db):
    return rawstore_tmp_db


class _CountingUW(_FakeUW):
    """Counts the /earnings/{t} reads; offers the one-call ``earnings_rows``."""

    def __init__(self, delay=0.0, boom=(), **ov):
        super().__init__(**ov)
        self.rows_calls = []
        self.legacy_calls = 0
        self.delay = delay
        self.boom = set(boom)
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def earnings_rows(self, ticker):
        with self._lock:
            self.rows_calls.append(ticker)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if ticker in self.boom:
                raise RuntimeError("provider blew up")
            return []
        finally:
            with self._lock:
                self.active -= 1

    def earnings_data(self, ticker):
        self.legacy_calls += 1
        return {}

    def earnings_history(self, ticker):
        self.legacy_calls += 1
        return []


def _strong():
    return {"close": "50", "call_volume": "5000", "put_volume": "5000", "total_open_interest": "50000",
            "call_premium": "400000", "put_premium": "300000"}


def _weak():
    return {"close": "50", "call_volume": "5000", "put_volume": "5000", "total_open_interest": "10",
            "call_premium": "400000", "put_premium": "300000"}


def _run(tickers, screener, uw=None, watchlist=None, em=0.05):
    uw = uw or _CountingUW()
    cal = {t: {"earnings_date": "2026-08-14", "full_name": t, "sector": "Technology",
               "expected_move_perc": em} for t in tickers}
    p = EarningsPipeline(tickers, uw=uw, cal_meta=cal)
    p._screener_cache = screener
    cards = p.run(use_preselect=True, watchlist=watchlist or set())
    return p, uw, cards


def _tickers(n):
    return [f"T{i:02d}" for i in range(n)]


class TestPreselectApplied:
    def test_failing_names_are_not_scored_and_not_fetched(self):
        ts = _tickers(25)
        scr = {t: (_weak() if i % 5 == 0 else _strong()) for i, t in enumerate(ts)}   # 5 weak
        p, uw, cards = _run(ts, scr)
        weak = {t for i, t in enumerate(ts) if i % 5 == 0}
        assert {c.ticker for c in cards} == set(ts) - weak
        assert not weak & set(uw.rows_calls)             # no provider call for an excluded name
        assert {e["ticker"] for e in p._filter_stats["excluded"]} == weak
        assert all(p._filter_outcome[t][0] == "excluded" for t in weak)

    def test_the_500_cap_is_applied_and_the_rest_are_recorded(self, monkeypatch):
        monkeypatch.setattr(pl, "MAX_DEEP_CHECKS_PER_DAY", 6)
        ts = _tickers(25)
        scr = {t: _strong() for t in ts}
        p, uw, cards = _run(ts, scr)
        assert len(cards) == 6
        assert len(set(uw.rows_calls)) == 6
        capped = [e for e in p._filter_stats["excluded"] if e["reasons"] == ["cap"]]
        assert len(capped) == 19
        assert p._filter_stats["passed"] == 6
        assert p._filter_stats["by_reason"]["cap"] == 19
        assert p._filter_stats["total"] == 25
        assert all(p._filter_outcome[e["ticker"]] == ("excluded", ["cap"]) for e in capped)
        assert {c.filter_status for c in cards} == {"passed"}
        assert p._filter_stats["filtered"] == 19

    def test_watchlist_names_bypass_the_gates_and_come_first_under_the_cap(self, monkeypatch):
        monkeypatch.setattr(pl, "MAX_DEEP_CHECKS_PER_DAY", 3)
        ts = _tickers(25)
        scr = {t: _strong() for t in ts}
        scr["T24"] = _weak()
        p, uw, cards = _run(ts, scr, watchlist={"T24"})
        assert "T24" in {c.ticker for c in cards}
        assert p._filter_outcome["T24"][0] == "bypassed"

    def test_a_small_run_is_not_filtered(self):
        ts = _tickers(5)
        scr = {t: _weak() for t in ts}
        p, uw, cards = _run(ts, scr)
        assert len(cards) == 5


class TestOneEarningsCallPerTicker:
    def test_score_reads_earnings_once(self):
        ts = _tickers(3)
        scr = {t: _strong() for t in ts}
        uw = _CountingUW()
        p = EarningsPipeline(ts, uw=uw, cal_meta={t: {"earnings_date": "2026-08-14", "expected_move_perc": 0.05} for t in ts})
        p._screener_cache = scr
        p._score("T00")
        assert uw.rows_calls == ["T00"] and uw.legacy_calls == 0

    def test_clients_without_the_combined_read_still_work(self):
        # Fakes and older clients keep the two-method read.
        fake = _FakeUW(earnings_data={"price": "10"}, earnings_history=[])
        p = EarningsPipeline(["AAA"], uw=fake, cal_meta={"AAA": {"earnings_date": "2026-08-14", "expected_move_perc": 0.05}})
        p._screener_cache = {"AAA": {"close": 10.0}}
        assert p._score("AAA").ticker == "AAA"


class TestBoundedScoringPool:
    def test_scoring_runs_concurrently_but_never_beyond_the_bound(self, monkeypatch):
        monkeypatch.setattr(pl, "SCORE_WORKERS", 3)
        ts = _tickers(24)
        uw = _CountingUW(delay=0.02)
        p, uw, cards = _run(ts, {t: _strong() for t in ts}, uw=uw)
        assert len(cards) == 24
        assert 1 < uw.peak <= 3

    def test_one_workers_is_serial(self, monkeypatch):
        monkeypatch.setattr(pl, "SCORE_WORKERS", 1)
        ts = _tickers(22)
        uw = _CountingUW(delay=0.005)
        p, uw, cards = _run(ts, {t: _strong() for t in ts}, uw=uw)
        assert len(cards) == 22 and uw.peak == 1

    def test_a_failing_ticker_does_not_drop_the_others(self):
        ts = _tickers(22)
        uw = _CountingUW(boom={"T03"})
        p, uw, cards = _run(ts, {t: _strong() for t in ts}, uw=uw)
        assert {c.ticker for c in cards} == set(ts) - {"T03"}

    def test_card_order_does_not_depend_on_completion_order(self):
        ts = _tickers(22)
        uw = _CountingUW(delay=0.003)
        _, _, cards = _run(ts, {t: _strong() for t in ts}, uw=uw)
        keys = [(-(c.confidence_score + c.pre_score), -c.expected_move_pct) for c in cards]
        assert keys == sorted(keys)


class TestProviderEarningsRows:
    def test_one_request_serves_the_event_and_the_history(self):
        from RevenueChaser.core.providers.result import ProviderResult
        from RevenueChaser.core.providers.unusual_whales import UWClient
        calls = []
        uw = UWClient.__new__(UWClient)
        uw.get_result = lambda ep, params=None: calls.append(ep) or ProviderResult(
            "ok", {"data": [{"a": 1}, {"a": 2}]}, None, ep)
        assert uw.earnings_rows("AAPL") == [{"a": 1}, {"a": 2}]
        assert uw.earnings_data("AAPL") == {"a": 1}
        assert uw.earnings_history("AAPL") == [{"a": 1}, {"a": 2}]
        assert calls == ["/earnings/AAPL"] * 3


class _LeapsCountingUW(_CountingUW):
    """Counts the LEAPS reads the scoring pool makes; C26 expects none."""

    def __init__(self, **ov):
        super().__init__(**ov)
        self.leaps_calls = []

    def oi_per_expiry(self, ticker):
        self.leaps_calls.append(ticker)
        return []

    def flow_per_expiry(self, ticker):
        self.leaps_calls.append(ticker)
        return []

    def iv_rank(self, ticker):
        self.leaps_calls.append(ticker)
        return []


class TestLeapsIsReadOnly:
    """C26 (#383, LP-D): the Earnings scoring pool never fetches or writes LEAPS.

    A cache miss is answered "N/A" locally — no UW call, no lazy store write.
    """

    def test_a_cache_miss_makes_no_uw_call_and_stays_n_a(self, monkeypatch):
        monkeypatch.setattr(pl, "leaps_latest", lambda tickers: {})
        ts = _tickers(8)
        uw = _LeapsCountingUW()
        _, _, cards = _run(ts, {t: _strong() for t in ts}, uw=uw)
        assert len(cards) == 8
        assert uw.leaps_calls == []                     # no LEAPS UW read at all
        assert {c.leaps_label for c in cards} == {"N/A"}
        assert {c.leaps_as_of for c in cards} == {""}
