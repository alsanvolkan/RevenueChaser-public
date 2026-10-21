"""#116 C6: a stored ``ok`` slice is never replaced by a worse one (no network).

Both writers of ``chain_filings`` (the sync and ``fetch_corpus``) go through
``engine.store_slices``. A rejected filing's raw document stays in ``edgar_docs``,
so the next sync re-slices it locally instead of downloading it again.
"""
from __future__ import annotations

import logging

from RevenueChaser.core import edgar_docs, rawstore
from RevenueChaser.module_value_chain import engine, sync

_ITEM = ("We purchase wafers from Taiwan Semiconductor Manufacturing Company Limited. "
         + "We sell products worldwide through distributors. " * 60)
GOOD_HTML = (f"<html><body><p><b>Item 1. Business</b></p><p>{_ITEM}</p>"
             "<p><b>Item 1A. Risk Factors</b></p><p>Risks.</p></body></html>")
BAD_HTML = "<html><body><p>Exhibit index only.</p></body></html>"


def _row(ticker="NVDA", acc="acc-1", quality="ok", text="old text", filed="2025-02-01"):
    return {"ticker": ticker, "cik": "1045810", "form": "10-K", "filed": filed,
            "accession": acc, "primary_doc": "u", "item_text": text, "quality": quality}


def _subs(accession, form="10-K", filed="2026-02-01"):
    return {"filings": {"recent": {
        "form": [form], "filingDate": [filed],
        "accessionNumber": [accession], "primaryDocument": ["doc.htm"]}}}


def _seed_sync(monkeypatch, subs, html, tickers=(("NVDA", "1045810"),)):
    rawstore.replace_all("equity_universe", [{"ticker": t, "market_cap": 1e9, "name": t}
                                             for t, _ in tickers])
    rawstore.write_rows("sec_company_tickers", [{"ticker": t, "cik": c, "name": t}
                                                for t, c in tickers])
    monkeypatch.setattr(sync.sec_edgar, "get_json", lambda url, client=None: subs)
    calls = {"n": 0}

    def _fake_get_text(url, client=None):
        calls["n"] += 1
        return html

    monkeypatch.setattr(sync.edgar_text.sec_edgar, "get_text", _fake_get_text)
    return calls


# ── the shared write helper ────────────────────────────────────────────────
def test_store_slices_keeps_ok_and_logs(rawstore_tmp_db, caplog):
    rawstore.write_rows("chain_filings", [_row()])
    with caplog.at_level(logging.WARNING):
        written, kept = engine.store_slices([_row(acc="acc-2", quality="missing", text="")])
    assert (written, kept) == (0, 1)
    row = rawstore.get("chain_filings", "NVDA")
    assert (row["accession"], row["quality"], row["item_text"]) == ("acc-1", "ok", "old text")
    assert any("kept ok slice acc-1" in r.getMessage() and "acc-2" in r.getMessage()
               for r in caplog.records)


def test_store_slices_lets_a_newer_ok_replace(rawstore_tmp_db):
    rawstore.write_rows("chain_filings", [_row()])
    assert engine.store_slices([_row(acc="acc-2", text="new text")]) == (1, 0)
    assert rawstore.get("chain_filings", "NVDA")["accession"] == "acc-2"


def test_store_slices_keeps_ok_against_overrun(rawstore_tmp_db):
    rawstore.write_rows("chain_filings", [_row()])
    assert engine.store_slices([_row(acc="acc-2", quality="overrun", text="x" * 9000)]) == (0, 1)
    assert rawstore.get("chain_filings", "NVDA")["quality"] == "ok"


def test_store_slices_replaces_a_non_ok_row(rawstore_tmp_db):
    rawstore.write_rows("chain_filings", [_row(quality="short", text="3")])
    assert engine.store_slices([_row(acc="acc-2", quality="missing", text="")]) == (1, 0)


# ── sync path ──────────────────────────────────────────────────────────────
def test_sync_keeps_ok_row_when_new_filing_slices_worse(rawstore_tmp_db, monkeypatch):
    calls = _seed_sync(monkeypatch, _subs("acc-2"), BAD_HTML)
    rawstore.write_rows("chain_filings", [_row()])
    res = sync.sync_one("NVDA", "1045810")
    assert res["status"] == "kept" and calls["n"] == 1
    row = rawstore.get("chain_filings", "NVDA")
    assert (row["accession"], row["quality"]) == ("acc-1", "ok")
    # the rejected filing's raw doc is kept: it is the record that it was seen
    assert edgar_docs.exists("1045810", "acc-2")


def test_second_sync_reslices_locally_without_download(rawstore_tmp_db, monkeypatch):
    calls = _seed_sync(monkeypatch, _subs("acc-2"), BAD_HTML)
    rawstore.write_rows("chain_filings", [_row()])
    sync.sync_filings()
    out = sync.sync_filings()
    assert calls["n"] == 1, "the rejected filing was downloaded again"
    assert out.get("kept") == 1
    assert rawstore.get("chain_filings", "NVDA")["accession"] == "acc-1"


