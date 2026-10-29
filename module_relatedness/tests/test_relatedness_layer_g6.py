"""Relatedness on the fundamentals layer (G6, build plan PR 20): the filing text the plan serves, and the filing a
profile was read from.

On the layer a profile's text is the newest annual report ``sec/filings`` serves (``filing_item``, Value Chain's
splitter and cap, so the same filing gives the same text and the same input hash), the company is the layer's
(``fundamental_company``), and the profile carries the CIK, accession, form and filing day it was read from (its
cache key's ``source``; the payload's ``source``). Off the layer it reads ``chain_filings`` as before, and a profile
made from a ``chain_filings`` row carries that row's filing too.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core import fundamentals_layer, rawstore
from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_relatedness import engine, store
from RevenueChaser.module_relatedness.tests.test_relatedness import _prof
from RevenueChaser.rcdata.build.fundamentals import companyfacts, filings
from RevenueChaser.rcdata.tests import fund_fixture as fx
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401  (fixtures)

BUSINESS = "We build cloud software and developer tools for enterprises. " * 40


@pytest.fixture
def layer(monkeypatch, http_mock):  # noqa: F811
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    monkeypatch.delenv(fundamentals_layer.SOURCE_ENV, raising=False)
    fx.spine()
    fx.sec_routes(http_mock, {"789019": fx.msft_facts()})
    ten_k = fx.ten_k().replace("We develop software and cloud services. Our customers include enterprises. ", "")
    ten_k = ten_k.replace("<p>Item 1. Business</p><p>", "<p>Item 1. Business</p><p>" + BUSINESS, 1)
    fx.filing_routes(http_mock, {"789019": fx.submissions(789019, fx.MSFT_FILINGS, "MICROSOFT CORP")},
                     {"msft-10k.htm": ten_k, "msft-def14a.htm": fx.proxy()})
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-26")
    fx.run(filings.NAME, mode="backfill", to="2026-10-28")
    assert fundamentals_layer.reads_layer()
    return http_mock


def test_the_text_and_the_filing_are_the_layers(layer):
    name, text = engine._filing_text("MSFT")
    assert "cloud software and developer tools" in text
    assert engine._filing_source("MSFT") == {"cik": "789019", "accession": fx.K25, "form": "10-K",
                                             "filed": date(2025, 7, 30)}
    assert engine._filing_source("ORCL") is None and engine._filing_text("ORCL")[1] == ""     # list never read
    assert engine._cik_for(["MSFT", "NOPE"]) == {"MSFT": "789019", "NOPE": ""}


def test_a_profile_carries_the_filing_it_was_read_from(layer, monkeypatch):
    monkeypatch.setattr(engine.profiles_mod, "profile_for",
                        lambda ticker, name, text, **k: _prof(products=["cloud software"]))
    engine.build_profiles(["MSFT"], usd_cap=1.0)
    meta = store.profile_meta(["MSFT"])["MSFT"]
    assert store.profile_source(meta["cache_key"]) == {"cik": "789019", "accession": fx.K25, "form": "10-K",
                                                       "filed": "2025-07-30"}
    (company,) = engine._payload(store.load_profiles(["MSFT"]), {})["companies"]
    assert company["source"]["accession"] == fx.K25 and company["source"]["form"] == "10-K"


def test_off_the_layer_a_profile_carries_its_chain_filings_row(monkeypatch):
    rawstore.write_rows("chain_filings", [{"ticker": "AAA", "cik": "1234", "form": "10-K", "filed": "2026-01-01",
                                           "accession": "acc-1", "primary_doc": "u", "item_text": "we make gpus",
                                           "quality": "ok", "fetched_at": 1.0}])
    assert engine._filing_text("AAA") == ("AAA", "we make gpus")
    assert engine._filing_source("AAA") == {"cik": "1234", "accession": "acc-1", "form": "10-K",
                                            "filed": "2026-01-01"}


def test_a_key_without_a_source_keeps_its_bytes():
    old = store.cache_key("m", True, "high", "v2", "aaa", "h")
    assert "source" not in old and store.profile_source(old) is None
    new = store.cache_key("m", True, "high", "v2", "aaa", "h", source={"cik": "1", "accession": "a"})
    assert store.profile_source(new) == {"cik": "1", "accession": "a", "form": None, "filed": None}
    assert not store.is_stale({"cache_key": new}, {"model": "m", "thinking": True, "effort": "high",
                                                   "prompt_version": "v2"})


def test_the_input_hash_reads_the_layers_text(layer):
    """The staleness hash reads the text a profile would be made from: the layer's, not ``chain_filings``."""
    from RevenueChaser.module_relatedness import profiles as profiles_mod
    want = store.prompt_hash(profiles_mod.SYSTEM, profiles_mod.build_user("Microsoft", engine._filing_text("MSFT")[1]))
    assert engine._input_hashes({"789019": ["MSFT"]}, {"MSFT": "Microsoft"}) == {"789019": want}
