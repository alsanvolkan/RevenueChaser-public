"""Value Chain Refresh is pure and reads no filing text it does not need (#115)."""
from __future__ import annotations

from RevenueChaser.core import rawstore
from RevenueChaser.module_value_chain import engine
from RevenueChaser.module_value_chain.tests.test_value_chain import _TEN_K


def _seed():
    rawstore.write_rows("chain_filings", [{
        "ticker": "NVDA", "cik": "1", "form": "10-K", "filed": "2026-02-01",
        "accession": "acc", "primary_doc": "http://x",
        "item_text": _TEN_K, "quality": "ok"}])
    rawstore.replace_all("chain_mentions", [{
        "src": "NVDA", "dst_name": "TSMC", "dst_ticker": "TSM",
        "relation": "supplier", "snippet": "s", "filed": "2026-02-01"}])
    rawstore.replace_all("chain_meta", [
        {"key": "last_sync", "value": "2026-01-01T00:00:00+00:00"},
        {"key": "n_filings", "value": "1"}, {"key": "n_mentions", "value": "1"}])


def test_refresh_writes_nothing_and_keeps_last_sync(rawstore_tmp_db, monkeypatch):
    _seed()

    def boom(*a, **k):
        raise AssertionError("Refresh must not write the store")
    for name in ("write_rows", "replace_all", "execute", "atomic", "delete"):
        monkeypatch.setattr(rawstore, name, boom)
    payload = engine.run(no_fetch=True, tickers=["NVDA"])
    assert payload["as_of"] == "2026-01-01T00:00:00+00:00"


def test_refresh_never_reads_item_text(rawstore_tmp_db, monkeypatch):
    _seed()
    real = rawstore.get_all
    calls = []

    def spy(name, columns=None):
        calls.append((name, columns))
        return real(name, columns=columns)
    monkeypatch.setattr(rawstore, "get_all", spy)
    engine.run(no_fetch=True, tickers=["NVDA"])
    engine.load_payload()
    filing_reads = [c for n, c in calls if n == "chain_filings"]
    assert filing_reads
    assert all(c and "item_text" not in c for c in filing_reads)


def test_fetch_reads_the_full_text_once(rawstore_tmp_db, monkeypatch):
    _seed()
    real = rawstore.get_all
    calls = []

    def spy(name, columns=None):
        calls.append((name, columns))
        return real(name, columns=columns)
    monkeypatch.setattr(rawstore, "get_all", spy)
    engine.run(no_fetch=False, tickers=["NVDA"])
    full = [c for n, c in calls if n == "chain_filings" and (not c or "item_text" in c)]
    assert len(full) == 1


def test_fetch_stamps_last_sync(rawstore_tmp_db):
    _seed()
    engine.run(no_fetch=False, tickers=["NVDA"])
    last = rawstore.get("chain_meta", "last_sync")["value"]
    assert last != "2026-01-01T00:00:00+00:00"
