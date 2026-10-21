"""Common K6 (#383 R1, C14, DF104 #161): every ``edgar_financials`` and ``chain_filings`` write carries ``fetched_at``.

- ``sec_edgar.get_fundamentals`` stamps the row with the time of the facts download.
- ``chain_filings``: a downloaded filing is stamped with its download (the ``edgar_documents`` row's ``fetched_at``);
  a re-slice of a stored document carries that document's own stamp, else the write time.
- Old rows are untouched: a stored row is served as it is, and a kept slice keeps its stamp.
Offline, temp store (no SEC request).
"""
from __future__ import annotations

from RevenueChaser.core import edgar_docs, rawstore
from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_value_chain import sync
from RevenueChaser.module_value_chain.tests.test_slice_guard import GOOD_HTML, _row, _seed_sync, _subs

_FACTS = {"entityName": "Acme", "facts": {}}


def test_a_fetched_edgar_financials_row_carries_fetched_at(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr(sec_edgar, "_cik_map", lambda: {"ACME": {"cik": "1", "name": "Acme"}})
    monkeypatch.setattr(sec_edgar, "_fetch_facts", lambda cik: _FACTS)
    monkeypatch.setattr(sec_edgar.time, "time", lambda: 1_790_000_000.0)
    sec_edgar.get_fundamentals("ACME")
    assert rawstore.get("edgar_financials", "ACME")["fetched_at"] == 1_790_000_000.0


def test_a_stored_edgar_financials_row_is_untouched(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("edgar_financials", [{"ticker": "OLD", "annual": [{"fiscal_year": 2024, "revenue": 1.0}],
                                              "quarterly": [], "meta": {"source": "sec_edgar"}}])
    monkeypatch.setattr(sec_edgar, "_fetch_facts", lambda cik: (_ for _ in ()).throw(AssertionError("fetched")))
    sec_edgar.get_fundamentals("OLD")
    assert rawstore.get("edgar_financials", "OLD")["fetched_at"] is None


def test_a_downloaded_filing_is_stamped_with_its_download(rawstore_tmp_db, monkeypatch):
    _seed_sync(monkeypatch, _subs("acc-2"), GOOD_HTML)
    assert sync.sync_one("NVDA", "1045810")["status"] == "updated"
    doc = rawstore.get("edgar_documents", "acc-2")
    row = rawstore.get("chain_filings", "NVDA")
    assert row["fetched_at"] is not None and row["fetched_at"] == doc["fetched_at"]


def test_a_reslice_carries_the_stored_documents_stamp(rawstore_tmp_db, monkeypatch):
    calls = _seed_sync(monkeypatch, _subs("acc-2"), GOOD_HTML)
    edgar_docs.write_html("1045810", "acc-2", GOOD_HTML)
    rawstore.write_rows("edgar_documents", [{"accession": "acc-2", "cik": "1045810", "tickers": "NVDA",
                                             "form": "10-K", "filed": "2026-02-01", "fetched_at": 1_780_000_000.0,
                                             "source": "sec_edgar", "run_at": 1_780_000_000.0}])
    assert sync.sync_one("NVDA", "1045810")["status"] == "updated" and calls["n"] == 0
    assert rawstore.get("chain_filings", "NVDA")["fetched_at"] == 1_780_000_000.0


def test_a_reslice_without_a_document_row_is_stamped_at_the_write(rawstore_tmp_db, monkeypatch):
    calls = _seed_sync(monkeypatch, _subs("acc-2"), GOOD_HTML)
    edgar_docs.write_html("1045810", "acc-2", GOOD_HTML)               # the file without its metadata row
    from RevenueChaser.module_value_chain import edgar_text
    monkeypatch.setattr(edgar_text.time, "time", lambda: 1_791_000_000.0)
    assert sync.sync_one("NVDA", "1045810")["status"] == "updated" and calls["n"] == 0
    assert rawstore.get("chain_filings", "NVDA")["fetched_at"] == 1_791_000_000.0


def test_a_kept_slice_keeps_its_old_stamp(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_value_chain.tests.test_slice_guard import BAD_HTML
    _seed_sync(monkeypatch, _subs("acc-2"), BAD_HTML)
    rawstore.write_rows("chain_filings", [_row()])                   # an old row, written before K6
    assert sync.sync_one("NVDA", "1045810")["status"] == "kept"
    assert rawstore.get("chain_filings", "NVDA")["fetched_at"] is None
