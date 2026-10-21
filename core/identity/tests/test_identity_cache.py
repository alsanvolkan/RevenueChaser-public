"""The last-good source cache: saved per (source, ticker), never blanked by a failing source."""
from __future__ import annotations

from RevenueChaser.core import rawstore
from RevenueChaser.core.identity import cache
from RevenueChaser.core.identity.sources import SourceResult


def figi_ok(sector_type="Common Stock"):
    return SourceResult("openfigi", "ok", [
        {"ticker": "AAPL", "status": "ok", "security_type": sector_type, "security_type2": "Common Stock",
         "market_sector": "Equity", "name": "APPLE INC"},
        {"ticker": "PDI", "status": "ok", "security_type": "Closed-End Fund", "security_type2": "Mutual Fund",
         "market_sector": "Equity", "name": "PIMCO DYNAMIC INCOME FUND"}], None, "2026-09-30T10:00:00Z")


def by_ticker(source):
    return {r["ticker"]: r for r in cache.last_good(source)}


def test_ok_rows_are_saved_with_typed_columns_and_payload():
    summary = cache.save_results([figi_ok()])
    assert summary["openfigi"]["saved"] == 2
    rows = rawstore.get_all("identity_source_cache")
    assert {r["source"] for r in rows} == {"openfigi"}
    aapl = next(r for r in rows if r["ticker"] == "AAPL")
    assert aapl["security_type"] == "Common Stock" and aapl["security_type2"] == "Common Stock"
    assert aapl["status"] == "ok" and aapl["fetched_at"] == "2026-09-30T10:00:00Z"
    assert aapl["payload"] == {"market_sector": "Equity", "name": "APPLE INC"}


def test_typed_filter_columns_are_real_columns_not_payload():
    cache.save_results([
        SourceResult("sec_submissions", "ok", [
            {"cik": "1370755", "ticker": "TCPC", "status": "ok", "sic": "", "name": "BlackRock TCP",
             "forms": ["10-K", "N-54A", "N-CSR"], "shell_closed": False}]),
        SourceResult("nasdaq", "ok", [{"ticker": "AAAP", "security_name": "Pacer ETF", "is_etf": True,
                                        "exchange": "Q"}]),
        SourceResult("uw_sector", "ok", [{"ticker": "OZK", "sector": "Financials", "industry": "Banks",
                                          "security_type": "Common Stock", "market_cap": 5.0e9}])])
    rows = rawstore.query("SELECT source, ticker, cik, forms, shell_closed, is_etf, sector, industry, "
                          "security_type FROM identity_source_cache ORDER BY source")
    by = {r["source"]: r for r in rows}
    assert by["sec_submissions"]["forms"] == "10-K,N-54A,N-CSR"
    assert by["sec_submissions"]["cik"] == "1370755" and by["sec_submissions"]["shell_closed"] is False
    assert by["nasdaq"]["is_etf"] is True
    assert by["uw_sector"]["sector"] == "Financials" and by["uw_sector"]["industry"] == "Banks"
    # a reader can filter on them in SQL
    hit = rawstore.query("SELECT ticker FROM identity_source_cache WHERE is_etf AND source = 'nasdaq'")
    assert hit == [{"ticker": "AAAP"}]
    hit = rawstore.query("SELECT ticker FROM identity_source_cache WHERE forms LIKE '%N-54A%'")
    assert hit == [{"ticker": "TCPC"}]


def test_last_good_returns_rows_with_payload_merged_back():
    cache.save_results([figi_ok()])
    rows = cache.last_good("openfigi")
    assert [r["ticker"] for r in rows] == ["AAPL", "PDI"]
    aapl = rows[0]
    assert aapl["security_type"] == "Common Stock" and aapl["name"] == "APPLE INC"
    assert aapl["market_sector"] == "Equity" and aapl["fetched_at"] == "2026-09-30T10:00:00Z"


def test_last_good_round_trips_lists_and_booleans():
    cache.save_results([SourceResult("sec_submissions", "ok", [
        {"cik": "5", "status": "ok", "forms": ["10-K", "8-K"], "shell_closed": True, "items_8k": ["5.06"]}])])
    row = cache.last_good("sec_submissions")[0]
    assert row["ticker"] == "5"                     # the CIK keys a row that has no ticker
    assert row["forms"] == ["10-K", "8-K"] and row["shell_closed"] is True
    assert row["items_8k"] == ["5.06"]


def test_the_uw_next_earnings_date_rides_in_the_payload_and_comes_back():
    cache.save_results([SourceResult("uw_sector", "ok", [
        {"ticker": "AAPL", "sector": "Technology", "industry": "Consumer Electronics", "security_type": "Common Stock",
         "market_cap": 3.0e12, "next_earnings_date": "2026-10-30"},
        {"ticker": "ZZZ", "sector": "Energy", "industry": "Oil & Gas E&P", "security_type": "Common Stock",
         "market_cap": 1.0e9, "next_earnings_date": None}], None, "2026-09-30T10:00:00Z")])
    stored = rawstore.get("identity_source_cache", ("uw_sector", "AAPL"))
    assert stored["payload"]["next_earnings_date"] == "2026-10-30"      # not a typed column: it stays in the payload
    rows = by_ticker("uw_sector")
    assert rows["AAPL"]["next_earnings_date"] == "2026-10-30"
    assert rows["ZZZ"]["next_earnings_date"] is None


