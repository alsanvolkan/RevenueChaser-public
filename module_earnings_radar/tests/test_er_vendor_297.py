"""#297 (D12): the Earnings pipeline keeps the UW statements it reads for R3/R7 and stores them through the run's
vendor capture. One ``/stock/{t}/financials`` request per ticker, as before. Offline fake UW, temp store."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.core import rawstore, vendor_data
from RevenueChaser.core.tests.test_vendor_data import _uw_fin
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW

COMPLETE = datetime(2026, 10, 8, 20, 35, tzinfo=timezone.utc).timestamp()
_SCREENER = {"AAPL": {"close": "10", "call_volume": "0", "put_volume": "0", "total_open_interest": "0"}}
_CAL = {"AAPL": {"earnings_date": "2026-08-14", "full_name": "Aple", "sector": "Technology",
                 "expected_move_perc": 0.05}}


@pytest.fixture(autouse=True)
def _isolate(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr(vendor_data, "_clock", lambda: COMPLETE)
    return rawstore_tmp_db


class _FinUW(_FakeUW):
    """A fake that serves ``financials`` like UWClient and maps it with the real ``_map_financials``."""

    def __init__(self, fin=None, **ov):
        super().__init__(**ov)
        self.fin = fin if fin is not None else _uw_fin()
        self.fin_calls = []
        self.mapped = []

    def financials(self, ticker):
        self.fin_calls.append(ticker)
        return self.fin

    def _map_financials(self, ticker, fin=None):
        from RevenueChaser.core.providers.unusual_whales import UWClient
        self.mapped.append(fin)
        return UWClient._map_financials(self, ticker, fin=fin)

    @staticmethod
    def _num(row, *keys):
        from RevenueChaser.core.providers.unusual_whales import UWClient
        return UWClient._num(row, *keys)


def _pipeline(uw):
    p = EarningsPipeline(["AAPL"], uw=uw, cal_meta=_CAL)
    p._screener_cache = _SCREENER
    p._dgs10_cache = {}
    return p


def test_a_scored_ticker_stores_its_uw_statements_with_one_request():
    uw = _FinUW()
    p = _pipeline(uw)
    card = p._score("AAPL")
    assert card is not None
    assert uw.fin_calls == ["AAPL"] and uw.mapped == [uw.fin]     # the map reused the body, no second request
    assert p.uw._map_financials("AAPL", fin=uw.fin)                # and the real map reads it (R3/R7)
    res = p.vendor.flush(run_id="job-er")
    assert res["statements_appended"] == 3
    rows = rawstore.query("SELECT * FROM vendor_financials ORDER BY statement")
    assert {(r["ticker"], r["source"], r["report_type"], r["run_id"]) for r in rows} == \
        {("AAPL", "uw", "quarterly", "job-er")}
    assert rawstore.query("SELECT * FROM vendor_valuation") == []   # UW has no valuation (profile 403, G2-08)


def test_an_empty_uw_body_stores_nothing():
    uw = _FinUW(fin={})
    p = _pipeline(uw)
    p._score("AAPL")
    assert p.vendor.flush(run_id="job-er") == vendor_data.EMPTY_RESULT


def test_a_fake_without_financials_still_scores():
    p = _pipeline(_FakeUW())
    assert p._score("AAPL") is not None
    assert p.vendor.flush(run_id="job-er") == vendor_data.EMPTY_RESULT


def test_a_capture_failure_leaves_r3_r7_as_they_were(monkeypatch):
    # The store side never changes the score: the map runs first, the capture has its own try (review N4).
    import RevenueChaser.module_earnings_radar.pipeline as pl
    seen = []
    real = pl._build_earnings_detail
    monkeypatch.setattr(pl, "_build_earnings_detail", lambda hist, recs: seen.append(recs) or real(hist, recs))
    uw = _FinUW()
    p = _pipeline(uw)

    def boom(*a, **k):
        raise RuntimeError("capture broke")

    monkeypatch.setattr(p.vendor, "add_uw", boom)
    assert p._score("AAPL") is not None
    assert seen and seen[0]                     # the mapped records reached the detail
