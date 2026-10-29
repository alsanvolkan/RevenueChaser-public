"""PR 10 (G4): the Swing and Fundamentals prefilter reads price and average volume from the prices store
(``rcdata.read.liquidity``), not from D04's ``quote_snapshot``, which no job fills since the 27 Oct purge."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

from RevenueChaser.core import rawstore, screen_common
from RevenueChaser.rcdata.tests.prices_seed import seed_bars, seed_factor

D = date(2026, 10, 14)
DAYS = [d.date() for d in pd.bdate_range(end=D, periods=5)]
CFG = SimpleNamespace(MIN_PRICE=5.0, MIN_DOLLAR_VOLUME=1_000_000.0)


def _rows(*tickers):
    return [{"ticker": t} for t in tickers]


def test_the_prefilter_reads_the_prices_store_and_never_d04(rawstore_tmp_db):
    rawstore.write_rows("quote_snapshot", [{
        "ticker": "OK", "session_date": D, "source": "yfinance", "market_cap": 1e9, "price": 1.0,
        "shares_outstanding": None, "week_52_high": None, "week_52_low": None,
        "payload": {"averageDailyVolume10Day": 1.0}, "fetched_at": 0.0, "run_id": "q", "run_at": 0.0}])
    seed_bars("OK", DAYS, fetched_at=1.0, close=50.0, volume=100_000.0)          # $5M a day
    seed_bars("CHEAP", DAYS, fetched_at=1.0, close=3.0, volume=10_000_000.0)
    seed_bars("THIN", DAYS, fetched_at=1.0, close=50.0, volume=1_000.0)          # $50k a day
    pf = screen_common.prefilter(_rows("OK", "CHEAP", "THIN", "NOBARS"), CFG)
    assert [r["ticker"] for r in pf.kept] == ["OK"]
    assert pf.drops == {"low_price": ["CHEAP"], "low_dollar_volume": ["THIN"], "no_quote": ["NOBARS"]}
    assert pf.quote_session == D and pf.older_quotes == 0 and pf.no_volume == []


def test_the_volume_is_on_todays_share_basis_and_an_older_close_is_counted():
    seed_bars("SPLIT", DAYS, fetched_at=1.0, close=50.0, volume=30_000.0)
    seed_factor("SPLIT", 2.0, valid_to=DAYS[-1])           # a 1-for-2 reverse split ex the last day: 30k old = 15k new
    seed_bars("LAG", DAYS[:-1], fetched_at=1.0, close=50.0, volume=100_000.0)
    pf = screen_common.prefilter(_rows("split", "LAG"), CFG)
    assert [r["ticker"] for r in pf.kept] == ["LAG"]
    assert pf.drops == {"low_dollar_volume": ["split"]}        # (4 x 15k + 30k) / 5 x $50 = $0.9M; unadjusted $1.5M
    assert pf.quote_session == D and pf.older_quotes == 1


def test_a_test_can_still_hand_in_its_own_quotes():
    pf = screen_common.prefilter(_rows("X"), CFG, quotes={"X": {"price": 10.0, "avg_volume": 1e6,
                                                                 "session_date": D}})
    assert [r["ticker"] for r in pf.kept] == ["X"]
