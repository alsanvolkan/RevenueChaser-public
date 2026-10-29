"""Tests for the cached OHLC loader (cache-first, no network when cached)."""
from __future__ import annotations

import time
import types
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_swing_potential import indicators


def _df(periods=300) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-01", periods=periods)
    close = pd.Series(100 + pd.Series(range(periods), index=idx) * 0.1, index=idx)
    return pd.DataFrame({
        "Open": close.shift(1).fillna(close.iloc[0]),
        "High": close * 1.01,
        "Low": close * 0.99,
        "Close": close,
        "Volume": 1_000_000,
    })


def _seed_ohlc(ticker: str, df: pd.DataFrame):
    indicators._write_ohlc(ticker, df)


def test_load_ohlc_uses_cache_without_network(rawstore_tmp_db, monkeypatch):
    _seed_ohlc("AAPL", _df())

    def boom(*a, **k):
        raise AssertionError("network must not be hit when cache is fresh")

    monkeypatch.setattr("yfinance.Ticker", boom)
    df = indicators.load_ohlc("AAPL")
    assert not df.empty
    assert "Close" in df.columns


def test_load_ohlc_fetches_on_cache_miss(rawstore_tmp_db, monkeypatch):
    calls = {"n": 0}
    fake = _df()

    class _T:
        def history(self, *a, **k):
            calls["n"] += 1
            return fake

    monkeypatch.setattr("yfinance.Ticker", lambda t: _T())
    df = indicators.load_ohlc("MSFT")
    assert calls["n"] == 1
    assert not df.empty
    assert rawstore.count("swing_ohlc") == len(fake)
    # now cached
    calls["n"] = 0
    indicators.load_ohlc("MSFT")
    assert calls["n"] == 0


def test_load_ohlc_no_fetch_miss_returns_empty(rawstore_tmp_db, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network must not be hit in no_fetch mode")

    monkeypatch.setattr("yfinance.Ticker", boom)
    assert indicators.load_ohlc("ZZZZ", no_fetch=True).empty
    # stale cache is still served in no_fetch mode (model-only, no network)
    _seed_ohlc("AAA", _df())
    df = indicators.load_ohlc("AAA", ttl_days=0.0, no_fetch=True)
    assert not df.empty


def test_load_ticker_info_no_fetch(rawstore_tmp_db, monkeypatch):
    assert indicators.load_ticker_info("ZZZZ", no_fetch=True) == {}
    rawstore.write_rows("swing_info", [{
        "ticker": "AAAA", "payload": {"sector": "Technology"}, "cached_at": 0.0,
    }])
    assert indicators.load_ticker_info("AAAA", no_fetch=True) == {"sector": "Technology"}


class TestScanProvenance:
    """``generated_at`` is when the scan RAN; the data date lives in
    core/provenance.py and is asserted in core/tests/test_provenance.py."""

    def test_payload_carries_the_data_date(self):
        from RevenueChaser.module_swing_potential.pipeline import build_payload
        p = build_payload([], run_stats={"data_as_of": "2026-08-31"})
        assert p["data_as_of"] == "2026-08-31"
        assert p["generated_at"][:10] != "2026-08-31"

    def test_data_as_of_delegates_to_provenance(self, rawstore_tmp_db):
        from RevenueChaser.core import rawstore
        from RevenueChaser.module_swing_potential import pipeline
        from RevenueChaser.core.tests.d01_seed import seed_d01
        assert pipeline.data_as_of() is None
        seed_d01("AAPL", ["2026-08-31"], 1.0)                 # the D01 date of a scored name (K7, #383 R3)
        rawstore.replace_all("swing_scores", [{"ticker": "AAPL"}])
        assert pipeline.data_as_of() == "2026-08-31"


# ── #128 F1: a cache that ends before the latest complete session is not fresh ─────────────────────
#
# ``load_ohlc`` reused any series younger than its TTL (1 day). A post-close run at 17:00 ET that
# started a few minutes before yesterday's write time reused bars ending D-1, and the ledger freeze
# then found no complete D bar. A Fetch now also refetches a series whose last bar is D or older (D is
# the latest complete session) and that was written before D's close + 30 min: the D bar of a
# mid-session Fetch is a partial bar (option A of #128, chosen by avo 2026-10-03).
#
# Since K8b (#383 R3) the freeze checks the picks' D01 bars (``core/tests/test_ledger_d01_only_383.py``):
# Swing and Fundamentals score from D01. ``load_ohlc`` still serves the SPY fallback and Co-movement, so
# these tests check that its cache ends with one complete D bar, by the same rule the freeze applies.

D = date(2026, 10, 2)                      # a Friday, EDT: close 20:00Z, close + 30 min = 20:30Z
RUN = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)         # 17:00 ET on D


