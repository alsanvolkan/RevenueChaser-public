"""#166: a failed UW flow request is not "No institutional flow".

Since G9 the Earnings Fetch makes no flow request (the prints come from the options store, ``store_reads``), so a
card can no longer meet a failed one; the label rule stays for any reader that still asks UW. The card side is
``test_options_store_g9`` (``flow_source`` ``option_print``, ``none`` or ``not_on_list``)."""
from __future__ import annotations

import pytest

from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline, _institutional_flow_comment

FAILED_LABEL = "Flow unavailable (provider failure)"


@pytest.fixture(autouse=True)
def _isolate_leaps_cache(rawstore_tmp_db):
    return rawstore_tmp_db


class _UW:
    """List-only client: empty data everywhere (the shape the pipeline tests already use)."""

    def __init__(self, prints=None):
        self.prints = prints or []

    def earnings_data(self, ticker): return {}
    def earnings_history(self, ticker): return []
    def flow_recent(self, ticker): return self.prints
    def aggregate_flow(self, ticker, flow): return {}
    def screener_batch(self, tickers): return {}
    def ticker_info(self, ticker): return {}
    def etf_ohlc(self, ticker, days=60, candle_size="1d"): return []
    def _map_financials(self, ticker): return []
    def gex_levels(self, ticker): return {}
    def ohlc_latest(self, ticker): return {}
    def options_volume(self, ticker): return {}


class _ResultUW(_UW):
    def __init__(self, result):
        super().__init__()
        self.result = result

    def flow_recent_result(self, ticker):
        return self.result


def _card(uw):
    p = EarningsPipeline(["AAPL"], uw=uw, cal_meta={"AAPL": {
        "earnings_date": "2026-08-14", "full_name": "Aple", "sector": "Technology", "expected_move_perc": 0.05}})
    p._screener_cache = {"AAPL": {"close": 100.0}}
    p._dgs10_cache = {}
    return p._score("AAPL")


def test_the_failed_label_is_neutral_and_says_why():
    assert _institutional_flow_comment({}, failed=True) == (FAILED_LABEL, 0.0, "neutral")
    assert _institutional_flow_comment({"inst_call": 9e6, "inst_put": 0}, failed=True)[0] == FAILED_LABEL


def test_without_a_failure_the_label_is_unchanged():
    assert _institutional_flow_comment({})[0] == "No institutional flow"
    assert _institutional_flow_comment({}, failed=False)[0] == "No institutional flow"

