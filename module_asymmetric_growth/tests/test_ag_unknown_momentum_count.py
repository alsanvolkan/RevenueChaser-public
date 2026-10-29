"""The board counts the cards whose momentum is unknown (Asymmetric Growth review N-2, PR 6).

A name with a price but no D01 bars keeps its card with unknown momentum (#380 S4, decision 3), so it can never
pass the momentum signal; nothing on the board said how many cards that is. The summary now counts them
(``momentum_unknown``) and the funnel shows them as a note step, like the Yahoo-statements note.
"""
from __future__ import annotations

from RevenueChaser.core import rawstore
from RevenueChaser.module_asymmetric_growth import engine
from RevenueChaser.module_asymmetric_growth.tests.test_ag_dated_momentum_380 import (
    edgar, row, seed_momentum, steps, universe)


def test_cards_with_unknown_momentum_are_counted_and_shown(rawstore_tmp_db, monkeypatch):
    seed_momentum(["HASBARS"])
    rawstore.write_rows("edgar_financials", [edgar("HASBARS"), edgar("QUOTEONLY")])
    universe(monkeypatch, [row("HASBARS", quote={}), row("QUOTEONLY", quote={"regularMarketPrice": 42.0})])
    res = engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)
    assert res["summary"]["momentum_unknown"] == 1
    note = steps(res)["momentum_unknown"]
    assert note["count"] == 1 and note["kind"] == "note" and note["tickers"] == ["QUOTEONLY"]


def test_a_summary_counted_from_cards_counts_them_too():
    known = {"ticker": "A", "sector": "X", "total_points": 5, "themes": [], "tech_breakthrough": False,
             "signals": [{"key": "momentum", "known": 3, "total": 3}]}
    unknown = dict(known, ticker="B", signals=[{"key": "momentum", "known": 0, "total": 3}])
    assert engine._summary([known, unknown], 0, {})["momentum_unknown"] == 1