def _utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _ending(last_day: date, periods: int = 30) -> pd.DataFrame:
    """``periods`` business days ending on ``last_day`` (which must itself be a business day)."""
    idx = pd.bdate_range(end=pd.Timestamp(last_day), periods=periods)
    close = pd.Series(range(periods), index=idx, dtype=float) + 100.0
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close,
                         "Volume": 1_000_000.0})


def _seed_at(ticker: str, last_day: date, written: datetime, periods: int = 30) -> pd.DataFrame:
    df = _ending(last_day, periods)
    rawstore.write_rows("swing_ohlc", [{
        "ticker": ticker, "ts": ts.isoformat(), "open": float(r["Open"]), "high": float(r["High"]),
        "low": float(r["Low"]), "close": float(r["Close"]), "volume": float(r["Volume"]),
        "cached_at": written.timestamp()} for ts, r in df.iterrows()])
    return df


def _cache_cause(ticker: str) -> str | None:
    """Why ``ticker``'s ``swing_ohlc`` cache lacks one complete D bar (None: it has one), by the freeze's rule. The
    cache's own reader (``ledger.last_bars``) left with the SPY fallback in PR 10; this is its query, kept here until
    PR 21 retires the cache."""
    rows = rawstore.query(
        "SELECT max(substr(ts, 1, 10)) AS last_day, arg_max(cached_at, ts) AS cached_at FROM swing_ohlc "
        "WHERE ticker = ? GROUP BY ticker", [ticker])
    return ledger._bar_cause(D, (rows[0]["last_day"], rows[0]["cached_at"]) if rows else None)


@pytest.fixture
def clock(monkeypatch):
    """Pin the market-calendar seam and ``time.time`` together: the TTL reads one, the rule the other."""
    def _set(now: datetime) -> None:
        monkeypatch.setattr(cal, "_now", lambda: now)
        monkeypatch.setattr(time, "time", lambda: now.timestamp())
    return _set


@pytest.fixture
def yahoo(monkeypatch):
    """A stand-in for ``yfinance.Ticker`` that counts calls. yfinance goes through curl_cffi, so a
    socket patch would not stop it: patch the entry points."""
    state = types.SimpleNamespace(calls=[], frame=None, fail=False, fail_for=set())

    class _T:
        def __init__(self, t):
            self.t = t

        def history(self, *a, **k):
            state.calls.append(self.t)
            if state.fail or self.t in state.fail_for:
                raise RuntimeError("Too Many Requests. Rate limited.")
            return state.frame

    def no_download(*a, **k):
        raise AssertionError("yfinance.download was called")

    monkeypatch.setattr("yfinance.Ticker", _T)
    monkeypatch.setattr("yfinance.download", no_download)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return state


def test_f1_a_series_ending_the_day_before_is_refetched_after_the_close(rawstore_tmp_db, clock, yahoo):
    """The repro: bars ending D-1, written 23 h 57 min before a 17:00 ET run on D. The 1-day TTL
    called them fresh; they cannot hold D, so the Fetch refetches and the ledger sees a D bar."""
    clock(RUN)
    _seed_at("AAA", date(2026, 10, 1), RUN - pd.Timedelta(hours=23, minutes=57))
    yahoo.frame = _ending(D)
    df = indicators.load_ohlc("AAA")
    assert yahoo.calls == ["AAA"]
    assert df.index[-1] == pd.Timestamp(D)
    assert not df.attrs.get("stale")
    assert _cache_cause("AAA") is None


