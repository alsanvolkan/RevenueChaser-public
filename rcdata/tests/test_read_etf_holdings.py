"""``rcdata.read.etf_holding_tickers``: each fund's newest day of the spine's whole ETF holdings (D4 of the
9 Nov 2026 review, E1 of #342), the source Relatedness reads instead of ``raw.duckdb.etf_holdings``."""
from __future__ import annotations

from RevenueChaser.rcdata import read

from . import spine_fixture as fx


def test_each_fund_is_read_from_its_own_newest_day():
    fx.write_etf_holdings({"SMH": ["NVDA", "INTC"], "XBI": ["VRTX"]}, as_of="2026-11-05")
    fx.write_etf_holdings({"SMH": ["NVDA", "TSM"]}, as_of="2026-11-07")
    out = read.etf_holding_tickers(["SMH", "xbi"])
    assert out == {"SMH": {"NVDA", "TSM"}, "XBI": {"VRTX"}}


def test_a_line_with_no_ticker_is_no_holding_and_an_unheld_fund_is_an_empty_set():
    fx.write_etf_holdings({"ARKQ": ["TSLA", None]})
    assert read.etf_holding_tickers(["ARKQ", "KBE"]) == {"ARKQ": {"TSLA"}, "KBE": set()}


def test_no_spine_file_is_every_fund_empty_not_an_error():
    assert read.etf_holding_tickers(["SMH"]) == {"SMH": set()}
    assert read.etf_holding_tickers([]) == {}
