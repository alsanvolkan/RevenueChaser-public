"""E4 (#372 ER-5): a stored card's ``inputs`` no longer keep the 60-bar slice and the print series twice.

``packet_ohlc`` and ``packet_r2_series`` rebuild both from ``ohlc_year`` / ``earnings_history`` for the
new shape, and read the old shape unchanged. Offline: fake UW, temp store.
"""
import json

import pytest

from RevenueChaser.module_earnings_radar.history import packet_ohlc, packet_r2_series
from RevenueChaser.module_earnings_radar.pipeline import READER_OHLC_SESSIONS, EarningsPipeline
from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW

BARS = [{"date": f"d{i}", "close": 100.0 + i} for i in range(270)]


@pytest.fixture(autouse=True)
def _isolate(rawstore_tmp_db):
    return rawstore_tmp_db


class _R2UW(_FakeUW):
    """Fake UW whose R2 endpoint (``stock_earnings``) answers the given rows."""

    def __init__(self, r2_rows, **ov):
        super().__init__(**ov)
        self._r2_rows = r2_rows

    def stock_earnings(self, ticker):
        return list(self._r2_rows)


def _card(uw):
    cal = {"AAPL": {"earnings_date": "2026-08-14", "full_name": "Aple", "sector": "Technology",
                    "expected_move_perc": 0.05}}
    p = EarningsPipeline(["AAPL"], uw=uw, cal_meta=cal)
    p._screener_cache = {"AAPL": {"close": "100", "call_volume": "5000", "put_volume": "5000",
                                  "total_open_interest": "50000"}}
    p._dgs10_cache = {}
    return p._score("AAPL")


def test_the_60_bar_slice_is_not_stored_twice():
    card = _card(_R2UW([{"actual_eps": "1.1", "report_date": "2026-07-30"}], etf_ohlc=BARS,
                       earnings_history=[{"a": 1}]))
    packet = json.loads(card.raw_packet_json)
    assert "ohlc" not in packet and packet["ohlc_year"] == BARS
    assert packet_ohlc(packet) == packet["ohlc_year"][-READER_OHLC_SESSIONS:]
    assert len(packet_ohlc(packet)) == READER_OHLC_SESSIONS


def test_the_r2_series_is_stored_when_the_r2_endpoint_answered():
    card = _card(_R2UW([{"actual_eps": "1.1", "report_date": "2026-07-30"}], etf_ohlc=BARS,
                       earnings_history=[{"a": 1}]))
    packet = json.loads(card.raw_packet_json)
    assert packet["r2_series"][0]["actual_eps"] == "1.1"
    assert packet_r2_series(packet) == packet["r2_series"]


def test_an_empty_r2_endpoint_stores_no_series_and_the_helper_falls_back():
    card = _card(_R2UW([], etf_ohlc=BARS, earnings_history=[{"a": 1}, {"b": 2}]))
    packet = json.loads(card.raw_packet_json)
    assert "r2_series" not in packet
    assert packet_r2_series(packet) == packet["earnings_history"]


def test_both_helpers_read_an_old_shape_packet_unchanged():
    old = {"ohlc": [1, 2, 3], "ohlc_year": [4, 5, 6], "r2_series": [7, 8], "earnings_history": [9]}
    assert packet_ohlc(old) == [1, 2, 3]
    assert packet_r2_series(old) == [7, 8]