@pytest.mark.parametrize("written", [
    _utc(2026, 10, 1, 21, 3),        # 23 h 57 min before the run: D-1's own post-close write
    _utc(2026, 10, 2, 10, 0),        # 06:00 ET on D, before the open
    _utc(2026, 10, 2, 20, 29),       # one minute short of close + 30 min
])
def test_f1_any_write_before_the_close_plus_30_with_an_older_last_bar_is_refetched(
        rawstore_tmp_db, clock, yahoo, written):
    clock(RUN)
    _seed_at("AAA", date(2026, 10, 1), written)
    yahoo.frame = _ending(D)
    indicators.load_ohlc("AAA")
    assert yahoo.calls == ["AAA"]


def test_f1_a_series_written_after_the_close_plus_30_with_an_older_last_bar_is_not_refetched(
        rawstore_tmp_db, clock, yahoo):
    """A halted name: nothing newer exists, so a refetch would only burn a request every run."""
    clock(RUN)
    _seed_at("HALT", date(2026, 10, 1), _utc(2026, 10, 2, 20, 45))
    df = indicators.load_ohlc("HALT")
    assert yahoo.calls == [] and df.index[-1] == pd.Timestamp(2026, 10, 1)


@pytest.mark.parametrize("written", [
    _utc(2026, 10, 2, 15, 30),       # 11:30 ET on D: a mid-session Fetch cached a partial D bar
    _utc(2026, 10, 2, 19, 59),       # one minute before the close
    _utc(2026, 10, 2, 20, 29),       # one minute short of close + 30 min
])
def test_option_a_a_series_whose_last_bar_is_d_written_before_the_close_plus_30_is_refetched(
        rawstore_tmp_db, clock, yahoo, written):
    """Option A: the D bar a mid-session Fetch cached is partial. The post-close Fetch refetches it,
    and the ledger then sees one complete D bar."""
    clock(RUN)
    _seed_at("AAA", D, written)
    yahoo.frame = _ending(D)
    df = indicators.load_ohlc("AAA")
    assert yahoo.calls == ["AAA"] and df.index[-1] == pd.Timestamp(D)
    assert _cache_cause("AAA") is None
    indicators.load_ohlc("AAA")
    assert yahoo.calls == ["AAA"]                          # once: the refetch is a post-close write now


@pytest.mark.parametrize("written", [
    _utc(2026, 10, 2, 20, 30),       # exactly close + 30 min: complete
    _utc(2026, 10, 2, 20, 31),
    _utc(2026, 10, 2, 20, 45),
])
def test_option_a_a_series_whose_last_bar_is_d_written_after_the_close_plus_30_is_not_refetched(
        rawstore_tmp_db, clock, yahoo, written):
    clock(RUN)
    _seed_at("AAA", D, written)
    df = indicators.load_ohlc("AAA")
    assert yahoo.calls == [] and df.index[-1] == pd.Timestamp(D)


def test_option_a_a_mid_session_fetch_leaves_todays_partial_bar_alone(rawstore_tmp_db, clock, yahoo):
    """At 12:00 ET on D the latest complete session is D-1: a D bar is today's own, and is not stale."""
    clock(_utc(2026, 10, 2, 16, 0))
    _seed_at("AAA", D, _utc(2026, 10, 2, 15, 0))
    df = indicators.load_ohlc("AAA")
    assert yahoo.calls == [] and df.index[-1] == pd.Timestamp(D)


def test_option_a_refetches_only_the_tickers_whose_bars_were_cached_before_the_close_plus_30(
        rawstore_tmp_db, clock, yahoo):
    """Co-movement's bulk Fetch cached thousands of D bars mid-session. A scan loads only its own
    candidates, and of those only the partial ones are refetched."""
    clock(RUN)
    _seed_at("PARTIAL", D, _utc(2026, 10, 2, 17, 0))               # 13:00 ET on D
    _seed_at("COMPLETE", D, _utc(2026, 10, 2, 20, 45))
    _seed_at("HALTED", date(2026, 10, 1), _utc(2026, 10, 2, 20, 45))
    yahoo.frame = _ending(D)
    for t in ("PARTIAL", "COMPLETE", "HALTED"):
        indicators.load_ohlc(t)
    assert yahoo.calls == ["PARTIAL"]


