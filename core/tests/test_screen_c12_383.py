"""C12 (#383 R3 slice 2; #147 27-3b; card C, avo 2026-10-15): Swing and Fundamentals scan the whole universe.

- **7-A prefilter** (avo 2026-10-02): price and dollar volume only. The trend rules (52-week distance, above the
  50-day average) leave it; the gates test them on fresh bars. The inputs come from D04 ``quote_snapshot`` (the
  newest session's row, Yahoo first), not the universe quote; a name with no D04 price drops as ``no_quote``. The
  quotes' session is reported (U9).
- **No cap:** every prefiltered name is scored (``MAX_PHASE2`` 500 goes); **no TOP_N cut:** every candidate reaches
  the page and the ledger (card 6/7).
- **Bars from D01** through ``prices.daily_bars``: level rules read nominal bars, returns read adjusted ones (D3).
  No ``swing_ohlc`` read or write and no yfinance history call for a scored name.
- **The RS population** is every scored name with a 12-month return: the whole screenable universe.
- **Long lane** for both Fetch jobs.
Offline: a temp store, the calendar seam as the clock.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import rawstore, screen_common
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_swing_potential import indicators
from RevenueChaser.module_swing_potential import pipeline as sp

D = date(2026, 10, 14)
NOW = datetime(2026, 10, 14, 21, 45, tzinfo=timezone.utc)      # 17:45 EDT: the 14 Oct session is complete
FETCHED = datetime(2026, 10, 14, 20, 40, tzinfo=timezone.utc).timestamp()
CFG = SimpleNamespace(MIN_PRICE=5.0, MIN_DOLLAR_VOLUME=250_000.0)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(cal, "_now", lambda: NOW)


def _sessions(end: date, n: int) -> list[date]:
    return [d.date() for d in pd.bdate_range(end=end, periods=n)]


def _evening(d: date) -> float:
    return datetime(d.year, d.month, d.day, 21, 35, tzinfo=timezone.utc).timestamp()


def _seed_d01(ticker: str, days: list[date], close=None, actions=()):
    """Each bar as the nightly job stored it, on its own session's evening: a bar before a split is pre-split."""
    rawstore.write_rows("price_bars_daily", [{
        "ticker": ticker, "session_date": d, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
        "adj_close": None, "volume": 1.0e6, "split_ratio": None, "adj_source": None, "source": "uw",
        "fetched_at": _evening(d), "run_id": "seed", "run_at": _evening(d)}
        for i, d in enumerate(days) for c in [close(i) if close else 50.0 + 0.1 * i]])
    if actions:
        rawstore.write_rows("price_actions", [
            {"ticker": ticker, "ex_date": ex, "action": kind, "value": v, "session_date": ex,
             "source": "yfinance", "fetched_at": FETCHED, "run_id": "seed", "run_at": FETCHED}
            for ex, kind, v in actions])


def _q(ticker, session, price, *, v10=None, v3m=None, source="yfinance", payload=None):
    p = payload if payload is not None else {k: v for k, v in (
        ("regularMarketPrice", price), ("averageDailyVolume10Day", v10), ("averageDailyVolume3Month", v3m))
        if v is not None}
    return {"ticker": ticker, "session_date": session, "source": source, "market_cap": 1.0e9, "price": price,
            "payload": p, "fetched_at": 0.0, "run_id": "r", "run_at": 0.0}


def _row(ticker, **quote):
    return {"ticker": ticker, "quote": quote}


# ── the 7-A prefilter on D04 ──────────────────────────────────────────────────

def test_the_trend_rules_are_gone(rawstore_tmp_db):
    rawstore.write_rows("quote_snapshot", [_q("FAR", D, 20.0, v10=1e6)])
    row = _row("FAR", regularMarketPrice=20.0, fiftyTwoWeekHigh=40.0, fiftyDayAverage=25.0,
               averageDailyVolume10Day=1e6)
    pf = screen_common.prefilter([row], CFG)
    assert pf.kept == [row] and pf.drops == {}


def test_the_prefilter_reads_d04_not_the_universe_quote(rawstore_tmp_db):
    rawstore.write_rows("quote_snapshot", [_q("UP", D, 50.0, v10=1e6), _q("DOWN", D, 3.0, v10=1e6)])
    up = _row("UP", regularMarketPrice=3.0, averageDailyVolume10Day=1e6)        # the stale universe quote
    down = _row("DOWN", regularMarketPrice=50.0, averageDailyVolume10Day=1e6)
    pf = screen_common.prefilter([up, down], CFG)
    assert pf.kept == [up]
    assert pf.drops == {"low_price": ["DOWN"]}


