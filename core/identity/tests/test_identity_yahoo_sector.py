"""Yahoo sector adapter: paced, backoff on rate limits, stops after 5 in a row."""
from __future__ import annotations

import pytest

from RevenueChaser.core.identity.sources import yahoo_sector


class RateLimited(Exception):
    pass


def good(sector="Technology", industry="Consumer Electronics"):
    return {"sector": sector, "industry": industry, "quoteType": "EQUITY"}


class Fake:
    """info_fn double: per-ticker behaviour is a value, an exception, or a list consumed per call."""

    def __init__(self, script):
        self.script = {k: (list(v) if isinstance(v, list) else v) for k, v in script.items()}
        self.calls = []

    def __call__(self, ticker):
        self.calls.append(ticker)
        v = self.script.get(ticker, good())
        if isinstance(v, list):
            v = v.pop(0) if len(v) > 1 else v[0]
        if isinstance(v, Exception):
            raise v
        return v


def run(tickers, script=None, **kw):
    sleeps = []
    fake = Fake(script or {})
    r = yahoo_sector.sectors(tickers, info_fn=fake, sleep=sleeps.append, rand=lambda a, b: 0.25, **kw)
    return r, fake, sleeps


def test_happy_path_returns_sector_and_industry_for_the_given_tickers_only():
    r, fake, _ = run(["AAPL", "OZK"], {"OZK": good("Financial Services", "Banks - Regional")})
    assert r.source == "yahoo_sector" and r.status == "ok"
    by = {x["ticker"]: x for x in r.rows}
    assert set(by) == {"AAPL", "OZK"} and fake.calls == ["AAPL", "OZK"]
    assert by["OZK"]["sector"] == "Financial Services" and by["OZK"]["industry"] == "Banks - Regional"
    assert by["AAPL"]["status"] == "ok" and by["AAPL"]["quote_type"] == "EQUITY"


def test_no_sector_is_a_no_data_row():
    r, _, _ = run(["LAZR"], {"LAZR": {"quoteType": "ETF"}})
    assert r.status == "ok" and r.rows[0]["status"] == "no_data" and r.rows[0]["sector"] is None


def test_calls_are_paced_with_jitter_and_not_before_the_first():
    _, _, sleeps = run(["A", "B", "C"])
    assert sleeps == [1.45, 1.45]                      # 1.2 + jitter(0.25), between calls only


def test_rate_limit_backs_off_10_30_90_then_gives_up_on_the_ticker():
    r, fake, sleeps = run(["X"], {"X": RateLimited("429 Too Many Requests")})
    assert [s for s in sleeps if s >= 10] == [10, 30, 90]
    assert fake.calls == ["X", "X", "X"]
    assert r.rows[0]["status"] == "error" and "rate" in r.rows[0]["error"]


def test_rate_limit_then_success_keeps_the_answer():
    r, _, sleeps = run(["X"], {"X": [RateLimited("Too Many Requests"), good("Energy", "Oil & Gas E&P")]})
    assert r.rows[0]["status"] == "ok" and r.rows[0]["sector"] == "Energy"
    assert 10 in sleeps and 30 not in sleeps


def test_stops_after_five_consecutive_rate_limited_tickers_and_keeps_partial_rows():
    tickers = [f"T{i}" for i in range(12)]
    script = {t: RateLimited("429") for t in tickers[2:]}          # first two succeed
    r, fake, _ = run(tickers, script)
    assert r.status == "error" and "5 consecutive" in r.error
    assert [x["ticker"] for x in r.rows if x["status"] == "ok"] == ["T0", "T1"]
    assert len(r.rows) == 7                                         # 2 ok + 5 rate-limited; the rest untried
    assert set(fake.calls) == set(tickers[:7]) and "T7" not in fake.calls


def test_a_success_resets_the_consecutive_counter():
    tickers = [f"T{i}" for i in range(12)]
    script = {t: RateLimited("429") for t in tickers}
    for ok in ("T4", "T9"):
        script[ok] = good()
    r, _, _ = run(tickers, script)
    assert r.status == "ok" and len(r.rows) == 12 and r.error is None
    assert sum(1 for x in r.rows if x["status"] == "ok") == 2


def test_stop_threshold_is_five_by_default_and_a_parameter():
    tickers = [f"T{i}" for i in range(6)]
    script = {t: RateLimited("429") for t in tickers}
    r, _, _ = run(tickers, script, stop_after=2)
    assert r.status == "error" and len(r.rows) == 2


def test_other_errors_do_not_count_as_rate_limits_and_are_redacted(monkeypatch):
    monkeypatch.setenv("OPENFIGI_API_KEY", "figi-test-key")
    r, _, sleeps = run(["A", "B"], {"A": ValueError("boom figi-test-key")})
    assert r.status == "ok"
    assert r.rows[0]["status"] == "error" and "figi-test-key" not in r.rows[0]["error"]
    assert r.rows[1]["status"] == "ok" and all(s < 10 for s in sleeps)


@pytest.mark.parametrize("exc,expected", [
    (RateLimited("429 Client Error"), True),
    (Exception("Too Many Requests. Rate limited."), True),
    (type("YFRateLimitError", (Exception,), {})("x"), True),
    (ValueError("nope"), False),
])
def test_is_rate_limit(exc, expected):
    assert yahoo_sector.is_rate_limit(exc) is expected


def test_default_info_function_goes_through_yfinance(monkeypatch):
    class FakeTicker:
        def __init__(self, t):
            self.info = {"sector": "Utilities", "industry": "Utilities - Regulated Electric"}

    monkeypatch.setattr("yfinance.Ticker", FakeTicker)
    r = yahoo_sector.sectors(["DUK"], sleep=lambda s: None)
    assert r.rows[0]["sector"] == "Utilities"


def test_no_tickers_is_ok_and_empty():
    r, fake, _ = run([])
    assert r.status == "ok" and r.rows == [] and fake.calls == []
