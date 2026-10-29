"""PR 11 (G3): Asymmetric Growth reads its momentum from the prices store (``prices.duckdb``), not D01.

``_price_session`` is SPY's newest session there (``rcdata.read.last_sessions``) and ``_bars_by_ticker`` one windowed
``rcdata.read.ticker_bars`` read on the split basis (D01's old "nominal"); a bar only in the old ``price_bars_daily``
is no bar. The board's data date (``core.provenance``) is its scored names' prices-store session, and each card keeps
its own bar date (review SF-1, the card half from PR 6). The freeze anchor still reads D01 until G4. Offline.
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.core import provenance, rawstore
from RevenueChaser.module_asymmetric_growth import engine
from RevenueChaser.module_asymmetric_growth.report import render_asymmetric_growth_hunter_html
from RevenueChaser.module_asymmetric_growth.tests.test_ag_dated_momentum_380 import (
    SESSION, bar, edgar, momentum, row, sessions, universe)
from RevenueChaser.rcdata.tests import prices_store


def _momentum_rows(ticker: str, days) -> list[dict]:
    return [bar(ticker, d, close=102.0 if k >= len(days) - 10 else 100.0,
                volume=160.0 if k >= len(days) - 10 else 100.0) for k, d in enumerate(days)]


def test_the_momentum_comes_from_the_prices_store_not_d01(rawstore_tmp_db):
    days = sessions()
    prices_store.put_bars(_momentum_rows("NEW", days) + _momentum_rows("SPY", days))
    rawstore.write_rows("price_bars_daily", [bar("OLD", d) for d in days] + [bar("SPY", d) for d in sessions(
        date(2026, 10, 20), 5)])                                         # the old D01: a newer SPY bar, OLD's bars
    preload = engine._preload(["NEW", "OLD"], True)
    assert preload["session"] == SESSION                                # SPY's newest prices-store session
    assert set(preload["bars"]) == {"NEW"}
    t = engine.technical_from_bars(preload["bars"]["NEW"], preload["session"])
    assert (t["price"], t["bar_date"], t["above_sma50"]) == (102.0, SESSION.isoformat(), True)


def test_a_split_reads_on_the_split_basis(rawstore_tmp_db):
    days = sessions()
    ex = days[-100]
    rows = [bar("SPL", d, close=100.0, high=105.0) for d in days if d < ex]
    rows += [bar("SPL", d, close=50.0, high=52.5) for d in days if d >= ex]
    prices_store.put_bars(rows + [bar("SPY", d) for d in days[-5:]])
    prices_store.put_action("SPL", ex, "split", 2.0)
    preload = engine._preload(["SPL"], True)
    t = engine.technical_from_bars(preload["bars"]["SPL"], preload["session"])
    assert (t["price"], t["high_52w"]) == (50.0, 52.5)


def test_a_dividend_does_not_move_the_52_week_high(rawstore_tmp_db):
    """The split basis takes no cash dividend out (the total-return one would): the high before the ex-date stays
    the level it traded at, so the distance from it is what a chart shows."""
    days = sessions()
    ex = days[-50]
    rows = [bar("DIV", d, close=100.0, high=105.0 if d < ex else 101.0) for d in days]
    prices_store.put_bars(rows + [bar("SPY", d) for d in days[-5:]])
    prices_store.put_action("DIV", ex, "dividend", 10.0)                # a tenth of the close: 105 would read 94.5
    preload = engine._preload(["DIV"], True)
    t = engine.technical_from_bars(preload["bars"]["DIV"], preload["session"])
    assert (t["price"], t["high_52w"]) == (100.0, 105.0)


def test_a_halt_row_with_no_close_does_not_blank_the_momentum(rawstore_tmp_db):
    """Review S1 (PR 270): a newest bar with no close (a halt row) is no bar, so the card keeps the session before's
    price, its momentum and that session as its bar date."""
    days = sessions()
    rows = _momentum_rows("HLT", days[:-1]) + [{**bar("HLT", days[-1]), "close": None}]
    prices_store.put_bars(rows + [bar("SPY", d) for d in days[-5:]])
    preload = engine._preload(["HLT"], True)
    t = engine.technical_from_bars(preload["bars"]["HLT"], preload["session"])
    assert (t["price"], t["bar_date"], t["above_sma50"]) == (102.0, days[-2].isoformat(), True)


def test_a_scan_dates_its_cards_from_the_prices_store(rawstore_tmp_db, monkeypatch):
    days = sessions()
    prices_store.put_bars(_momentum_rows("AAA", days) + _momentum_rows("SPY", days))
    rawstore.write_rows("edgar_financials", [edgar("AAA")])
    universe(monkeypatch, [row("AAA")])
    res = engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)
    (card,) = res["cards"]
    assert card["price"] == 102.0 and momentum(card)["known"] == 3
    assert res["data_as_of"] == SESSION.isoformat()
    html = render_asymmetric_growth_hunter_html(res)
    assert f"Prices: session {SESSION.isoformat()}" in html and "D01" not in html


def test_the_board_data_date_is_its_scored_names_prices_store_session(rawstore_tmp_db):
    prices_store.put_bars([bar("AAA", d) for d in sessions(n=5)] + [bar("ZZZ", d) for d in sessions(n=30)][:20])
    rawstore.write_rows("price_bars_daily", [bar("AAA", d) for d in sessions(date(2026, 10, 20), 3)])
    rawstore.write_rows("ticker_scores", [{"ticker": "AAA", "total_points": 5}])
    assert provenance.RESOLVERS["module_asymmetric_growth"]() == SESSION.isoformat()


def test_the_selection_version_moved_with_the_source():
    assert engine.SELECTION_VERSION == "8"                  # after G6's 7 (review SF-4)
