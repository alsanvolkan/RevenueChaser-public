"""Quotes and market cap per session (D04, #294): ``quote_snapshot``, Yahoo first, UW beside.

Every test is offline: a fake UW client stands in for the screener and the universe pull runs on
the fake ``yf.screen`` / ``yf.Ticker`` the AG tests use. The clock is passed explicitly, or the
calendar seam is patched for the universe pull.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar, quotes, rawstore
from RevenueChaser.core import universe_fetch as uf

UTC = timezone.utc
AFTER_CLOSE = datetime(2026, 10, 8, 21, 0, tzinfo=UTC)     # 17:00 EDT Thursday: session 2026-10-08, complete
NEXT_DAY = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)        # 17:00 EDT Friday: session 2026-10-09
INTRADAY = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)        # 11:00 EDT: the session is still open
PRE_OPEN_MONDAY = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)  # 08:00 EDT Monday: Friday's session
SESSION = date(2026, 10, 8)
BEFORE_OPEN = datetime(2026, 10, 8, 13, 29, tzinfo=UTC)    # 09:29 EDT Thursday: Wednesday's session, complete
AFTER_OPEN = datetime(2026, 10, 8, 13, 31, tzinfo=UTC)     # 09:31 EDT: the session has opened


# ── fakes and helpers ────────────────────────────────────────────────────────

class FakeUW:
    """``screener_batch`` and ``health`` as the UW client has them; one call per 100 tickers."""

    def __init__(self, items=None, auth_error=None):
        self.items = dict(items or {})
        self.auth_error = auth_error
        self.breaker_open = False
        self.calls: list[list[str]] = []
        self.raise_on_call = False

    def screener_batch(self, tickers, date=None):
        out = {}
        for i in range(0, len(tickers), 100):
            chunk = list(tickers[i:i + 100])
            self.calls.append(chunk)
            if self.raise_on_call:
                raise RuntimeError("screener down")
            for t in chunk:
                if t in self.items:
                    out[t] = dict(self.items[t], ticker=t)
        return out

    def health(self):
        return {"calls": len(self.calls), "breaker_open": self.breaker_open,
                "auth_error": self.auth_error}


def uw_item(cap, close=10.0, hi=12.0, lo=8.0):
    return {"marketcap": None if cap is None else str(cap), "close": str(close),
            "week_52_high": str(hi), "week_52_low": str(lo), "date": "2026-10-08"}


def yahoo_quote(ticker, cap=1.0e9, price=10.0, shares=1.0e8, hi=12.0, lo=8.0):
    q = {"symbol": ticker, "quoteType": "EQUITY", "exchange": "NMS", "currency": "USD",
         "regularMarketPrice": price, "sharesOutstanding": shares,
         "fiftyTwoWeekHigh": hi, "fiftyTwoWeekLow": lo, "shortName": f"{ticker} Inc"}
    if cap is not None:
        q["marketCap"] = cap
    return q


def seed_universe(*tickers):
    rawstore.write_rows("equity_universe", [{"ticker": t, "name": t} for t in tickers])


def snapshot(source=None):
    rows = rawstore.get_all("quote_snapshot")
    return sorted((r for r in rows if source is None or r["source"] == source),
                  key=lambda r: (r["ticker"], r["session_date"], r["source"]))


# ── the session a run writes for ─────────────────────────────────────────────

def test_a_run_after_the_close_writes_that_session():
    assert quotes.snapshot_session(AFTER_CLOSE) == (SESSION, None)


def test_a_run_before_the_open_writes_the_previous_session():
    assert quotes.snapshot_session(PRE_OPEN_MONDAY) == (date(2026, 10, 9), None)


def test_an_intraday_run_has_no_session_to_write():
    assert quotes.snapshot_session(INTRADAY) == (None, "intraday")


def test_a_clock_past_the_holiday_table_is_a_calendar_skip_not_an_error():
    assert quotes.snapshot_session(datetime(2029, 1, 3, 21, 0, tzinfo=UTC)) == (None, "calendar")


# ── Yahoo rows ───────────────────────────────────────────────────────────────

def test_a_yahoo_quote_becomes_one_row_with_its_values_and_payload(rawstore_tmp_db):
    res = quotes.record_yahoo([{"ticker": "AAA", "quote": yahoo_quote("AAA", cap=2.5e9, price=25.0)}],
                              now=AFTER_CLOSE, run_id="r1")
    assert res["rows_inserted"] == 1 and res["session"] == "2026-10-08"
    (row,) = snapshot()
    assert (row["ticker"], row["session_date"], row["source"]) == ("AAA", SESSION, "yfinance")
    assert (row["market_cap"], row["price"], row["shares_outstanding"]) == (2.5e9, 25.0, 1.0e8)
    assert (row["week_52_high"], row["week_52_low"]) == (12.0, 8.0)
    assert row["payload"]["symbol"] == "AAA", "the whole vendor quote is kept, compressed"
    assert row["run_id"] == "r1" and row["fetched_at"] == AFTER_CLOSE.timestamp()


def test_a_second_run_in_one_session_adds_no_row(rawstore_tmp_db):
    quotes.record_yahoo([{"ticker": "AAA", "quote": yahoo_quote("AAA", cap=1e9)}], now=AFTER_CLOSE, run_id="r1")
    res = quotes.record_yahoo([{"ticker": "AAA", "quote": yahoo_quote("AAA", cap=9e9)}], now=AFTER_CLOSE,
                              run_id="r2")
    assert res["rows_inserted"] == 0
    (row,) = snapshot()
    assert row["market_cap"] == 1e9 and row["run_id"] == "r1", "the first write of a session wins"


def test_the_next_session_adds_its_own_row(rawstore_tmp_db):
    quotes.record_yahoo([{"ticker": "AAA", "quote": yahoo_quote("AAA", cap=1e9)}], now=AFTER_CLOSE)
    quotes.record_yahoo([{"ticker": "AAA", "quote": yahoo_quote("AAA", cap=2e9)}], now=NEXT_DAY)
    assert [(r["session_date"], r["market_cap"]) for r in snapshot()] == [
        (SESSION, 1e9), (date(2026, 10, 9), 2e9)]


def test_a_quote_without_a_cap_writes_no_row_so_a_later_run_can_fill_it(rawstore_tmp_db):
    res = quotes.record_yahoo([{"ticker": "AAA", "quote": yahoo_quote("AAA", cap=None)},
                               {"ticker": "BBB", "quote": yahoo_quote("BBB", cap=0)}], now=AFTER_CLOSE)
    assert res["rows_inserted"] == 0 and res["no_cap"] == 2
    assert snapshot() == []
    quotes.record_fast_info([{"ticker": "AAA", "shares": 1e8, "price": 12.0}], now=AFTER_CLOSE)
    (row,) = snapshot()
    assert row["market_cap"] == pytest.approx(1.2e9) and row["price"] == 12.0
    assert row["shares_outstanding"] == 1e8
    assert row["payload"] == {"via": "fast_info", "shares": 1e8, "last_price": 12.0}


def test_an_intraday_run_writes_nothing(rawstore_tmp_db):
    res = quotes.record_yahoo([{"ticker": "AAA", "quote": yahoo_quote("AAA")}], now=INTRADAY)
    assert res["skipped"] == "intraday" and res["rows_inserted"] == 0
    assert snapshot() == []


def test_a_failed_write_is_reported_and_never_raised(rawstore_tmp_db, monkeypatch):
    def boom(name, rows):
        raise RuntimeError("store busy")
    monkeypatch.setattr(rawstore, "write_rows", boom)
    res = quotes.record_yahoo([{"ticker": "AAA", "quote": yahoo_quote("AAA")}], now=AFTER_CLOSE)
    assert res["errors"] == ["write"] and res["rows_inserted"] == 0


# ── UW rows ──────────────────────────────────────────────────────────────────

def test_both_sources_get_a_row(rawstore_tmp_db):
    seed_universe("AAA")
    quotes.record_yahoo([{"ticker": "AAA", "quote": yahoo_quote("AAA", cap=1.00e9)}], now=AFTER_CLOSE)
    uw = FakeUW({"AAA": uw_item(1.02e9, close=10.2)})
    res = quotes.ensure_quote_snapshot(uw=uw, now=AFTER_CLOSE, run_id="r1")
    assert res["rows_inserted"] == 1 and res["uw_calls"] == 1
    assert [(r["source"], r["market_cap"]) for r in snapshot()] == [("uw", 1.02e9), ("yfinance", 1.0e9)]
    uw_row = snapshot("uw")[0]
    assert (uw_row["price"], uw_row["week_52_high"], uw_row["week_52_low"]) == (10.2, 12.0, 8.0)
    assert uw_row["shares_outstanding"] is None
    assert uw_row["payload"]["marketcap"] == "1020000000.0"


def test_uw_is_asked_in_batches_of_100_over_the_universe(rawstore_tmp_db):
    tickers = [f"T{i:03d}" for i in range(250)]
    seed_universe(*tickers)
    uw = FakeUW({t: uw_item(1e9) for t in tickers})
    res = quotes.ensure_quote_snapshot(uw=uw, now=AFTER_CLOSE)
    assert [len(c) for c in uw.calls] == [100, 100, 50]
    assert res["scope"] == 250 and res["rows_inserted"] == 250 and res["uw_calls"] == 3


def test_a_second_uw_run_in_one_session_asks_only_the_missing_tickers(rawstore_tmp_db):
    seed_universe("AAA", "BBB")
    quotes.ensure_quote_snapshot(uw=FakeUW({"AAA": uw_item(1e9)}), now=AFTER_CLOSE)
    uw = FakeUW({"AAA": uw_item(5e9), "BBB": uw_item(2e9)})
    res = quotes.ensure_quote_snapshot(uw=uw, now=AFTER_CLOSE)
    assert uw.calls == [["BBB"]]
    assert res["rows_inserted"] == 1
    assert [(r["ticker"], r["market_cap"]) for r in snapshot("uw")] == [("AAA", 1e9), ("BBB", 2e9)]


def test_a_uw_row_with_no_cap_is_counted_not_written(rawstore_tmp_db):
    seed_universe("AAA", "BBB")
    res = quotes.ensure_quote_snapshot(uw=FakeUW({"AAA": uw_item(None)}), now=AFTER_CLOSE)
    assert res["no_cap"] == 1 and res["not_returned"] == 1 and res["rows_inserted"] == 0


def test_uw_rows_match_the_universe_spelling(rawstore_tmp_db):
    seed_universe("BRK-B")

    class DotUW(FakeUW):
        def screener_batch(self, tickers, date=None):
            self.calls.append(list(tickers))
            return {"BRK.B": dict(uw_item(9e11), ticker="BRK.B")}
    res = quotes.ensure_quote_snapshot(uw=DotUW(), now=AFTER_CLOSE)
    assert res["rows_inserted"] == 1 and snapshot("uw")[0]["ticker"] == "BRK-B"


def test_an_intraday_uw_run_makes_no_call(rawstore_tmp_db):
    seed_universe("AAA")
    uw = FakeUW({"AAA": uw_item(1e9)})
    res = quotes.ensure_quote_snapshot(uw=uw, now=INTRADAY)
    assert uw.calls == [] and res["skipped"] == "intraday"


def test_a_rejected_uw_key_stops_before_any_call(rawstore_tmp_db):
    seed_universe("AAA")
    uw = FakeUW({"AAA": uw_item(1e9)}, auth_error={"status": 401})
    res = quotes.ensure_quote_snapshot(uw=uw, now=AFTER_CLOSE)
    assert uw.calls == [] and res["errors"] == ["uw_auth"]


def test_an_open_breaker_stops_the_remaining_batches(rawstore_tmp_db):
    tickers = [f"T{i:03d}" for i in range(250)]
    seed_universe(*tickers)

    class Tripping(FakeUW):
        def screener_batch(self, tickers, date=None):
            out = super().screener_batch(tickers, date)
            self.breaker_open = True
            return out
    uw = Tripping({t: uw_item(1e9) for t in tickers})
    res = quotes.ensure_quote_snapshot(uw=uw, now=AFTER_CLOSE)
    assert len(uw.calls) == 1 and res["rows_inserted"] == 100 and res["errors"] == ["uw_breaker"]


def test_a_raising_screener_is_an_error_not_an_exception(rawstore_tmp_db):
    seed_universe("AAA")
    uw = FakeUW({"AAA": uw_item(1e9)})
    uw.raise_on_call = True
    res = quotes.ensure_quote_snapshot(uw=uw, now=AFTER_CLOSE)
    assert res["errors"] == ["uw_failed"] and res["rows_inserted"] == 0


class Clock:
    """The calendar seam as a clock a test can move while a run is going."""

    def __init__(self, monkeypatch, at):
        self.at = at
        monkeypatch.setattr(calendar, "_now", lambda: self.at)


def test_a_uw_batch_that_comes_back_after_the_open_is_not_written(rawstore_tmp_db, monkeypatch):
    seed_universe(*[f"T{i:03d}" for i in range(250)])
    clock = Clock(monkeypatch, BEFORE_OPEN)

    class Slow(FakeUW):
        def screener_batch(self, tickers, date=None):
            out = super().screener_batch(tickers, date)
            clock.at = AFTER_OPEN               # the call was asked at 09:29 and answered at 09:31
            return out
    uw = Slow({f"T{i:03d}": uw_item(1e9) for i in range(250)})
    res = quotes.ensure_quote_snapshot(uw=uw)
    assert res["session"] == "2026-10-07"
    assert len(uw.calls) == 1, "no batch is asked once the session has opened"
    assert res["rows_inserted"] == 0 and snapshot() == [], "values served after 09:30 are intraday"
    assert res["errors"] == ["session_moved"]


def test_a_uw_run_crossing_the_open_keeps_its_earlier_batches_and_asks_no_more(rawstore_tmp_db, monkeypatch):
    seed_universe(*[f"T{i:03d}" for i in range(250)])
    clock = Clock(monkeypatch, BEFORE_OPEN)
    real_write = quotes.write_snapshot

    def write_then_open(rows):
        n = real_write(rows)
        clock.at = AFTER_OPEN                   # the open passes between batch 1 and batch 2
        return n
    monkeypatch.setattr(quotes, "write_snapshot", write_then_open)
    uw = FakeUW({f"T{i:03d}": uw_item(1e9) for i in range(250)})
    res = quotes.ensure_quote_snapshot(uw=uw)
    assert len(uw.calls) == 1 and res["rows_inserted"] == 100 and res["errors"] == ["session_moved"]
    assert {r["session_date"] for r in snapshot()} == {date(2026, 10, 7)}


def test_a_cancel_stops_the_uw_side_between_batches(rawstore_tmp_db):
    from RevenueChaser.core import job_control
    seed_universe(*[f"T{i:03d}" for i in range(250)])
    token = job_control.StopToken()

    class Cancelling(FakeUW):
        def screener_batch(self, tickers, date=None):
            token.cancel()
            return super().screener_batch(tickers, date)
    uw = Cancelling({f"T{i:03d}": uw_item(1e9) for i in range(250)})
    with job_control.bound(token), pytest.raises(job_control.JobCancelled):
        quotes.ensure_quote_snapshot(uw=uw, now=AFTER_CLOSE)
    assert len(uw.calls) == 1 and len(snapshot("uw")) == 100, "the batch already answered is kept"


def test_a_breaker_that_opens_on_the_last_batch_is_reported(rawstore_tmp_db):
    seed_universe("AAA", "BBB")

    class Tripping(FakeUW):
        def screener_batch(self, tickers, date=None):
            out = super().screener_batch(tickers, date)
            self.breaker_open = True
            return out
    res = quotes.ensure_quote_snapshot(uw=Tripping({"AAA": uw_item(1e9), "BBB": uw_item(2e9)}), now=AFTER_CLOSE)
    assert res["rows_inserted"] == 2 and res["errors"] == ["uw_breaker"]


def test_a_uw_cap_of_zero_or_below_is_no_cap(rawstore_tmp_db):
    seed_universe("AAA", "BBB")
    res = quotes.ensure_quote_snapshot(uw=FakeUW({"AAA": uw_item(0), "BBB": uw_item(-5e8)}), now=AFTER_CLOSE)
    assert res["no_cap"] == 2 and res["rows_inserted"] == 0 and snapshot() == []


def _uw_row(ticker, cap, session=SESSION):
    return quotes._row(ticker, session, quotes.SOURCE_UW, cap=cap, price=None, shares=None, high=None, low=None,
                       payload={}, fetched_at=1.0, run_id="r", run_at=1.0)


def test_a_key_twice_in_one_batch_is_written_once_first_one_wins(rawstore_tmp_db):
    assert quotes.write_snapshot([_uw_row("AAA", 1e9), _uw_row("AAA", 2e9)]) == 1
    assert [r["market_cap"] for r in snapshot()] == [1e9]


def test_a_stored_key_past_the_first_thousand_keeps_its_row(rawstore_tmp_db):
    quotes.write_snapshot([_uw_row("T1200", 1.0)])
    rows = [_uw_row(f"T{i:04d}", 2.0) for i in range(1500)]
    assert quotes.write_snapshot(rows) == 1499
    assert rawstore.query("SELECT market_cap FROM quote_snapshot WHERE ticker = 'T1200'")[0]["market_cap"] == 1.0


# ── the read side (the quote half of the D6 view) ────────────────────────────

def test_the_latest_cap_is_yahoos_when_both_sources_have_one(rawstore_tmp_db):
    seed_universe("AAA", "BBB")
    quotes.record_yahoo([{"ticker": "AAA", "quote": yahoo_quote("AAA", cap=1.0e9)}], now=AFTER_CLOSE)
    quotes.ensure_quote_snapshot(uw=FakeUW({"AAA": uw_item(1.3e9), "BBB": uw_item(4e9)}), now=AFTER_CLOSE)
    caps = quotes.latest_caps()
    assert caps["AAA"]["market_cap"] == 1.0e9 and caps["AAA"]["source"] == "yfinance"
    assert caps["BBB"]["market_cap"] == 4e9 and caps["BBB"]["source"] == "uw", "UW only where Yahoo has none"
    assert caps["AAA"]["session_date"] == SESSION


def test_the_latest_cap_is_the_newest_sessions(rawstore_tmp_db):
    quotes.record_yahoo([{"ticker": "AAA", "quote": yahoo_quote("AAA", cap=1e9)}], now=AFTER_CLOSE)
    seed_universe("AAA")
    quotes.ensure_quote_snapshot(uw=FakeUW({"AAA": uw_item(3e9)}), now=NEXT_DAY)
    caps = quotes.latest_caps(["AAA", "ZZZ"])
    assert caps == {"AAA": {"market_cap": 3e9, "price": 10.0, "source": "uw",
                            "session_date": date(2026, 10, 9)}}


# ── the universe pull writes Yahoo's rows (Phase A, Phase B) ─────────────────

@pytest.fixture
def pull(monkeypatch, rawstore_tmp_db):
    """A one-chunk universe pull on fake yfinance, with the clock after the close."""
    import yfinance as yf
    from RevenueChaser.core.identity.sources import SourceResult, sec

    monkeypatch.setattr(calendar, "_now", lambda: AFTER_CLOSE)
    monkeypatch.setattr(uf, "MIN_FULL_UNIVERSE", 1)
    monkeypatch.setattr(uf, "MC_CHUNKS", [(0, 1e13)])
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    def run(members, screen_quotes, fast=None, during_screen=None):
        rows = [{"ticker": t, "cik": str(i), "name": t, "exchange": "Nasdaq"} for i, t in enumerate(members)]
        monkeypatch.setattr(sec, "exchange_map",
                            lambda *a, **k: SourceResult("sec_exchange", "ok", rows, None))

        def screen(query, offset=None, size=None, **kw):
            if during_screen:
                during_screen()
            start = offset or 0
            return {"total": len(screen_quotes), "quotes": screen_quotes[start:start + size]}
        monkeypatch.setattr(yf, "screen", screen)

        class FI:
            def __init__(self, shares, price):
                self.shares, self.last_price = shares, price

        class Ticker:
            def __init__(self, t):
                self.t = t

            @property
            def fast_info(self):
                return FI(*(fast or {}).get(self.t, (None, None)))
        monkeypatch.setattr(yf, "Ticker", Ticker)
        return uf.fetch_universe_stats(refresh=True, refreshed_by="Test")
    return run


def test_a_full_pull_writes_yahoos_quotes_and_the_phase_b_fills(pull):
    _, stats = pull(["AAA", "BBB", "CCC"],
                    [yahoo_quote("AAA", cap=1e9), yahoo_quote("BBB", cap=None, price=5.0)],
                    fast={"BBB": (2e8, 5.0), "CCC": (1e7, 3.0)})
    assert stats["quote_snapshot"]["rows_inserted"] == 1
    got = {r["ticker"]: (r["market_cap"], r["payload"].get("via")) for r in snapshot("yfinance")}
    assert got == {"AAA": (1e9, None), "BBB": (pytest.approx(1e9), "fast_info"),
                   "CCC": (pytest.approx(3e7), "fast_info")}
    assert all(r["session_date"] == SESSION for r in snapshot())


def test_a_partial_pull_writes_the_quotes_it_got(pull):
    seed_universe(*[f"S{i}" for i in range(10)])
    rawstore.update_columns("equity_universe", "ticker",
                            [{"ticker": f"S{i}", "quote": {"symbol": f"S{i}"}} for i in range(10)], ["quote"])
    _, stats = pull([f"S{i}" for i in range(10)], [yahoo_quote("S0", cap=7e8)])
    assert stats["partial"] is True
    assert [(r["ticker"], r["market_cap"]) for r in snapshot()] == [("S0", 7e8)]


def test_a_failing_quote_write_never_stops_the_universe_pull(pull, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("quote writer broke")
    monkeypatch.setattr(quotes, "write_snapshot", boom)
    rows, stats = pull(["AAA"], [yahoo_quote("AAA", cap=1e9)])
    assert [r["ticker"] for r in rows] == ["AAA"]
    assert rawstore.get("equity_universe", "AAA")["market_cap"] == 1e9
    assert stats["quote_snapshot"]["errors"] == ["write"]


def test_a_screener_name_not_on_the_member_list_gets_no_row(pull):
    pull(["AAA"], [yahoo_quote("AAA", cap=1e9), yahoo_quote("ZZZ", cap=2e9)])
    assert [r["ticker"] for r in snapshot()] == ["AAA"]


def test_a_pull_that_starts_inside_the_session_writes_no_quote_after_the_close(pull, monkeypatch):
    clock = {"at": datetime(2026, 10, 8, 20, 20, tzinfo=UTC)}     # 16:20 EDT: still inside the session
    monkeypatch.setattr(calendar, "_now", lambda: clock["at"])

    def close_passes():
        clock["at"] = AFTER_CLOSE                               # the screener answers after 16:30
    _, stats = pull(["AAA"], [yahoo_quote("AAA", cap=1e9)], during_screen=close_passes)
    assert stats["quote_snapshot"]["skipped"] == "session_moved"
    assert snapshot() == []


def test_a_pull_that_crosses_the_open_writes_no_quote(pull, monkeypatch):
    clock = {"at": BEFORE_OPEN}
    monkeypatch.setattr(calendar, "_now", lambda: clock["at"])

    def open_passes():
        clock["at"] = AFTER_OPEN
    _, stats = pull(["AAA"], [yahoo_quote("AAA", cap=1e9)], during_screen=open_passes)
    assert stats["quote_snapshot"]["skipped"] == "session_moved"
    assert snapshot() == []


def test_phase_b_that_starts_inside_the_session_writes_no_quote_after_the_close(monkeypatch, rawstore_tmp_db):
    import yfinance as yf

    clock = {"at": datetime(2026, 10, 8, 20, 20, tzinfo=UTC)}     # 16:20 EDT: still inside the session
    monkeypatch.setattr(calendar, "_now", lambda: clock["at"])
    seed_universe("AAA")

    class FI:
        shares, last_price = 1e8, 10.0

    class Ticker:
        def __init__(self, t):
            clock["at"] = AFTER_CLOSE                           # the fill lands after 16:30

        fast_info = FI()
    monkeypatch.setattr(yf, "Ticker", Ticker)
    assert uf.backfill_universe_caps()["filled"] == 1
    assert rawstore.get("equity_universe", "AAA")["market_cap"] == 1e9, "the universe still gets its cap"
    assert snapshot() == []


def test_a_phase_b_rate_limit_stop_is_recorded_for_data_health(monkeypatch, rawstore_tmp_db):
    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    monkeypatch.setattr(calendar, "_now", lambda: AFTER_CLOSE)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    seed_universe(*[f"RL{i:02d}" for i in range(12)])

    class Ticker:
        def __init__(self, t):
            raise YFRateLimitError()
    monkeypatch.setattr(yf, "Ticker", Ticker)
    assert uf.backfill_universe_caps()["rate_limited"] is True
    assert quotes.health_summary()["rate_limit_stop"] == AFTER_CLOSE.timestamp()


# ── Data Health ──────────────────────────────────────────────────────────────

def test_health_counts_rows_per_source_against_the_universe(rawstore_tmp_db):
    seed_universe("AAA", "BBB", "CCC", "DDD")
    quotes.record_yahoo([{"ticker": t, "quote": yahoo_quote(t, cap=c)}
                         for t, c in (("AAA", 100.0), ("BBB", 100.0), ("CCC", 100.0))], now=AFTER_CLOSE)
    quotes.ensure_quote_snapshot(uw=FakeUW({"AAA": uw_item(105.0), "BBB": uw_item(105.01),
                                            "CCC": uw_item(94.0), "DDD": uw_item(1.0)}), now=AFTER_CLOSE)
    h = quotes.health_summary()
    assert h["newest_session"] == "2026-10-08" and h["universe"] == 4
    assert h["rows"] == {"yfinance": 3, "uw": 4}
    assert h["missing"] == {"yfinance": 1, "uw": 0}
    assert h["gaps"] == 2, "exactly 5% apart is not more than 5%"
    assert h["gap_examples"] == ["CCC", "BBB"], "largest gap first"
    assert h["rate_limit_stop"] is None


def test_health_on_an_empty_store(rawstore_tmp_db):
    seed_universe("AAA")
    h = quotes.health_summary()
    assert h["newest_session"] is None and h["rows"] == {"yfinance": 0, "uw": 0}
    assert h["missing"] == {"yfinance": 1, "uw": 1} and h["gaps"] == 0
