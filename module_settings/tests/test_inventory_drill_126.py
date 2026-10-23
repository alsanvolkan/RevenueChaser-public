"""The per-ticker drill's dataset presence and identifiers (S10 of the Data Health plan, #126).

Five datasets decide whether a board can score a name: D01 bars, the quote, vendor
statements, EDGAR facts and ownership. The drill reports each one's count and newest
session/date, and the identifiers the identity spine holds. Store only, one query per
table, and a table or column this store does not carry reads as absent — never an
exception.
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.core import rawstore
from RevenueChaser.module_settings import inventory


def _bars(ticker, *days):
    return [{"ticker": ticker, "session_date": date.fromisoformat(day), "close": 1.0,
             "source": "uw", "volume": 1.0} for day in days]


def _seed_every_dataset():
    """One ticker with a row in all five scoring datasets and in ``security_master``."""
    rawstore.write_rows("equity_universe", [
        {"ticker": "MU", "market_cap": 1.0e9, "name": "Micron Technology"}])
    rawstore.write_rows("security_master", [
        {"ticker": "MU", "cik": "0000723125", "figi": "BBG000BLNNH6"}])
    rawstore.write_rows("price_bars_daily", _bars("MU", "2026-01-30", "2026-02-02"))
    rawstore.write_rows("quote_snapshot", [
        {"ticker": "MU", "session_date": date(2026, 10, 20), "source": "yfinance",
         "market_cap": 1.0e9, "price": 100.0, "run_at": 1.0}])
    rawstore.write_rows("vendor_financials", [
        {"ticker": "MU", "source": "yfinance", "statement": "income", "report_type": "annual",
         "period_end": date(2026, 6, 4), "status": "present", "run_at": 1.0}])
    rawstore.write_rows("edgar_financials", [
        {"ticker": "MU", "annual": [
            {"fiscal_year": "2025", "revenue": 1.0, "period_ends": {"revenue": "2025-08-28"}},
            {"fiscal_year": "2026", "revenue": 2.0, "period_ends": {"revenue": "2026-08-27"}}],
         "quarterly": [], "meta": {"source": "sec_edgar"}, "fetched_at": 1.0}])
    rawstore.write_rows("ownership_snapshot", [
        {"ticker": "MU", "session_date": date(2026, 10, 21), "source": "yfinance",
         "institutional_pct": 0.8, "run_at": 1.0}])


def test_the_five_scoring_datasets_report_a_count_and_the_newest_date(rawstore_tmp_db):
    _seed_every_dataset()
    presence = inventory.ticker_detail("MU")["presence"]

    assert presence["price_bars_daily"] == {"bars": 2, "newest": "2026-02-02"}
    assert presence["quote_snapshot"] == {"rows": 1, "newest": "2026-10-20", "source": "yfinance"}
    assert presence["vendor_financials"] == {"rows": 1, "newest": "2026-06-04"}
    assert presence["edgar_financials"] == {"periods": 2, "newest": "2026-08-27"}
    assert presence["ownership_snapshot"] == {"rows": 1, "newest": "2026-10-21", "source": "yfinance"}


def test_a_ticker_with_only_d01_bars_shows_the_other_four_absent(rawstore_tmp_db):
    rawstore.write_rows("price_bars_daily", _bars("NVDA", "2026-02-02"))
    presence = inventory.ticker_detail("NVDA")["presence"]

    assert presence["price_bars_daily"] == {"bars": 1, "newest": "2026-02-02"}
    assert presence["quote_snapshot"] == {"rows": 0, "newest": None, "source": None}
    assert presence["vendor_financials"] == {"rows": 0, "newest": None}
    assert presence["edgar_financials"] == {"periods": 0, "newest": None}
    assert presence["ownership_snapshot"] == {"rows": 0, "newest": None, "source": None}


def test_a_missing_presence_table_is_no_data_not_an_exception(rawstore_tmp_db):
    """A store without the table (or an old one) must render "absent", never raise."""
    _seed_every_dataset()
    for table in ("quote_snapshot", "vendor_financials", "ownership_snapshot", "security_master"):
        rawstore.execute(f"DROP TABLE {table}")
    rawstore.execute("DROP TABLE edgar_financials", db="edgar")

    detail = inventory.ticker_detail("MU")

    assert detail is not None
    presence = detail["presence"]
    assert presence["price_bars_daily"] == {"bars": 2, "newest": "2026-02-02"}
    assert presence["quote_snapshot"] == {"rows": 0, "newest": None, "source": None}
    assert presence["vendor_financials"] == {"rows": 0, "newest": None}
    assert presence["edgar_financials"] == {"periods": 0, "newest": None}
    assert presence["ownership_snapshot"] == {"rows": 0, "newest": None, "source": None}


def test_the_drill_carries_the_identifiers_the_master_holds(rawstore_tmp_db):
    _seed_every_dataset()
    detail = inventory.ticker_detail("MU")

    assert detail["ticker"] == "MU"
    assert detail["cik"] == "0000723125"
    assert detail["figi"] == "BBG000BLNNH6"
    # The columns exist (#127) but nothing is applied for this ticker: no value, not n/a.
    assert detail["cusip"] is None
    assert detail["isin"] is None


def test_the_identifiers_say_na_when_the_columns_are_not_in_this_store(rawstore_tmp_db, monkeypatch):
    """An older store without the columns: n/a, never an exception."""
    _seed_every_dataset()
    monkeypatch.setattr(inventory, "_has_column",
                        lambda table, column: False if column in ("cusip", "isin") else
                        column in inventory._existing_columns(table))
    detail = inventory.ticker_detail("MU")
    assert detail["cusip"] == "n/a" and detail["isin"] == "n/a"


def test_the_identifiers_show_as_soon_as_the_columns_exist(rawstore_tmp_db):
    """CUSIP / ISIN values written on the master show in the drill."""
    _seed_every_dataset()
    rawstore.execute("UPDATE security_master SET cusip = ?, isin = ? WHERE ticker = ?",
                     ["594918104", "US5949181045", "MU"])

    detail = inventory.ticker_detail("MU")

    assert detail["cusip"] == "594918104"
    assert detail["isin"] == "US5949181045"


def test_the_master_cik_beats_the_fallback_but_a_missing_one_still_falls_back(rawstore_tmp_db):
    rawstore.write_rows("equity_universe", [{"ticker": "MU", "name": "Micron"}])
    rawstore.write_rows("security_master", [{"ticker": "MU", "figi": "BBG000BLNNH6"}])
    rawstore.write_rows("chain_filings", [
        {"ticker": "MU", "cik": "0000723125", "form": "10-K", "filed": "2026-02-01"}])

    assert inventory.ticker_detail("MU")["cik"] == "0000723125"

    rawstore.execute("UPDATE security_master SET cik = '0000000001' WHERE ticker = 'MU'")
    assert inventory.ticker_detail("MU")["cik"] == "0000000001"


def test_a_ticker_the_drill_cannot_find_is_still_none(rawstore_tmp_db):
    _seed_every_dataset()
    assert inventory.ticker_detail("ZZZZ") is None


def test_a_ticker_with_only_a_stored_quote_is_found(rawstore_tmp_db):
    """The drill reads the scoring datasets, so a name present only in one of them is not "unknown"."""
    rawstore.write_rows("quote_snapshot", [
        {"ticker": "ONLYQ", "session_date": date(2026, 10, 20), "source": "uw",
         "market_cap": 1.0e9, "run_at": 1.0}])

    detail = inventory.ticker_detail("ONLYQ")

    assert detail is not None
    assert detail["presence"]["quote_snapshot"] == {"rows": 1, "newest": "2026-10-20", "source": "uw"}


def test_the_edgar_newest_period_is_the_newest_annual_record_not_its_position(rawstore_tmp_db):
    """The rows were written oldest first; a row stored out of order still reports the newest end."""
    rawstore.write_rows("edgar_financials", [
        {"ticker": "OUT", "annual": [
            {"fiscal_year": "2026", "revenue": 2.0, "period_ends": {"revenue": "2026-08-27"}},
            {"fiscal_year": "2024", "revenue": 0.5, "period_ends": {"revenue": "2024-08-29"}}],
         "quarterly": [], "meta": {"source": "sec_edgar"}, "fetched_at": 1.0}])

    assert inventory.ticker_detail("OUT")["presence"]["edgar_financials"] == {
        "periods": 2, "newest": "2026-08-27"}
