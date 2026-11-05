"""Real UW check, 5 Nov 2026: a Fetch run outside the session got nbbo_bid = nbbo_ask = 0 on every contract.

The board read those as quoted mids of 0.00, so every trade structure printed "debit -0.00" and a zero-cost
straddle was possible. A zero-wide, zero-priced book is no market: no quote, and the leg is modeled from UW's IV.
"""
from __future__ import annotations

from RevenueChaser.module_spx_gamma.chains import normalize_option_chain, straddle_move
from RevenueChaser.module_spx_gamma.structures import build_structures

BANDS = {"regime": "below_flip", "consolidation": (7500.0, 7721.82), "upside": (7721.82, None),
         "downside": (7400.0, 7500.0)}


def _uw(strike: int, typ: str, bid="0", ask="0", iv="0.15") -> dict:
    return {"option_symbol": f"SPXW261106{typ}0{strike}000", "nbbo_bid": bid, "nbbo_ask": ask,
            "implied_volatility": iv, "open_interest": 100}


def _chain(**kw):
    return {"data": [_uw(k, t, **kw) for k in range(7300, 7850, 25) for t in ("C", "P")]}


def test_a_zero_bid_and_ask_is_no_quote():
    rows = normalize_option_chain(_chain())
    assert all(r["mid"] is None for r in rows)
    assert straddle_move(rows, 7568.92) is None


def test_a_zero_book_is_modeled_from_iv_when_spot_is_known():
    rows = normalize_option_chain(_chain(), 7568.92, "2026-11-04", "2026-11-05T12:55:00Z")
    put = [r for r in rows if r["strike"] == 7500.0 and r["type"] == "P"][0]
    assert put["price_source"] == "modeled" and put["mid"] > 0


def test_structures_on_a_modeled_chain_say_so_and_never_price_zero():
    rows = normalize_option_chain(_chain(), 7568.92, "2026-11-04", "2026-11-05T12:55:00Z")
    built = build_structures(BANDS, rows, "2026-11-06")
    assert built["source"] == "model"
    assert "model" in built["label"].lower() and "not a market price" in built["label"]
    for s in built["structures"]:
        assert s["net"] is not None and s["net"] != 0.0


def test_live_quotes_still_price_as_quotes():
    rows = normalize_option_chain(_chain(bid="4.0", ask="6.0"), 7568.92, "2026-11-04")
    built = build_structures(BANDS, rows, "2026-11-06")
    assert built["source"] == "quotes" and built["label"].startswith("Report structure, not advice: priced from UW quotes")


def test_a_settled_same_day_chain_feeds_neither_the_butterfly_nor_the_book():
    """Real UW check 5 Nov: the 4 Nov 0DTE chain, fetched after its close, gave the Iron Butterfly IVs of 558 %."""
    from RevenueChaser.module_spx_gamma.normalize import normalize_snapshot
    from .conftest import make_raw
    raw = make_raw()
    live = normalize_snapshot(raw, "2026-08-14")
    assert live["iron_butterfly"] is not None and live.get("iron_butterfly_na") is None
    raw["_meta"] = {"api_snapshot_time": "2026-08-15T12:00:00Z"}     # the next morning: the 14 Aug 0DTE settled
    snap = normalize_snapshot(raw, "2026-08-14")
    assert snap["iron_butterfly"] is None
    assert "settled" in snap["iron_butterfly_na"] and "2026-08-14" in snap["iron_butterfly_na"]
    book = snap["dealer_book"]
    assert book is None or "2026-08-14" not in book["expiries"]
    from RevenueChaser.module_spx_gamma import render
    html, md = render.render_html(snap), render.render_markdown(snap)
    assert "Iron Butterfly n/a: the 2026-08-14 0DTE chain had settled" in html
    assert "Iron Butterfly n/a: the 2026-08-14 0DTE chain had settled" in md


def test_a_modeled_butterfly_says_so():
    """Fable delta S1: a pre-open Fetch of the as-of day keeps its 0DTE, all 0/0, so the butterfly is modeled."""
    from RevenueChaser.module_spx_gamma.engines.iron_butterfly import build_iron_butterfly
    chain = normalize_option_chain({"data": [_uw(k, t).copy() | {"option_symbol": f"SPXW261105{t}0{k}000"}
                                             for k in range(7400, 7750, 25) for t in ("C", "P")]},
                                   7568.92, "2026-11-05", "2026-11-05T13:00:00Z")
    ib = build_iron_butterfly({"current_level": 7568.92, "asof_date": "2026-11-05", "option_chain": chain,
                               "expected_move": {"points": 50.0}, "prediction": {"active_pin": 7575.0}})
    assert ib["source"] == "model" and {l["source"] for l in ib["legs"]} == {"modeled"}
    from RevenueChaser.module_spx_gamma import render
    from .conftest import make_snapshot
    snap = make_snapshot()
    snap["iron_butterfly"] = ib
    assert "legs are modeled (Black-Scholes on UW's IV)" in render.render_html(snap)
    assert "legs are modeled (Black-Scholes on UW's IV)" in render.render_markdown(snap)


def test_a_settled_0dte_gives_no_0dte_skew():
    """Fable delta S2: the settled same-day chain must not set the 0DTE skew cell."""
    from RevenueChaser.module_spx_gamma.normalize import chain_skew
    raw = {"option_chain": {"data": [{"option_symbol": "SPXW260814P07500000", "implied_volatility": "5.5",
                                      "delta": "-0.25"},
                                     {"option_symbol": "SPXW260814C07600000", "implied_volatility": "1.8",
                                      "delta": "0.25"}]}}
    assert (chain_skew(raw) or {}).get("put_bias_0dte") is not None
    assert (chain_skew(raw, settled_expiry="2026-08-14") or {}).get("put_bias_0dte") is None


def test_a_naive_fetch_time_is_not_trusted():
    from datetime import datetime
    from RevenueChaser.module_spx_gamma.charm_pin import _expired_by
    assert _expired_by("2026-11-04", datetime(2026, 11, 5, 12, 0)) is False
