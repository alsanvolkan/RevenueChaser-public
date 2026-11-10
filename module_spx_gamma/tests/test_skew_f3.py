"""F3 (Fable report review, 5 Nov 2026): skew is the 25-delta put IV minus the 25-delta call IV of a real chain.

The board used to guess which of UW's two /interpolated-iv blocks is puts and which is calls (``derived_unverified``).
The reports quote the 25-delta risk reversal at 0DTE, 1M and 3M; UW's /option-contracts rows carry ``delta`` and
``implied_volatility`` per contract, so the skew is read from the front chain and from one chain near 30 and 90 days.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_spx_gamma import chains, normalize as nm, snapshot as snap


def _row(expiry, strike, cp, delta, iv):
    ymd = expiry.replace("-", "")[2:]
    return {"option_symbol": f"SPXW{ymd}{cp}{int(strike * 1000):08d}", "delta": str(delta),
            "implied_volatility": str(iv), "nbbo_bid": "1", "nbbo_ask": "2"}


def _chain(expiry, put_iv=0.22, call_iv=0.14):
    return {"data": [
        _row(expiry, 7400, "P", -0.10, 0.30), _row(expiry, 7550, "P", -0.24, put_iv),
        _row(expiry, 7600, "P", -0.32, 0.20), _row(expiry, 7700, "C", 0.50, 0.16),
        _row(expiry, 7850, "C", 0.26, call_iv), _row(expiry, 7950, "C", 0.12, 0.13),
    ]}


def test_delta25_skew_takes_the_contracts_nearest_25_delta():
    rows = chains.normalize_option_chain(_chain("2026-12-04"))
    s = chains.delta25_skew(rows)
    assert s["expiry"] == "2026-12-04"
    assert s["put_strike"] == 7550.0 and s["call_strike"] == 7850.0
    assert s["skew"] == pytest.approx(0.22 - 0.14)


def test_delta25_skew_uses_the_front_expiry_of_a_mixed_chain():
    rows = chains.normalize_option_chain({"data": _chain("2026-11-05")["data"] + _chain("2026-11-06", 0.5, 0.1)["data"]})
    assert chains.delta25_skew(rows)["expiry"] == "2026-11-05"


def test_delta25_skew_needs_a_contract_near_25_delta_on_both_sides():
    rows = chains.normalize_option_chain({"data": [_row("2026-12-04", 7400, "P", -0.05, 0.3),
                                                   _row("2026-12-04", 7850, "C", 0.26, 0.14)]})
    assert chains.delta25_skew(rows) is None


@pytest.mark.parametrize("expiries, want", [
    (["2026-11-05", "2026-11-06", "2026-12-04", "2026-12-18", "2027-02-05", "2027-03-19"],
     {"1m": "2026-12-04", "3m": "2027-02-05"}),
    (["2026-11-05", "2026-11-06"], {}),                    # nothing within half a tenor of 30 or 90 days
])
def test_skew_expiries_pick_the_nearest_to_30_and_90_days(expiries, want):
    assert snap.skew_expiries(expiries, "2026-11-04") == want


def test_chain_skew_reports_three_tenors_from_uw_chains():
    raw = {"option_chain": _chain("2026-11-05", 0.25, 0.15),
           "skew_chains": {"1m": _chain("2026-12-04", 0.22, 0.14), "3m": _chain("2027-02-05", 0.21, 0.15)}}
    s = nm.chain_skew(raw)
    assert s["put_bias_0dte"] == pytest.approx(0.10)
    assert s["put_bias_1m"] == pytest.approx(0.08)
    assert s["put_bias_3m"] == pytest.approx(0.06)
    assert s["expiries"] == {"0dte": "2026-11-05", "1m": "2026-12-04", "3m": "2027-02-05"}
    assert "derived_unverified" not in s and "25-delta" in s["note"]


def test_chain_skew_is_none_without_chains():
    assert nm.chain_skew({}) is None


def test_the_twin_block_guess_is_retired():
    raw = {"interpolated_iv": {"data": [{"days": 30, "volatility": "0.119"}, {"days": 30, "volatility": "0.115"}]}}
    assert nm.chain_skew(raw) is None
    assert not hasattr(nm, "normalize_skew")


def test_a_stored_day_reads_its_skew_chains_back(tmp_path):
    import json
    from RevenueChaser.module_spx_gamma.raw_day import load_raw_day
    from .conftest import store_day
    store_day("2026-11-04", {"effective_date": "2026-11-04"}, {"skew_chain_1m.json": _chain("2026-12-04")})
    raw = load_raw_day("2026-11-04")
    assert set(raw["skew_chains"]) == {"1m"}
    assert nm.chain_skew(raw)["put_bias_1m"] == pytest.approx(0.08)