def test_option_a_a_failed_refetch_of_a_partial_bar_still_serves_the_cache_stale(rawstore_tmp_db, clock, yahoo):
    clock(RUN)
    seeded = _seed_at("AAA", D, _utc(2026, 10, 2, 17, 0))
    yahoo.fail = True
    df = indicators.load_ohlc("AAA")
    assert df.attrs.get("stale") is True and len(df) == len(seeded)
    assert _cache_cause("AAA") == ledger.NEEDS_REFETCH                       # a later Fetch can complete it


@pytest.mark.parametrize("last_day, written", [
    (date(2026, 10, 1), RUN - pd.Timedelta(hours=23, minutes=57)),     # ends D-1: the F1 shape
    (D, _utc(2026, 10, 2, 17, 0)),                                     # ends D, cached mid-session: option A's
])
def test_f1_no_fetch_never_refetches_and_serves_the_cached_bars(rawstore_tmp_db, clock, yahoo,
                                                                 last_day, written):
    """Refresh makes zero network calls (AGENTS.md section 2): the rule is not evaluated for it."""
    clock(RUN)
    seeded = _seed_at("AAA", last_day, written)
    df = indicators.load_ohlc("AAA", no_fetch=True)
    assert yahoo.calls == []
    assert not df.empty and len(df) == len(seeded)
    assert list(df["Close"]) == list(seeded["Close"]) and df.index[-1] == pd.Timestamp(last_day)


def test_f1_no_fetch_does_not_even_read_the_calendar(rawstore_tmp_db, monkeypatch, yahoo):
    """The rule sits after the no_fetch return, so Refresh pays nothing for it."""
    _seed_at("AAA", date(2026, 10, 1), RUN)
    monkeypatch.setattr(ledger, "needs_session_refetch",
                        lambda *a, **k: pytest.fail("the staleness rule ran on a no_fetch call"))
    assert not indicators.load_ohlc("AAA", no_fetch=True).empty


def test_f1_a_failed_refetch_still_serves_the_stale_cache(rawstore_tmp_db, clock, yahoo):
    """#205 holds: stale beats empty. The failed refetch marks the frame stale, so the board's SPY /
    data-quality flags see it, and the freeze then refuses the day by name."""
    clock(RUN)
    seeded = _seed_at("AAA", date(2026, 10, 1), RUN - pd.Timedelta(hours=23, minutes=57))
    yahoo.fail = True
    df = indicators.load_ohlc("AAA")
    assert yahoo.calls == ["AAA"] * 3                                 # the three retries
    assert df.attrs.get("stale") is True and len(df) == len(seeded)
    assert df.index[-1] == pd.Timestamp(2026, 10, 1)


def test_f1_a_clock_past_the_holiday_table_falls_back_to_the_ttl(rawstore_tmp_db, clock, yahoo):
    """The calendar raises past 2028. load_ohlc must not: the TTL alone decides, as before #128."""
    now = _utc(2029, 3, 1, 22, 0)
    clock(now)
    _seed_at("AAA", date(2029, 2, 27), now - pd.Timedelta(hours=20))
    assert not indicators.load_ohlc("AAA").empty and yahoo.calls == []
    _seed_at("BBB", date(2029, 2, 27), now - pd.Timedelta(hours=30))
    yahoo.frame = _ending(date(2029, 2, 28))
    indicators.load_ohlc("BBB")
    assert yahoo.calls == ["BBB"]                                      # TTL expired: refetched as ever


def test_f1_the_ttl_still_expires_a_series_that_ends_on_d(rawstore_tmp_db, clock, yahoo):
    """The rule adds to the TTL; it does not replace it. SPY's 12 h TTL (ttl_days=0.5) still applies."""
    written = _utc(2026, 10, 2, 20, 31)                                # a post-close write of D
    _seed_at("SPY", D, written)
    clock(written + pd.Timedelta(hours=11))
    indicators.load_ohlc("SPY", ttl_days=0.5)
    assert yahoo.calls == []                                           # 11 h old: fresh
    clock(written + pd.Timedelta(hours=13))
    yahoo.frame = _ending(D)
    indicators.load_ohlc("SPY", ttl_days=0.5)
    assert yahoo.calls == ["SPY"]                                      # 13 h old: expired


