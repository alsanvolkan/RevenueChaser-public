"""UW option-contract mapping, expiry selection and forward inference."""
from __future__ import annotations

import math

from RevenueChaser.module_oipd import opd_math, uw_chain


def _row(sym, bid, ask, iv=None, oi=10, vol=1):
    return {"option_symbol": sym, "nbbo_bid": bid, "nbbo_ask": ask,
            "last_price": bid, "implied_volatility": iv, "open_interest": oi,
            "volume": vol, "delta": 0.5, "last_tape_time": "2026-09-23T20:00:00Z"}


class TestMapRows:
    def test_parses_symbol_and_mid(self):
        rows = uw_chain.map_rows([_row("AAPL261016C00185000", 4.0, 4.4, iv=0.25)])
        assert len(rows) == 1
        c = rows[0]
        assert c["strike"] == 185.0 and c["option_type"] == "C"
        assert c["expiry"] == "2026-10-16"
        assert c["mid"] == 4.2

    def test_falls_back_to_last_when_no_quote(self):
        rows = uw_chain.map_rows([_row("AAPL261016P00185000", 0, 0, iv=0.25)])
        assert rows[0]["mid"] == 0.0 or rows[0]["mid"] is None

    def test_drops_unparsable_symbol(self):
        assert uw_chain.map_rows([_row("garbage", 1, 1)]) == []


class TestExpirySelection:
    def _rows(self):
        return [
            {"expiry": "2026-09-23", "open_interest": 900, "volume": 1, "chains": 5},   # 0DTE
            {"expiry": "2026-10-17", "open_interest": 500, "volume": 1, "chains": 5},   # 24d
            {"expiry": "2026-12-18", "open_interest": 800, "volume": 1, "chains": 5},   # 86d
            {"expiry": "2027-12-17", "open_interest": 999, "volume": 1, "chains": 5},   # >365d
        ]

    def test_filters_dte_and_sorts(self):
        picked = uw_chain.expiries_from_breakdown(
            self._rows(), "2026-09-23", min_dte=1, max_dte=365, max_expiries=12)
        assert [p["expiry"] for p in picked] == ["2026-10-17", "2026-12-18"]

    def test_caps_by_highest_open_interest(self):
        rows = self._rows() + [
            {"expiry": "2026-11-20", "open_interest": 700, "volume": 1, "chains": 5}]
        picked = uw_chain.expiries_from_breakdown(
            rows, "2026-09-23", min_dte=1, max_dte=365, max_expiries=2)
        # 86d (800 OI) and 58d (700 OI) win over 24d (500 OI)
        assert [p["expiry"] for p in picked] == ["2026-11-20", "2026-12-18"]

    def test_reads_the_expires_key_uw_really_sends(self):
        """UW's expiry-breakdown sends ``expires`` (the options data's first real sample, 2026-10-28), though the
        spec names it ``expiry``; read by the spec's name only, no expiry was ever picked."""
        rows = [{("expires" if k == "expiry" else k): v for k, v in r.items()} for r in self._rows()]
        picked = uw_chain.expiries_from_breakdown(
            rows, "2026-09-23", min_dte=1, max_dte=365, max_expiries=12)
        assert [p["expiry"] for p in picked] == ["2026-10-17", "2026-12-18"]


class TestForward:
    def test_infers_from_put_call_parity(self):
        T, r, spot = 0.25, 0.04, 100.0
        F = spot * math.exp(r * T)
        contracts = []
        for K in (90.0, 95.0, 100.0, 105.0, 110.0):
            c = opd_math.black76_price(F, K, T, r, 0.25, "C")
            p = opd_math.black76_price(F, K, T, r, 0.25, "P")
            contracts.append({"option_type": "C", "strike": K, "mid": c})
            contracts.append({"option_type": "P", "strike": K, "mid": p})
        got = uw_chain.infer_forward(contracts, T, r, spot)
        assert abs(got - F) < 0.05

    def test_falls_back_to_spot_when_no_pairs(self):
        got = uw_chain.infer_forward([], 0.25, 0.04, 100.0)
        assert abs(got - 100.0 * math.exp(0.04 * 0.25)) < 1e-9


class TestContractIv:
    def test_prefers_price_implied_vol(self):
        F, T, r = 100.0, 0.25, 0.04
        price = opd_math.black76_price(F, 100.0, T, r, 0.30, "C")
        c = {"option_type": "C", "strike": 100.0, "mid": price, "iv": 0.99}
        iv = uw_chain.contract_iv(c, F, T, r)
        assert abs(iv - 0.30) < 1e-4

    def test_falls_back_to_vendor_iv(self):
        c = {"option_type": "C", "strike": 100.0, "mid": None, "iv": 0.42}
        assert uw_chain.contract_iv(c, 100.0, 0.25, 0.04) == 0.42