def test_the_newest_session_wins_and_yahoo_comes_first(rawstore_tmp_db):
    old = date(2026, 10, 13)
    rawstore.write_rows("quote_snapshot", [
        _q("AAA", old, 60.0, v10=1e6), _q("AAA", D, 3.0, v10=1e6),                     # newest says 3
        _q("BBB", D, 3.0, source="uw", payload={"close": "3"}), _q("BBB", D, 60.0, v10=1e6),   # Yahoo first
        _q("CCC", D, 60.0, source="uw", payload={"close": "60"})])                    # UW where Yahoo has none
    pf = screen_common.prefilter([_row("AAA"), _row("BBB"), _row("CCC")], CFG)
    assert [r["ticker"] for r in pf.kept] == ["BBB", "CCC"]
    assert pf.drops == {"low_price": ["AAA"]}


def test_dollar_volume_reads_the_yahoo_quote_and_a_row_without_volume_skips_the_rule(rawstore_tmp_db):
    rawstore.write_rows("quote_snapshot", [
        _q("THIN", D, 10.0, v3m=24_999.0),                    # no 10-day: the 3-month average stands in
        _q("EDGE", D, 10.0, v10=25_000.0),                    # exactly at the floor
        _q("UWONLY", D, 10.0, source="uw", payload={"close": "10"})])
    pf = screen_common.prefilter([_row("THIN"), _row("EDGE"), _row("UWONLY")], CFG)
    assert [r["ticker"] for r in pf.kept] == ["EDGE", "UWONLY"]
    assert pf.drops == {"low_dollar_volume": ["THIN"]}


def test_the_10_day_volume_comes_before_the_3_month_one(rawstore_tmp_db):
    rawstore.write_rows("quote_snapshot", [_q("TEN", D, 10.0, v10=1_000.0, v3m=1.0e6)])
    pf = screen_common.prefilter([_row("TEN")], CFG)
    assert pf.drops == {"low_dollar_volume": ["TEN"]}         # 10 x 1,000 is under the floor; the 3-month is not read


def test_a_name_with_no_d04_price_drops_as_no_quote(rawstore_tmp_db):
    rawstore.write_rows("quote_snapshot", [_q("NOPX", D, None, payload={"marketCap": 1e9})])
    pf = screen_common.prefilter([_row("NONE", regularMarketPrice=50.0), _row("NOPX")], CFG)
    assert pf.kept == [] and pf.drops == {"no_quote": ["NONE", "NOPX"]}


def test_the_quotes_session_is_reported(rawstore_tmp_db):
    old = date(2026, 10, 13)
    rawstore.write_rows("quote_snapshot", [_q("AAA", D, 50.0), _q("BBB", D, 50.0), _q("CCC", old, 50.0)])
    pf = screen_common.prefilter([_row("AAA"), _row("BBB"), _row("CCC")], CFG)
    assert pf.quote_session == D and pf.older_quotes == 1


def test_the_prefilter_reads_only_its_two_thresholds():
    rows = [_row("AAA")]
    quotes = {"AAA": {"price": 50.0, "avg_volume": 1e5, "session_date": D, "source": "yfinance"}}
    assert screen_common.prefilter(rows, CFG, quotes=quotes).kept == rows
    assert screen_common.prefilter(rows, SimpleNamespace(MIN_PRICE=51.0, MIN_DOLLAR_VOLUME=0.0),
                                   quotes=quotes).drops == {"low_price": ["AAA"]}
    assert screen_common.prefilter(rows, SimpleNamespace(MIN_PRICE=0.0, MIN_DOLLAR_VOLUME=5_000_001.0),
                                   quotes=quotes).drops == {"low_dollar_volume": ["AAA"]}


@pytest.mark.parametrize("cfg", [sp.config, fp.config], ids=["swing", "fundamentals"])
def test_the_cap_the_top_n_and_the_trend_thresholds_are_gone_from_both_configs(cfg):
    for gone in ("MAX_PHASE2", "TOP_N", "DIST_52W_HIGH_MAX"):
        assert not hasattr(cfg, gone), gone


# ── no cap, no TOP_N ──────────────────────────────────────────────────────────

def test_every_prefiltered_name_is_scored(monkeypatch):
    rows = [{"ticker": f"T{i:03d}", "quote": {}} for i in range(600)]
    seen: list[str] = []
    monkeypatch.setattr(screen_common._universe, "excluded_tickers", lambda: set())
    monkeypatch.setattr(screen_common._universe, "spine_gics", lambda: {})

    def compute_row(row, spy_df, no_fetch, spine, drops):
        seen.append(row["ticker"])
        return {"ticker": row["ticker"]}

    screen_common.run_screen(
        board="t", refreshed_by="T", cfg=CFG, load_universe=lambda **kw: (rows, {}),
        prefilter=lambda rs: screen_common.Prefiltered(list(rs), {}), load_spy=lambda: None,
        compute_row=compute_row, build_payload=lambda results, top_n=None, run_stats=None: {"no_data": False},
        persist=lambda results: None)
    assert len(seen) == 600


