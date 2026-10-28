"""Each Asymmetric Growth card says which session its price and momentum come from (AG review SF-1, PR 6).

The board says "data as of" the newest D01 session among its cards, but a name whose bars stop earlier (a halt, a
Daily Bars gap) was scored on its own older bars with nothing on the card to say so. Now the card carries its
own bar date (``period_ends["price_bar"]``, stored with the card) and a data check when that date is older than
the session the scan read up to; a card with no bars at all has no date and no check (its momentum is unknown).
"""
from __future__ import annotations

from RevenueChaser.core import rawstore, ticker_store
from RevenueChaser.module_asymmetric_growth import engine, report
from RevenueChaser.module_asymmetric_growth.tests.test_ag_dated_momentum_380 import (
    SESSION, edgar, row, seed_momentum, sessions, universe)


def test_a_card_whose_bars_stop_early_says_so(rawstore_tmp_db, monkeypatch):
    seed_momentum(["FRESH"])
    early = sessions(SESSION, 262)[-3]                              # two sessions before the scan's
    seed_momentum(["HALTED"], session=early)
    rawstore.write_rows("edgar_financials", [edgar("FRESH"), edgar("HALTED"), edgar("QUOTEONLY")])
    universe(monkeypatch, [row("FRESH", quote={}), row("HALTED", quote={}),
                           row("QUOTEONLY", quote={"regularMarketPrice": 42.0})])
    res = engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)
    cards = {c["ticker"]: c for c in res["cards"]}

    assert cards["FRESH"]["period_ends"]["price_bar"] == SESSION.isoformat()
    assert "price_bar_older" not in cards["FRESH"]["flags"]
    assert cards["HALTED"]["period_ends"]["price_bar"] == early.isoformat()
    assert "price_bar_older" in cards["HALTED"]["flags"]
    assert cards["QUOTEONLY"]["period_ends"]["price_bar"] is None
    assert "price_bar_older" not in cards["QUOTEONLY"]["flags"]

    ticker_store.write_from_cards(res["cards"], asof=SESSION.isoformat())
    stored = {c["ticker"]: c for c in ticker_store.all_cards()}     # a Refresh shows the same
    assert stored["HALTED"]["period_ends"]["price_bar"] == early.isoformat()
    assert "price_bar_older" in stored["HALTED"]["flags"]


def test_the_card_explains_the_check():
    src = report.__loader__.get_source(report.__name__)
    assert "price_bar_older:" in src and "pe.price_bar" in src
