"""M-OIPD-3 (#382): the smile markers plot the solved IV, not the vendor's ``c["iv"]``.

An expiry whose contracts all carry a wrong vendor IV (0.99) still has a price-implied vol near
the true 0.20: the marker must follow the price, exactly like the fit does.
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np

from RevenueChaser.module_oipd import engine, opd_math, uw_chain

_ASOF = "2026-09-23"
_EXPIRY = "2026-10-23"          # _ASOF + 30 days
_SPOT = 100.0
_R = 0.04
_TRUE_IV = 0.20
_VENDOR_IV = 0.99


def _occ(expiry, cp, strike):
    return f"TST{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}{cp}{int(round(strike * 1000)):08d}"


def _flat_chain():
    """Contracts priced at a flat 20% vol, but whose vendor IV is a wrong 0.99."""
    d = (date.fromisoformat(_EXPIRY) - date.fromisoformat(_ASOF)).days
    T = d / 365.0
    F = _SPOT * math.exp(_R * T)
    rows = []
    for K in np.arange(85.0, 116.0, 2.0):
        for cp in ("C", "P"):
            price = opd_math.black76_price(F, K, T, _R, _TRUE_IV, cp)
            rows.append({
                "option_symbol": _occ(_EXPIRY, cp, K),
                "nbbo_bid": round(price * 0.99, 4), "nbbo_ask": round(price * 1.01, 4),
                "last_price": round(price, 4), "implied_volatility": _VENDOR_IV,
                "open_interest": 100, "volume": 10, "delta": 0.5,
                "last_tape_time": "2026-09-23T20:00:00Z"})
    return uw_chain.map_rows(rows)


def test_the_smile_marker_uses_the_solved_iv_not_the_vendor_iv():
    contracts = _flat_chain()
    entry, err = engine._fit_expiry(_EXPIRY, contracts, _SPOT, _R, _ASOF)
    assert err is None and entry is not None
    ivs = entry["smile"]["iv_mkt"]
    assert ivs, "the smile has no market markers"
    assert min(ivs) > 0.1 and max(ivs) < 0.5, ivs
    assert all(abs(v - _VENDOR_IV) > 1e-6 for v in ivs), ivs