def _swing_result(t: str, rs: float) -> dict:
    ind = {"price": 50.0, "sma50": 45.0, "sma150": 40.0, "sma200": 35.0, "sma200_prev_1m": 34.0,
           "above_52w_low_pct": 1.5, "within_52w_high_pct": 0.95, "rs_vs_spy_12m": rs, "mom_12_1": rs,
           "consistency_6m": 0.8, "atr14": 1.0, "sma150_prev_1m": 39.0, "ema10": 49.0, "ema20": 48.0,
           "ema10_prev": 48.5, "ema20_prev": 47.5, "adr_pct": 0.04, "move_max_1_3m": 0.3,
           "high_52w": 52.0, "vol_ratio": 2.0, "rs_vs_spy_3m": 0.1, "ret_3m": 0.1}   # the direction rule (#384)
    return {"ticker": t, "name": t, "market_cap": 1e9, "sector": "Technology", "industry": "Software",
            "themes": [], "earnings_date": None, "ind": ind}


def test_swing_shows_every_candidate(rawstore_tmp_db):
    results = [_swing_result(f"T{i:03d}", i / 300) for i in range(300)]
    p = sp.build_payload(results, run_stats={})
    assert len(p["candidates"]) == sum(1 for c in p["candidates"] if c["methods"]) > 200   # methods: #384


def test_fundamentals_shows_every_candidate(rawstore_tmp_db):
    from RevenueChaser.module_fundamentals.tests.test_fundamental import _m
    rows = [{"ticker": f"T{i:03d}", "name": "x", "price": 50.0, "market_cap": 1e9, "sector": "Technology",
             "industry": "Software", "themes": [], "earnings_date": None, "period": "",
             "metrics": _m(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=50.0 + i, gross_margin=0.35, roic=0.20,
                           net_debt_ebitda=2.0, dist_52w_pct=0.90, rs_6m=0.05, above_200d=True)}
            for i in range(250)]
    assert len(fp.build_payload(rows, run_stats={})["candidates"]) == 250


def test_the_rs_rank_is_a_percentile_over_every_scored_name(rawstore_tmp_db):
    results = [_swing_result(f"T{i:03d}", float(i)) for i in range(601)]
    p = sp.build_payload(results, run_stats={})
    rs = {c["ticker"]: c["rs_rating"] for c in p["candidates"]}
    assert rs["T600"] == 100.0 and rs["T300"] == 50.0


# ── levels nominal, returns adjusted (D3) ─────────────────────────────────────

def _frame(closes, start="2025-01-02"):
    idx = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1e6})


def test_levels_read_the_nominal_bars_and_returns_the_adjusted_ones():
    n = 300
    nominal = _frame([100.0] * n)
    adjusted = _frame([50.0 + i * 0.25 for i in range(n)])
    ind = indicators.compute_indicators(nominal, None, returns_df=adjusted)
    assert ind["price"] == 100.0 and ind["sma50"] == 100.0 and ind["high_52w"] == pytest.approx(101.0)
    assert ind["mom_12_1"] == pytest.approx(((50 + 299 * 0.25) / (50 + 47 * 0.25)) / ((50 + 299 * 0.25) / (50 + 278 * 0.25)) - 1)
    assert ind["change_1m"] == pytest.approx((50 + 299 * 0.25) / (50 + 278 * 0.25) - 1)
    alone = indicators.compute_indicators(nominal, None)
    assert alone["mom_12_1"] == 0.0 and alone["change_1m"] == 0.0     # without returns_df: one frame, as before


# ── the rows read D01 ─────────────────────────────────────────────────────────