def test_f1_the_default_ttl_is_still_one_day(rawstore_tmp_db, clock, yahoo):
    written = _utc(2026, 10, 2, 20, 31)
    _seed_at("AAA", D, written)
    clock(written + pd.Timedelta(hours=23))
    indicators.load_ohlc("AAA")
    assert yahoo.calls == []
    clock(written + pd.Timedelta(hours=25))
    yahoo.frame = _ending(D)
    indicators.load_ohlc("AAA")
    assert yahoo.calls == ["AAA"]


def test_f1_est_boundary_uses_the_est_close(rawstore_tmp_db, clock, yahoo):
    """D = Monday 2026-11-02 (EST): close 16:00 EST = 21:00Z, so close + 30 min = 21:30Z (not the EDT
    20:30Z). A series ending the Friday before and written at 20:30Z is refetched; one written at 21:45Z
    is not."""
    clock(_utc(2026, 11, 2, 22, 0))                                    # 17:00 EST on D
    _seed_at("EARLY", date(2026, 10, 30), _utc(2026, 11, 2, 20, 30))
    _seed_at("LATE", date(2026, 10, 30), _utc(2026, 11, 2, 21, 45))
    yahoo.frame = _ending(date(2026, 11, 2))
    indicators.load_ohlc("EARLY")
    indicators.load_ohlc("LATE")
    assert yahoo.calls == ["EARLY"]


def test_f1_the_rule_compares_dates_not_the_calendar_on_the_last_bar(rawstore_tmp_db, clock, yahoo):
    """Fixtures end in 2024 or 2025, before the holiday table starts; the last bar is never looked up in it."""
    clock(RUN)
    _seed_at("OLDFIX", date(2024, 5, 1), RUN)                          # written after D's close + 30: not stale
    assert not indicators.load_ohlc("OLDFIX").empty
    assert yahoo.calls == []


# ── option A on a whole scan: the refetch completes every cache, and a retry refetches only a failure ──
#
# Before K8b these tests froze the Swing board on the refetched cache. The freeze reads D01 now (C12
# scores from it; ``core/tests/test_ledger_d01_only_383.py`` freezes and refuses on D01), so they check
# the cache the loader leaves behind for its remaining readers (the SPY fallback, Co-movement).

def _scan(*tickers):
    """A scan that loads SPY with its 12 h TTL, then each name."""
    indicators.load_ohlc("SPY", ttl_days=0.5)
    for t in tickers:
        indicators.load_ohlc(t)


def test_option_a_a_scan_on_partial_bars_completes_every_cache(rawstore_tmp_db, clock, yahoo):
    """Before option A the post-close Fetch reused the 11:30 ET bars and the freeze skipped the day."""
    clock(RUN)
    partial = _utc(2026, 10, 2, 15, 30)
    for t in ("SPY", "AAA", "BBB"):
        _seed_at(t, D, partial)
    yahoo.frame = _ending(D)
    _scan("AAA", "BBB")
    assert sorted(yahoo.calls) == ["AAA", "BBB", "SPY"]
    assert [_cache_cause(t) for t in ("SPY", "AAA", "BBB")] == [None, None, None]


def test_option_a_one_failed_refetch_leaves_that_cache_partial_and_the_retry_refetches_only_it(
        rawstore_tmp_db, clock, yahoo):
    clock(RUN)
    partial = _utc(2026, 10, 2, 15, 30)
    for t in ("SPY", "AAA", "BBB"):
        _seed_at(t, D, partial)
    yahoo.frame = _ending(D)
    yahoo.fail_for = {"BBB"}                                     # a rate limit on one name
    _scan("AAA", "BBB")
    assert [_cache_cause(t) for t in ("SPY", "AAA", "BBB")] == [None, None, ledger.NEEDS_REFETCH]

    yahoo.calls.clear()
    yahoo.fail_for = set()                                       # the retry run, still after the close
    _scan("AAA", "BBB")
    assert yahoo.calls == ["BBB"]                                # SPY and AAA were completed by the first run
    assert _cache_cause("BBB") is None
