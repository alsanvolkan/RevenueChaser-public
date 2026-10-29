"""Tests for the stored SEC 13F enrichment (enrich.py), read only since #300 retired its writer."""
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

