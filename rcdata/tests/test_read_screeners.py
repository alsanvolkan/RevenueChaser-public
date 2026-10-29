"""``rcdata.read.last_bars`` / ``bars_by_ticker`` / ``liquidity``: the reads Swing and Fundamentals make of the prices
store (PR 10), on a store seeded directly (``prices_seed``)."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import read

from .prices_seed import seed_bars, seed_factor

DAYS = [date(2026, 10, d) for d in (19, 20, 21, 22, 23)]


def test_no_prices_store_is_no_rows():
    assert read.last_bars(["AAPL"]) == {} and read.bars_by_ticker(["AAPL"]) == [] and read.liquidity(["AAPL"]) == {}


def test_last_bars_is_each_tickers_newest_session_and_its_fetch_time():
    seed_bars("AAPL", DAYS, fetched_at=1000.0)
    seed_bars("MSFT", DAYS[:3], fetched_at=2000.0)
    out = read.last_bars(["aapl", "MSFT", "NOPE", ""])
    assert set(out) == {"AAPL", "MSFT"}
    assert (out["AAPL"]["last_session"], out["AAPL"]["last_fetched_at"]) == (date(2026, 10, 23), 1000.0)
    assert (out["MSFT"]["last_session"], out["MSFT"]["last_fetched_at"]) == (date(2026, 10, 21), 2000.0)
    assert out["AAPL"]["security_id"] == "sym:AAPL"
    assert read.last_bars([]) == {}


def test_bars_by_ticker_are_nominal_rows_with_the_factors_and_the_ticker():
    seed_bars("FLG", DAYS, fetched_at=1.0, close=12.0)
    seed_factor("FLG", 3.0, valid_to=date(2026, 10, 22), tr_factor=2.9)
    rows = read.bars_by_ticker(["flg"], start=date(2026, 10, 20), end=date(2026, 10, 22))
    assert [r["session_date"] for r in rows] == [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)]
    assert {r["ticker"] for r in rows} == {"FLG"} and {r["basis"] for r in rows} == {"nominal"}
    assert [r["close"] for r in rows] == [12.0, 12.0, 12.0]                  # as traded: nothing applied
    assert [(r["split_factor"], r["tr_factor"]) for r in rows] == [(3.0, 2.9), (3.0, 2.9), (1.0, 1.0)]


def test_liquidity_is_the_newest_close_and_the_mean_volume_of_the_last_sessions_on_todays_share_basis():
    seed_bars("FLG", DAYS, fetched_at=1.0, close=12.0, volume=300.0)
    seed_factor("FLG", 3.0, valid_to=date(2026, 10, 22))             # the 19th-21st: 300 old shares = 100 today
    out = read.liquidity(["FLG"], sessions=3)
    flg = out["FLG"]
    assert (flg["price"], flg["price_session"]) == (12.0, date(2026, 10, 23))
    assert flg["avg_volume"] == pytest.approx((100.0 + 300.0 + 300.0) / 3)   # the 21st (factor 3), 22nd, 23rd
    assert flg["volume_sessions"] == 3
    assert read.liquidity(["FLG"])["FLG"]["volume_sessions"] == 5           # fewer bars than the window: all


def test_liquidity_leaves_out_a_name_whose_newest_close_is_too_old():
    seed_bars("AAPL", [date(2026, 11, 20)], fetched_at=1.0)
    seed_bars("OLD", [date(2026, 10, 1)], fetched_at=1.0, replace=True)     # 50 days before the store's newest
    out = read.liquidity(["AAPL", "OLD"])
    assert set(out) == {"AAPL"}