def test_local_reslice_picks_up_a_better_slice(rawstore_tmp_db, monkeypatch):
    """A stored doc that now slices ``ok`` (e.g. after a pattern fix) is written."""
    calls = _seed_sync(monkeypatch, _subs("acc-2"), GOOD_HTML)
    edgar_docs.write_html("1045810", "acc-2", GOOD_HTML)
    rawstore.write_rows("chain_filings", [_row()])
    res = sync.sync_one("NVDA", "1045810")
    assert res["status"] == "updated" and calls["n"] == 0
    row = rawstore.get("chain_filings", "NVDA")
    assert row["accession"] == "acc-2" and row["quality"] == "ok"


def test_dual_class_pair_sharing_an_accession_both_update(rawstore_tmp_db, monkeypatch):
    calls = _seed_sync(monkeypatch, _subs("acc-g"), GOOD_HTML,
                       tickers=(("GOOGL", "1652044"), ("GOOG", "1652044")))
    assert sync.sync_one("GOOGL", "1652044")["status"] == "updated"
    assert sync.sync_one("GOOG", "1652044")["status"] == "updated"
    assert calls["n"] == 1  # one download, the second share class re-slices locally
    assert {rawstore.get("chain_filings", t)["accession"] for t in ("GOOGL", "GOOG")} == {"acc-g"}


def test_sync_force_keeps_ok(rawstore_tmp_db, monkeypatch):
    calls = _seed_sync(monkeypatch, _subs("acc-1"), BAD_HTML)
    rawstore.write_rows("chain_filings", [_row()])
    assert sync.sync_one("NVDA", "1045810", force=True)["status"] == "kept" and calls["n"] == 1
    assert rawstore.get("chain_filings", "NVDA")["quality"] == "ok"


def test_sync_ok_replaces_with_newer_ok(rawstore_tmp_db, monkeypatch):
    calls = _seed_sync(monkeypatch, _subs("acc-2"), GOOD_HTML)
    rawstore.write_rows("chain_filings", [_row()])
    assert sync.sync_one("NVDA", "1045810")["status"] == "updated" and calls["n"] == 1
    assert rawstore.get("chain_filings", "NVDA")["accession"] == "acc-2"


# ── Fetch path ─────────────────────────────────────────────────────────────
def test_fetch_corpus_force_keeps_ok(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("chain_filings", [_row()])
    monkeypatch.setattr(engine.sec_edgar, "_cik_map", lambda: {"NVDA": {"cik": "1045810"}})
    monkeypatch.setattr(engine.edgar_text, "fetch_filing",
                        lambda t, cik, client=None: _row(acc="acc-2", quality="missing", text=""))
    out = engine.fetch_corpus(tickers=["NVDA"], force=True)
    row = rawstore.get("chain_filings", "NVDA")
    assert (row["accession"], row["quality"]) == ("acc-1", "ok")
    assert out["kept"] == 1 and out["fetched"] == 0


def test_store_slices_keeps_an_overrun_row_against_short_or_missing(rawstore_tmp_db, caplog):
    # An overrun slice still holds all of Item 1, so a worse slice must not wipe it.
    rawstore.write_rows("chain_filings", [_row(quality="overrun", text="full item 1 " * 500)])
    with caplog.at_level(logging.WARNING):
        assert engine.store_slices([_row(acc="acc-2", quality="missing", text="")]) == (0, 1)
    assert any("kept overrun slice acc-1" in r.getMessage() for r in caplog.records)
    assert engine.store_slices([_row(acc="acc-3", quality="short", text="3")]) == (0, 1)
    assert rawstore.get("chain_filings", "NVDA")["quality"] == "overrun"
    assert engine.store_slices([_row(acc="acc-4", quality="overrun", text="newer")]) == (1, 0)
    assert engine.store_slices([_row(acc="acc-5", quality="ok", text="ok")]) == (1, 0)


def test_store_slices_rereads_after_a_concurrent_ok_write(rawstore_tmp_db, monkeypatch):
    # Another writer commits an ok row between the guard's read and its write;
    # atomic() must lose that race, retry, re-read and keep the ok row.
    rawstore.write_rows("chain_filings", [_row(quality="short", text="3")])
    real = rawstore._apply_write
    raced = []

    def racing(conn, name, spec, rows):
        if name == "chain_filings" and not raced:
            raced.append(True)
            rawstore.write_rows("chain_filings", [_row(acc="acc-ok", text="good text")])
        return real(conn, name, spec, rows)

    monkeypatch.setattr(rawstore, "_apply_write", racing)
    assert engine.store_slices([_row(acc="acc-2", quality="missing", text="")]) == (0, 1)
    assert raced
    row = rawstore.get("chain_filings", "NVDA")
    assert (row["accession"], row["quality"]) == ("acc-ok", "ok")
