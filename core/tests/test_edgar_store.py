"""EDGAR document metadata store (`.store/edgar.duckdb`) + file round-trip."""
from __future__ import annotations

import duckdb

from RevenueChaser.core import edgar_store as es


def _row(acc="0000320193-25-000001", ticker="AAPL"):
    body = "<html>Item 1. Business ... we design and sell devices.</html>" * 50
    return es.document_row(acc, "320193", ticker, "10-K", "2025-10-31", body,
                           doc_url="https://www.sec.gov/x", fiscal_year="2025")


def test_round_trip(rawstore_tmp_db):
    es.save_document(_row())
    got = es.get_document("0000320193-25-000001")
    assert got is not None
    assert got["tickers"] == "AAPL"
    assert got["form"] == "10-K"
    assert "Item 1. Business" in got["content"]
    assert got["bytes_raw"] > got["bytes_gzip"]  # gzip actually compresses
    assert got["path"].endswith(".html.gz") and got["sha256"]


def test_missing_returns_none(rawstore_tmp_db):
    es.ensure_schema()
    assert es.get_document("nope") is None
    assert es.count() == 0


def test_upsert_is_idempotent(rawstore_tmp_db):
    es.save_document(_row())
    es.save_document(_row())  # same accession -> replace, not duplicate
    assert es.count() == 1


def test_count_missing_file_is_zero(rawstore_tmp_db):
    assert es.count() == 0


def test_compress_decompress_round_trip():
    s = "Item 1. Business — ünïcode"
    assert es.decompress(es.compress(s)) == s
