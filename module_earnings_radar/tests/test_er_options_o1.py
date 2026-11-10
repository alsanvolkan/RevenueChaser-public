"""O1 = A (avo, 10 Nov 2026): R4's GEX levels, spot exposures by strike and term structure come from the options
store's earnings set (``store_reads.earnings_options``) when it holds the evening for the card's report; otherwise,
and for each part it holds empty, the pipeline asks UW live as before."""
from datetime import date

import pytest

from RevenueChaser.module_earnings_radar import store_reads
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline

REPORT = "2026-08-14"
CAL = {"AAPL": {"earnings_date": REPORT, "full_name": "Apple", "expected_move_perc": 0.05}}
STRIKES = [{"strike": k, "call_gamma_oi": 1000.0, "put_gamma_oi": -900.0, "call_gamma_vol": 10.0,
            "put_gamma_vol": -9.0} for k in (95.0, 100.0, 105.0)]
TERM = [{"dte": 3, "volatility": 0.6}, {"dte": 45, "volatility": 0.3}]


@pytest.fixture(autouse=True)
def _isolate(rawstore_tmp_db):
    return rawstore_tmp_db


class _UW:
    """Counts the three R4 calls; every other answer is empty."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: {} if name in ("earnings_data", "ticker_info", "aggregate_flow") else []

    def gex_levels(self, ticker):
        self.calls.append("gex_levels")
        return {"gamma_flip": 99.0}

    def spot_exposures_by_strike(self, ticker):
        self.calls.append("spot_exposures")
        return STRIKES

    def term_structure(self, ticker):
        self.calls.append("term_structure")
        return TERM


def _pipeline(monkeypatch, stored: dict):
    monkeypatch.setattr(store_reads, "earnings_options", lambda tickers, latest=None: (stored, {"label": "x"}))
    uw = _UW()
    p = EarningsPipeline(["AAPL"], uw=uw, cal_meta=CAL)
    p._screener_cache = {"AAPL": {"close": 100.0}}
    p._warm_er_options(["AAPL"])
    return p, uw


def _row(report=REPORT, **over):
    return {"session_date": date(2026, 8, 13), "report_date": date.fromisoformat(report),
            "gex_levels": {"call_wall": 110.0, "gamma_flip": 102.5, "gamma_magnet": 105.0, "put_wall": 95.0},
            "strikes": STRIKES, "term": TERM, **over}


def test_a_stored_evening_for_this_report_makes_no_r4_call(monkeypatch):
    p, uw = _pipeline(monkeypatch, {"AAPL": _row()})
    card = p._score("AAPL")
    assert uw.calls == []
    assert card.gex_gamma_flip == "$102.50"
    assert p._options_stored == {"AAPL"} and p._options_live == set()


def test_no_stored_evening_asks_uw_live(monkeypatch):
    p, uw = _pipeline(monkeypatch, {})
    p._score("AAPL")
    assert sorted(uw.calls) == ["gex_levels", "spot_exposures", "term_structure"]
    assert p._options_live == {"AAPL"}


def test_an_evening_fetched_for_another_report_is_not_used(monkeypatch):
    p, uw = _pipeline(monkeypatch, {"AAPL": _row(report="2026-05-14")})
    p._score("AAPL")
    assert sorted(uw.calls) == ["gex_levels", "spot_exposures", "term_structure"]


def test_a_part_the_evening_holds_empty_is_asked_live(monkeypatch):
    p, uw = _pipeline(monkeypatch, {"AAPL": _row(strikes=[], term=[],
                                                 gex_levels={"call_wall": None, "gamma_flip": None,
                                                             "gamma_magnet": None, "put_wall": None})})
    p._score("AAPL")
    assert sorted(uw.calls) == ["gex_levels", "spot_exposures", "term_structure"]
