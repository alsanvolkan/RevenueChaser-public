"""#166: a failed UW flow request is not "No institutional flow"."""
from __future__ import annotations

import pytest

from RevenueChaser.core.providers.result import ProviderResult
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


def test_a_failed_flow_request_marks_the_card():
    card = _card(_ResultUW(ProviderResult("failed", [], {"status": 500}, "")))
    assert card.flow_source == "failed" and card.inst_flow_label == FAILED_LABEL


def test_an_empty_tape_is_still_no_flow():
    card = _card(_ResultUW(ProviderResult("ok", [], None, "")))
    assert card.flow_source == "none" and card.inst_flow_label == "No institutional flow"


def test_a_404_flow_request_is_not_a_failure():
    card = _card(_ResultUW(ProviderResult("failed", [], {"status": 404}, "")))
    assert card.flow_source == "none" and card.inst_flow_label == "No institutional flow"


def test_flow_that_arrived_keeps_its_source():
    prints = [{"premium": "100", "size": 20, "option_type": "call", "executed_at": "2026-08-13T15:00:00Z"}]
    card = _card(_ResultUW(ProviderResult("ok", prints, None, "")))
    assert card.flow_source == "flow_recent"


def test_a_list_only_client_behaves_as_before():
    assert _card(_UW()).flow_source == "none"
    assert _card(_UW([{"premium": "1", "size": 20, "option_type": "call"}])).flow_source == "flow_recent"