def _no_old_read(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a scored name read swing_ohlc / yfinance")
    monkeypatch.setattr(sp, "load_ohlc", boom, raising=False)
    monkeypatch.setattr(fp, "load_ohlc", boom, raising=False)
    monkeypatch.setattr(indicators, "load_ohlc", boom)
    monkeypatch.setattr(sp, "load_ticker_info", lambda t, no_fetch=False: {})
    monkeypatch.setattr(fp, "load_ticker_info", lambda t, no_fetch=False: {})


def test_a_swing_row_reads_d01_nominal_levels_and_adjusted_returns(rawstore_tmp_db, clock, monkeypatch):
    _no_old_read(monkeypatch)
    days = _sessions(D, 300)
    _seed_d01("AAA", days, close=lambda i: 100.0 if i < 150 else 50.0, actions=[(days[150], "split", 2.0),
                                                                                (days[280], "dividend", 1.0)])
    row = sp._compute_row({"ticker": "AAA", "name": "A", "market_cap": 1e9, "quote": {}}, None, spine={})
    ind = row["ind"]
    assert ind["price"] == 50.0                                # the last nominal close
    assert ind["high_52w"] == pytest.approx(50.5)              # split-adjusted, not 101: levels are nominal
    assert ind["change_1m"] == pytest.approx(50.0 / (50.0 * (1 - 1.0 / 50.0)) - 1)   # the dividend is a return
    assert row["ohlc_stale"] is False
    assert rawstore.count("swing_ohlc") == 0


def test_a_scored_name_reads_only_the_window_of_d01(rawstore_tmp_db, clock, monkeypatch):
    """K8a's windowed read: the last ``BAR_DAYS`` calendar days before the session, not the whole stored history."""
    _seed_d01("LONG", _sessions(D, 900))
    nominal, adjusted = screen_common.load_bars("LONG")
    first = min(d for d in _sessions(D, 900) if d >= D - pd.Timedelta(days=screen_common.BAR_DAYS).to_pytimedelta())
    assert nominal.index[0].date() == first and adjusted.index[0].date() == first
    assert nominal.index[-1].date() == D and len(nominal) < 900


def test_a_name_whose_d01_bars_stop_before_the_session_is_marked_stale(rawstore_tmp_db, clock, monkeypatch):
    _no_old_read(monkeypatch)
    _seed_d01("OLD", _sessions(date(2026, 10, 9), 300))
    row = sp._compute_row({"ticker": "OLD", "name": "O", "market_cap": 1e9, "quote": {}}, None, spine={})
    assert row["ohlc_stale"] is True


def test_a_name_with_no_d01_bars_drops_with_its_reason(rawstore_tmp_db, clock, monkeypatch):
    _no_old_read(monkeypatch)
    _seed_d01("SHORT", _sessions(D, 100))
    drops: list[str] = []
    assert sp._compute_row({"ticker": "NONE", "quote": {}}, None, spine={}, drops=drops) is None
    assert sp._compute_row({"ticker": "SHORT", "quote": {}}, None, spine={}, drops=drops) is None
    assert drops == ["no_bars", "short_bars"]


def test_a_fundamentals_row_reads_d01(rawstore_tmp_db, clock, monkeypatch):
    _no_old_read(monkeypatch)
    days = _sessions(D, 300)
    _seed_d01("AAA", days, close=lambda i: 100.0 if i < 150 else 50.0, actions=[(days[150], "split", 2.0),
                                                                                (days[280], "dividend", 1.0)])
    _seed_d01("SPY", days, close=lambda i: 400.0)
    spy = screen_common._ohlcv(__import__("RevenueChaser.core.prices", fromlist=["x"]).daily_bars(
        ["SPY"], basis="adjusted"))
    monkeypatch.setattr(fp.sec_edgar, "get_fundamentals",
                        lambda t, no_fetch=False: ([{"fy": 2025, "revenue": 1e9}], [], {}))
    row = fp._compute_row({"ticker": "AAA", "name": "A", "market_cap": 1e9, "quote": {}}, spy, spine={})
    assert row["price"] == 50.0
    assert row["metrics"]["dist_52w_pct"] == pytest.approx(50.0 / 50.5)
    # flat after the split, flat SPY: only the dividend moves the adjusted closes, and rs_6m is a return (D3)
    assert row["metrics"]["rs_6m"] == pytest.approx(50.0 / (50.0 * (1 - 1.0 / 50.0)) - 1)
    assert rawstore.count("swing_ohlc") == 0


# ── U9: the board says which session its prefilter read ──────────────────────

@pytest.mark.parametrize("mod", [sp, fp], ids=["swing", "fundamentals"])
def test_the_payload_names_the_quotes_session_and_counts_older_quotes(rawstore_tmp_db, monkeypatch, mod):
    from RevenueChaser.core.tests import screen_parity as scn
    scn.seed()
    scn.patch(monkeypatch)
    with rawstore.connect(db=rawstore.MANIFEST["quote_snapshot"].get("db", "raw")) as conn:
        conn.execute("UPDATE quote_snapshot SET session_date = DATE '2026-10-13' WHERE ticker = 'UP2'")
    dq = mod.run_scan()["data_quality"]
    assert dq["quote_session"] == "2026-10-14" and dq["older_quotes"] == 1


# ── the jobs ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("job", ["swing", "module_fundamentals"])
def test_both_fetch_jobs_run_on_the_long_lane(job):
    from RevenueChaser.app import dashboard  # noqa: F401 - registers the jobs
    from RevenueChaser.app import jobs
    assert jobs._LANE[job] == "long"
