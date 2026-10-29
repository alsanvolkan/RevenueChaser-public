"""``core.market_bars``: the prices store's bars in the frame the screeners read (PR 10, G4). The boards' "nominal"
is the split basis and their "adjusted" the total-return basis; volume is on today's share basis on both."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from RevenueChaser.core import market_bars
from RevenueChaser.rcdata.tests.prices_seed import seed_bars, seed_factor

DAYS = [date(2026, 10, d) for d in (20, 21, 22)]


def test_no_store_is_an_empty_frame_with_the_columns():
    both = market_bars.daily_bars_bases(["AAPL"])
    assert set(both) == {"nominal", "adjusted"}
    assert list(both["nominal"].columns) == market_bars.COLUMNS and both["nominal"].empty
    assert market_bars.last_bars(["AAPL"]) == {}


def test_the_boards_bases_are_the_split_and_the_total_return_bases():
    seed_bars("FLG", DAYS, fetched_at=5.0, close=12.0, volume=300.0)
    seed_factor("FLG", 3.0, valid_to=date(2026, 10, 22), tr_factor=2.5)
    both = market_bars.daily_bars_bases(["flg"], start=date(2026, 10, 21))
    nominal, adjusted = both["nominal"], both["adjusted"]
    assert list(nominal["session_date"]) == [pd.Timestamp("2026-10-21"), pd.Timestamp("2026-10-22")]
    assert list(nominal["close"]) == [36.0, 12.0] and list(adjusted["close"]) == [30.0, 12.0]
    assert list(nominal["volume"]) == [100.0, 300.0] == list(adjusted["volume"])
    assert list(nominal["ticker"]) == ["FLG", "FLG"] and set(nominal["source"]) == {"alpaca/bars"}
    assert market_bars.daily_bars(["FLG"], "adjusted", end=date(2026, 10, 20))["close"].tolist() == [30.0]
    with pytest.raises(ValueError, match="basis"):
        market_bars.daily_bars(["FLG"], "split")


def test_last_bars_has_the_ledger_shape():
    seed_bars("SPY", DAYS, fetched_at=7.5)
    assert market_bars.last_bars(["spy", "NOPE"]) == {"SPY": ("2026-10-22", 7.5)}
