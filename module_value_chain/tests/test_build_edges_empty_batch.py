"""``build_edges`` never wipes the stored edges with an empty batch (#284, B9).

A degraded run (every filing missing or without Item text) produced no edges, and the
``replace_all`` that persists them emptied ``chain_mentions``: the board lost its last good data.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_value_chain import engine
from RevenueChaser.module_value_chain.tests.test_value_chain import _TEN_K

_OLD_EDGE = {"src": "NVDA", "dst_name": "TSMC", "dst_ticker": "TSM",
             "relation": "supplier", "snippet": "s", "filed": "2026-02-01"}


def _filing(**kw):
    return {"ticker": "NVDA", "cik": "1", "form": "10-K", "filed": "2026-02-01",
            "accession": "acc", "primary_doc": "http://x", "item_text": "We make chips.",
            "quality": "ok", **kw}


def _stored():
    return [(r["src"], r["dst_name"], r["relation"]) for r in rawstore.get_all("chain_mentions")]


@pytest.mark.parametrize("filings", [
    [],
    [_filing(quality="missing", item_text="")],
    [_filing(item_text="We make chips and sell them worldwide.")],
], ids=["empty corpus", "only missing filings", "text without any named company"])
def test_empty_batch_keeps_the_stored_edges_and_says_why(rawstore_tmp_db, caplog, filings):
    rawstore.ensure_table("chain_mentions")
    rawstore.write_rows("chain_mentions", [_OLD_EDGE])
    if filings:
        rawstore.write_rows("chain_filings", filings)
    with caplog.at_level("WARNING"):
        edges = engine.build_edges()
    assert _stored() == [("NVDA", "TSMC", "supplier")]
    # what is returned is what the store holds, so the board, n_mentions and a later Refresh agree
    assert [(e["src"], e["dst_name"]) for e in edges] == [("NVDA", "TSMC")]
    assert any("chain_mentions" in r.getMessage() and "kept" in r.getMessage()
               for r in caplog.records)


def test_empty_batch_on_a_cold_store_is_a_quiet_noop(rawstore_tmp_db):
    assert engine.build_edges() == []
    assert _stored() == []


def test_a_non_empty_batch_still_replaces_the_stored_edges(rawstore_tmp_db):
    rawstore.ensure_table("chain_mentions")
    rawstore.write_rows("chain_mentions", [{**_OLD_EDGE, "dst_name": "GONE CORP", "dst_ticker": ""}])
    rawstore.write_rows("chain_filings", [_filing(item_text=_TEN_K)])
    edges = engine.build_edges()
    assert ("TSM", "supplier") in {(e["dst_ticker"], e["relation"]) for e in edges}
    assert "GONE CORP" not in {name for _, name, _ in _stored()}
    assert len(_stored()) == len(edges)
