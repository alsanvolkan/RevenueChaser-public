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


# ── ``read.etf_holdings``: the lines themselves, ticker, name and weight (#642 F9) ──────────────────────────────

def test_holdings_are_each_funds_newest_day_by_weight_with_name_and_weight():
    fx.write_etf_holdings({"SMH": ["NVDA", "INTC"]}, as_of="2026-11-05")
    fx.write_etf_holdings({"SMH": ["NVDA", "TSM"], "ARKQ": ["TSLA", None]}, as_of="2026-11-07")
    rows = read.etf_holdings(["smh", "arkq", "KBE"])
    assert [(r["etf"], r["ticker"], r["name"], r["weight"]) for r in rows] == [
        ("ARKQ", "TSLA", "TSLA NAME", 2.0), ("ARKQ", None, "NONE NAME", 1.0),
        ("SMH", "NVDA", "NVDA NAME", 2.0), ("SMH", "TSM", "TSM NAME", 1.0)]
    assert {str(r["as_of"]) for r in rows} == {"2026-11-07"}


def test_a_spine_file_without_the_holdings_table_is_no_rows():
    """The reader shares the table's name; a file built before the table existed must read as no rows, not as
    DuckDB's replacement scan finding the Python function (#642 PR 3 CI, 11 Nov 2026)."""
    fx.write_security([])
    assert read.etf_holdings(["SMH"]) == []
    assert read.etf_holding_tickers(["SMH"]) == {"SMH": set()}


def test_holdings_of_nothing_or_no_spine_file_are_no_rows():
    assert read.etf_holdings(["SMH"]) == []
    assert read.etf_holdings([]) == []
