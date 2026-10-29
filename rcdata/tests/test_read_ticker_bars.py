"""``rcdata.read``'s by-ticker bar reads for the price boards (G3: Co-movement, Sector Rotation, Asymmetric Growth).

The boards name tickers; the prices store keys every row on the security id. A ticker is the security that trades
under it now (``security_symbol``), so a renamed name brings its bars from before the rename, and the bases are the
store's own: ``split`` (splits applied, dividends not: the old D01 "nominal") and ``total_return`` (the old D01
"adjusted").
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import read

from . import prices_store


def _sessions(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if calendar.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _sessions(date(2026, 9, 1), 10)


def _bars(ticker, closes, *, days=DAYS, **extra):
    return [{"ticker": ticker, "session_date": d, "close": c, "volume": 100.0, **extra} for d, c in zip(days, closes)]


def test_no_prices_file_is_no_rows():
    assert read.ticker_bars(["AAPL"]).empty
    assert read.last_sessions(["AAPL"]) == {} and read.newest_session() is None
    assert read.changed_at(["AAPL"]) == 0.0 and read.bar_sessions(["AAPL"]) == []
    assert read.bar_facts(["AAPL"], start=DAYS[0], flat_run=3) == {"first": {}, "last": {}, "sessions": [], "flat": []}


def test_a_renamed_security_reads_its_whole_history_under_todays_ticker():
    prices_store.put_bars(_bars("OLD", [10, 11, 12], days=DAYS[:3], security_id="figi:X")
                          + _bars("NEW", [13, 14], days=DAYS[3:5], security_id="figi:X"), replace=False)
    frame = read.ticker_bars(["new", "OLD"])
    assert list(frame["ticker"].unique()) == ["NEW"]
    assert list(frame["close"]) == [10, 11, 12, 13, 14]
    assert read.last_sessions(["NEW", "OLD"]) == {"NEW": DAYS[4]}


def test_split_and_total_return_bases_apply_the_stored_factors():
    prices_store.put_bars(_bars("SPL", [100, 100, 50, 50]) + _bars("DIV", [20, 20, 19, 19]))
    prices_store.put_action("SPL", DAYS[2], "split", 2.0)
    prices_store.put_action("DIV", DAYS[2], "dividend", 1.0)
    split = read.ticker_bars(["SPL", "DIV"], basis="split")
    spl = split[split["ticker"] == "SPL"]
    assert list(spl["close"]) == [50, 50, 50, 50] and list(spl["volume"]) == [200, 200, 100, 100]
    assert list(split[split["ticker"] == "DIV"]["close"]) == [20, 20, 19, 19]       # a dividend moves no split bar
    tr = read.ticker_bars(["DIV"], basis="total_return")
    assert list(tr["close"]) == pytest.approx([19, 19, 19, 19])                       # 20 * (1 - 1 / 20)
    assert set(tr["basis"]) == {"total_return"}
    with pytest.raises(ValueError, match="basis"):
        read.ticker_bars(["SPL"], basis="adjusted")


def test_bounds_and_sort_order():
    prices_store.put_bars(_bars("BBB", range(1, 11)) + _bars("AAA", range(1, 11)))
    frame = read.ticker_bars(["BBB", "AAA"], start=DAYS[2], end=DAYS[4])
    assert list(frame["ticker"]) == ["AAA"] * 3 + ["BBB"] * 3
    assert [d.date() for d in frame["session_date"]] == DAYS[2:5] * 2


def test_a_bar_without_a_close_is_not_a_bar():
    """Review S1 (PR 270): like the other five reads, ``ticker_bars`` leaves out a NULL or NaN close (a halt row), so
    no board takes a bar with no price as its newest one."""
    prices_store.put_bars(_bars("AAA", [1, 2, 3], days=DAYS[:3])
                          + [{"ticker": "AAA", "session_date": DAYS[3], "close": None, "volume": 5.0},
                             {"ticker": "AAA", "session_date": DAYS[4], "close": float("nan"), "volume": 5.0}],
                          replace=False)
    frame = read.ticker_bars(["AAA"])
    assert [d.date() for d in frame["session_date"]] == DAYS[:3] and list(frame["close"]) == [1, 2, 3]


def test_newest_session_leaves_out_the_index_level_and_counts_only_closes():
    prices_store.put_bars(_bars("AAA", [1, 2, 3], days=DAYS[:3])
                          + [{"ticker": "SPX", "security_id": "index:SPX", "session_date": DAYS[5], "close": 1.0},
                             {"ticker": "AAA", "session_date": DAYS[4], "close": None}], replace=False)
    assert read.newest_session() == DAYS[2]
    assert read.bar_sessions(["AAA"]) == DAYS[:3]
    assert read.bar_sessions(["AAA"], start=DAYS[1]) == DAYS[1:3]


def test_changed_at_moves_with_a_new_action():
    prices_store.put_bars(_bars("AAA", [1, 2, 3], fetched_at=100.0))
    assert read.changed_at(["AAA"]) == 100.0
    prices_store.put_action("AAA", DAYS[2], "dividend", 0.1, fetched_at=500.0)
    assert read.changed_at(["aaa"]) == 500.0
    assert read.changed_at(["ZZZ"]) == 0.0


def test_bar_facts_first_last_sessions_and_flat_candidates():
    prices_store.put_bars(_bars("FLAT", [5, 5, 5, 5, 6, 7, 8, 9, 10, 11]) + _bars("MOVE", range(1, 11))
                          + _bars("EXD", [10, 20, 30, 15, 16, 17, 18, 19, 20, 21])
                          + _bars("SPLF", [10, 10, 5, 5, 6, 7, 8, 9, 10, 11]))
    prices_store.put_action("EXD", DAYS[3], "split", 2.0)          # 30 -> 15 is a split, not a move of the stock
    prices_store.put_action("SPLF", DAYS[2], "split", 2.0)         # 10, 10, 5, 5 is flat once the split is applied
    facts = read.bar_facts(["FLAT", "MOVE", "EXD", "SPLF", "NONE"], start=DAYS[0], flat_run=3)
    assert facts["first"] == {t: DAYS[0].isoformat() for t in ("FLAT", "MOVE", "EXD", "SPLF")}
    assert facts["last"]["MOVE"] == DAYS[-1].isoformat()
    assert facts["sessions"] == [d.isoformat() for d in DAYS]
    assert facts["flat"] == ["FLAT", "SPLF"]
    later = read.bar_facts(["FLAT"], start=DAYS[3], flat_run=3)
    assert later["flat"] == [] and later["first"] == {"FLAT": DAYS[3].isoformat()}
