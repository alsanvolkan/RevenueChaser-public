"""A7 (Fable report review, 5 Nov 2026): the straddle expected move beside the 1-sd move.

The reports quote the at-the-money straddle of the target expiry as the market's own expected move. The Fetch
asks UW /option-contracts for the target Friday (one call, live only); the straddle is the ATM call mid plus the
ATM put mid, quotes only (a modeled mid is not a market price).
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.module_spx_gamma import chains, normalize as nm, render, snapshot as snap
from RevenueChaser.module_spx_gamma.raw_day import load_raw_day


def _row(expiry, strike, cp, bid, ask):
    ymd = expiry.replace("-", "")[2:]
    return {"option_symbol": f"SPXW{ymd}{cp}{int(strike * 1000):08d}", "nbbo_bid": str(bid), "nbbo_ask": str(ask),
            "implied_volatility": "0.15", "delta": "0.5" if cp == "C" else "-0.5"}


CHAIN = {"data": [_row("2026-11-06", 7575, "C", 40, 42), _row("2026-11-06", 7575, "P", 38, 40),
                  _row("2026-11-06", 7600, "C", 28, 30), _row("2026-11-06", 7600, "P", 50, 52)]}


def test_straddle_at_the_strike_nearest_spot():
    s = chains.straddle_move(chains.normalize_option_chain(CHAIN), 7580.0)
    assert s["strike"] == 7575.0 and s["expiry"] == "2026-11-06"
    assert s["points"] == pytest.approx(41.0 + 39.0)
    assert s["lower"] == pytest.approx(7500.0) and s["upper"] == pytest.approx(7660.0)


def test_no_quotes_no_straddle():
    rows = chains.normalize_option_chain({"data": [{**r, "nbbo_bid": None, "nbbo_ask": None} for r in CHAIN["data"]]},
                                         spot=7580.0, asof_date="2026-11-04")
    assert chains.straddle_move(rows, 7580.0) is None


def test_snapshot_reads_the_target_chain():
    s = nm.normalize_snapshot({"target_chain": CHAIN, "spot": {"value": 7580.0}}, "2026-11-04")
    assert s["straddle_move"]["points"] == pytest.approx(80.0)


def test_target_chain_file_is_live_and_read_back(tmp_path):
    assert "target_chain.json" in snap.LIVE_FILES
    from .conftest import store_day
    store_day("2026-11-04", {"effective_date": "2026-11-04"}, {"target_chain.json": CHAIN})
    assert load_raw_day("2026-11-04")["target_chain"] == CHAIN


def test_vol_board_shows_the_straddle():
    from .conftest import make_snapshot
    s = make_snapshot()
    s["straddle_move"] = {"expiry": "2026-08-21", "strike": 7775.0, "points": 80.0, "lower": 7704.0, "upper": 7864.0}
    assert "Straddle expected move" in render.render_html(s)
