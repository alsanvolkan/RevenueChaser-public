"""Tests for the stored SEC 13F enrichment (enrich.py), read only since #300 retired its writer."""
from datetime import date

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_sec_13f import enrich


class TestLoadEnrichment:
    def test_reads_the_stored_rows(self, rawstore_tmp_db):
        assert enrich.load_enrichment() == {}  # nothing written yet

        data = {"AAPL": {"price": {"2025-03-31": 150.0}, "insider_pct": 5.0}}
        rawstore.write_rows("sec13f_enrichment", [{"ticker": t, "entry": v} for t, v in data.items()])
        assert enrich.load_enrichment() == data

    def test_malformed_json_returns_empty(self, rawstore_tmp_db):
        rawstore.write_rows("sec13f_enrichment", [{"ticker": "BAD", "entry": "not a dict"}])
        assert enrich.load_enrichment() == {}


class TestLoadShortInterest:
    def test_empty_set_returns_empty(self):
        assert enrich.load_short_interest([]) == ({}, None)

    def test_si_float_fraction_becomes_percent(self, rawstore_tmp_db):
        rawstore.write_rows("short_interest", [
            {"ticker": "AAPL", "session_date": date(2026, 10, 15), "market_date": None,
             "short_interest": None, "si_float": 0.052, "total_float": None, "days_to_cover": None,
             "fee_rate": None, "rebate_rate": None, "short_shares_available": None, "payload": {},
             "source": "uw_short_screener", "fetched_at": 0.0, "run_id": "r", "run_at": 0.0}])
        out, newest = enrich.load_short_interest(["AAPL"])
        assert out["AAPL"]["short_pct"] == pytest.approx(5.2)
        assert out["AAPL"]["short_pct_as_of"] == date(2026, 10, 15)
        assert newest == date(2026, 10, 15)

    def test_newest_non_null_row_wins(self, rawstore_tmp_db):
        base = {"ticker": "AAPL", "market_date": None, "short_interest": None, "total_float": None,
                "days_to_cover": None, "fee_rate": None, "rebate_rate": None,
                "short_shares_available": None, "payload": {}, "source": "uw_interest_float",
                "fetched_at": 0.0, "run_id": "r", "run_at": 0.0}
        rawstore.write_rows("short_interest", [
            {**base, "session_date": date(2026, 10, 13), "si_float": 0.090},
            {**base, "session_date": date(2026, 10, 15), "si_float": 0.052},
            {**base, "session_date": date(2026, 10, 14), "si_float": 0.040}])
        out, newest = enrich.load_short_interest(["AAPL"])
        assert out["AAPL"]["short_pct"] == pytest.approx(5.2)
        assert newest == date(2026, 10, 15)

    def test_tickers_outside_the_wanted_set_are_ignored(self, rawstore_tmp_db):
        rawstore.write_rows("short_interest", [
            {"ticker": "AAPL", "session_date": date(2026, 10, 15), "market_date": None,
             "short_interest": None, "si_float": 0.052, "total_float": None, "days_to_cover": None,
             "fee_rate": None, "rebate_rate": None, "short_shares_available": None, "payload": {},
             "source": "uw_short_screener", "fetched_at": 0.0, "run_id": "r", "run_at": 0.0}])
        assert enrich.load_short_interest(["TSLA"]) == ({}, None)


def test_short_pct_as_of_is_the_session_of_the_row_it_came_from(rawstore_tmp_db):
    """A newer row with a NULL si_float neither wins the value nor dates it (#375 F6 review)."""
    from datetime import date
    from RevenueChaser.core import rawstore
    from RevenueChaser.module_sec_13f import enrich
    rawstore.write_rows("short_interest", [
        {"ticker": "AAA", "session_date": date(2026, 10, 14), "si_float": 0.052, "source": "uw_short_screener"},
        {"ticker": "AAA", "session_date": date(2026, 10, 15), "si_float": None, "source": "yfinance"},
    ])
    out, newest = enrich.load_short_interest(["AAA"])
    assert out["AAA"]["short_pct"] == 5.2 or abs(out["AAA"]["short_pct"] - 5.2) < 1e-9
    assert out["AAA"]["short_pct_as_of"] == date(2026, 10, 14) and newest == date(2026, 10, 14)