def test_the_openfigi_figi_fields_ride_in_the_payload_and_come_back():
    cache.save_results([SourceResult("openfigi", "ok", [
        {"ticker": "AAPL", "status": "ok", "security_type": "Common Stock", "security_type2": "Common Stock",
         "market_sector": "Equity", "name": "APPLE INC", "figi": "BBG000B9XRY4", "composite_figi": "BBG000B9XRY4",
         "share_class_figi": "BBG001S5N8V8", "exch_code": "US", "security_description": "AAPL"}],
        None, "2026-09-30T10:00:00Z")])
    stored = rawstore.get("identity_source_cache", ("openfigi", "AAPL"))["payload"]
    assert stored["composite_figi"] == "BBG000B9XRY4" and stored["share_class_figi"] == "BBG001S5N8V8"
    row = by_ticker("openfigi")["AAPL"]
    assert (row["figi"], row["composite_figi"], row["share_class_figi"], row["exch_code"],
            row["security_description"]) == ("BBG000B9XRY4", "BBG000B9XRY4", "BBG001S5N8V8", "US", "AAPL")


def test_last_good_of_an_unknown_source_is_empty():
    assert cache.last_good("nope") == []


# -- never blank on failure --------------------------------------------------------
def test_an_error_result_does_not_overwrite_or_delete_ok_rows():
    cache.save_results([figi_ok()])
    before = cache.last_good("openfigi")
    failed = SourceResult("openfigi", "error", [], "OpenFIGI HTTP 503", "2026-10-01T10:00:00Z")
    summary = cache.save_results([failed])
    assert cache.last_good("openfigi") == before
    assert summary["openfigi"]["saved"] == 0 and "503" in summary["openfigi"]["note"]


def test_an_error_result_carrying_rows_cannot_overwrite_existing_keys():
    cache.save_results([figi_ok()])
    before = by_ticker("openfigi")
    bad = SourceResult("openfigi", "error", [
        {"ticker": "AAPL", "status": "ok", "security_type": "GARBAGE", "security_type2": None},
        {"ticker": "PDI", "status": "no_match", "security_type": None}], "boom", "2026-10-01T10:00:00Z")
    cache.save_results([bad])
    assert by_ticker("openfigi") == before
    assert by_ticker("openfigi")["AAPL"]["security_type"] == "Common Stock"


def test_a_skipped_result_leaves_the_cache_alone():
    cache.save_results([SourceResult("uw_sector", "ok", [{"ticker": "AAPL", "sector": "Technology"}])])
    before = cache.last_good("uw_sector")
    cache.save_results([SourceResult("uw_sector", "skipped", [], "no key")])
    assert cache.last_good("uw_sector") == before


def test_partial_progress_of_an_errored_run_fills_only_new_keys():
    cache.save_results([figi_ok()])
    partial = SourceResult("openfigi", "error", [
        {"ticker": "AAPL", "status": "ok", "security_type": "GARBAGE"},
        {"ticker": "MSFT", "status": "ok", "security_type": "Common Stock", "name": "MICROSOFT"},
        {"ticker": "BAD", "status": "error", "error": "x"}], "stopped early")
    summary = cache.save_results([partial])
    got = by_ticker("openfigi")
    assert got["AAPL"]["security_type"] == "Common Stock"      # untouched
    assert got["MSFT"]["name"] == "MICROSOFT"                   # new answer kept
    assert "BAD" not in got
    assert summary["openfigi"]["saved"] == 1


def test_an_ok_result_with_no_rows_deletes_nothing():
    cache.save_results([figi_ok()])
    cache.save_results([SourceResult("openfigi", "ok", [])])
    assert len(cache.last_good("openfigi")) == 2


def test_an_error_row_inside_an_ok_result_keeps_the_old_answer():
    cache.save_results([figi_ok()])
    mixed = SourceResult("openfigi", "ok", [
        {"ticker": "AAPL", "status": "error", "error": "rate", "security_type": None},
        {"ticker": "PDI", "status": "ok", "security_type": "ETP", "security_type2": "ETF"}])
    summary = cache.save_results([mixed])
    got = by_ticker("openfigi")
    assert got["AAPL"]["security_type"] == "Common Stock"
    assert got["PDI"]["security_type"] == "ETP"
    assert summary["openfigi"]["saved"] == 1


def test_a_later_ok_result_updates_the_answer():
    cache.save_results([figi_ok()])
    newer = figi_ok("ADR")
    newer.fetched_at = "2026-10-05T00:00:00Z"
    cache.save_results([newer])
    row = by_ticker("openfigi")["AAPL"]
    assert row["security_type"] == "ADR" and row["fetched_at"] == "2026-10-05T00:00:00Z"
    assert rawstore.count("identity_source_cache") == 2


def test_sources_are_independent():
    cache.save_results([figi_ok(), SourceResult("nasdaq", "ok", [
        {"ticker": "AAPL", "security_name": "Apple", "is_etf": False, "exchange": "Q"}])])
    cache.save_results([SourceResult("openfigi", "error", [], "down")])
    assert len(cache.last_good("nasdaq")) == 1 and len(cache.last_good("openfigi")) == 2


def test_not_found_and_no_match_rows_are_cached_as_answers():
    cache.save_results([SourceResult("sec_submissions", "ok", [
        {"cik": "12345", "status": "not_found"}])])
    assert cache.last_good("sec_submissions")[0]["status"] == "not_found"
