"""Relatedness reads its business text from the SEC form reader (#841): ``sec/forms``' ``filing_section`` instead of
``sec/filings``' ``filing_item``, with a cap on the uncut section, a readiness guard, a rule for a ``crossref`` cut
and the filing's two note blocks in the prompt.

The ``crossref`` bodies below are the real ones the ``ours-2`` cutter returned on the stored annual reports
(measured read-only, 13 Nov 2026): Diebold Nixdorf's 10-K gives the single character ``4``, Smith+Nephew's 20-F a
page index, America Movil's 20-F nothing.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_relatedness import config, engine, locator, profile_v3, profiles
from RevenueChaser.module_relatedness.tests.test_relatedness import long_body, v3_reply
from RevenueChaser.rcdata import read
from RevenueChaser.rcdata.build.fundamentals import companyfacts, filings
from RevenueChaser.rcdata.build.sec_forms import forms
from RevenueChaser.rcdata.tests import fund_fixture as fx
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401  (fixtures)

BUSINESS = "We build cloud software and developer tools for enterprises. " * 40

# cik 845982, 20-F, ``ours-2`` label ``crossref``: the opening of the 1,212-character body, a page index.
SNN_INDEX = ("A – History and Development of the Company Corporate Headquaters and Regional Office 298\n"
             "Note 1 'Basis of preparation' 215\nGroup Information 274\nAbout Smith+Nephew 297")
CROSSREF_BODIES = {"DBD": "4", "SNN": SNN_INDEX, "AMX": ""}

WHOLE = ("Annual Report and Accounts\nStrategic report\nWe make orthopaedic implants and wound dressings.\n"
         + long_body("We sell to hospitals in more than 100 countries"))


def _stored(http_mock) -> None:  # noqa: F811
    fx.spine()
    fx.sec_routes(http_mock, {"789019": fx.msft_facts()})
    ten_k = fx.ten_k().replace("We develop software and cloud services. Our customers include enterprises. ", "")
    ten_k = ten_k.replace("<p>Item 1. Business</p><p>", "<p>Item 1. Business</p><p>" + BUSINESS, 1)
    fx.filing_routes(http_mock, {"789019": fx.submissions(789019, fx.MSFT_FILINGS, "MICROSOFT CORP")},
                     {"msft-10k.htm": ten_k, "msft-def14a.htm": fx.proxy()})
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-26")
    fx.run(filings.NAME, mode="backfill", to="2026-10-28")


@pytest.fixture
def filings_only(monkeypatch, http_mock):  # noqa: F811
    """The server's state on 13 Nov 2026: ``sec/filings`` built, ``sec/forms`` never."""
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    _stored(http_mock)
    return http_mock


@pytest.fixture
def layer(filings_only):
    fx.run(forms.NAME, "2026-10-28")
    return filings_only


@pytest.fixture
def writer(monkeypatch):
    calls = []
    monkeypatch.setattr(profiles.deepseek, "complete",
                        lambda system, user, **k: calls.append({"system": system, "user": user})
                        or v3_reply(["cloud software"]))
    return calls


# ── the read sites ──────────────────────────────────────────────────────────────────────────────────────────────

def test_the_business_text_is_the_form_readers_row(layer, monkeypatch):
    """The text, its label and its filing come from ``filing_section``; ``filing_item`` is not read."""
    def old_reader(*a, **k):
        raise AssertionError("Relatedness read filing_item")
    for name in ("filing_texts", "filing_text", "filing_documents"):
        monkeypatch.setattr(read, name, old_reader)
    (row,) = read.form_business(["789019"])
    assert engine._filing_text("MSFT") == ("MSFT", row["text"])
    assert "cloud software and developer tools" in row["text"]
    assert engine._filing_quality("MSFT") == row["quality"]
    assert engine._filing_source("MSFT")["accession"] == fx.K25
    whole = engine._whole_document("MSFT")
    assert len(whole["text"]) > len(row["text"]) and whole["source"]["accession"] == fx.K25
    assert engine._filing_input("MSFT")["fallback"] == "item"
    engine._filing_extras("789019", row["text"])                # the whole document for the customer sentences


# ── the readiness guard ─────────────────────────────────────────────────────────────────────────────────────────

def test_a_run_stops_before_any_call_when_sec_forms_has_not_built(filings_only, writer):
    """Without the guard every company would read ``no_text`` and be saved as such."""
    with pytest.raises(engine.RunStopped) as stopped:
        engine.build_profiles(["MSFT"], usd_cap=1.0)
    assert stopped.value.reason == "forms"
    assert writer == [] and not rawstore.get("relatedness_profiles", "MSFT")


def test_a_run_stops_when_the_sections_are_cut_with_another_rule_version(layer, writer, monkeypatch):
    """A run on ``ours-1`` rows would be paid for again once ``sec/forms`` re-cuts with ``ours-2``."""
    assert forms.SECTIONS_VERSION != "next-rules"
    monkeypatch.setattr(config, "FORMS_RULES_VERSION", "next-rules")
    with pytest.raises(engine.RunStopped) as stopped:
        engine.build_profiles(["MSFT"], usd_cap=1.0)
    assert stopped.value.reason == "forms" and forms.SECTIONS_VERSION in str(stopped.value) and writer == []

    monkeypatch.setattr(config, "FORMS_RULES_VERSION", forms.SECTIONS_VERSION)
    engine.build_profiles(["MSFT"], usd_cap=1.0)
    assert len(writer) == 1 and rawstore.get("relatedness_profiles", "MSFT")["status"] == "ok"


def test_the_version_the_guard_waits_for_is_ours_2():
    assert config.FORMS_RULES_VERSION == "ours-2"


def test_off_the_layer_the_guard_checks_nothing():
    engine._forms_ready(["AAA"])


# ── the cap ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _seed(ticker: str, text: str, quality: str = "ok") -> None:
    rawstore.write_rows("chain_filings", [{"ticker": ticker, "cik": "1", "form": "10-K", "filed": "2026-01-01",
                                           "accession": "acc", "primary_doc": "u", "item_text": text,
                                           "quality": quality, "fetched_at": 1.0}])


def test_a_section_over_the_cap_is_read_to_the_cap(rawstore_tmp_db, monkeypatch):
    sentence = "We design and sell accelerated computing platforms.\n"
    _seed("NVDA", sentence * 400)
    monkeypatch.setattr(config, "MAX_INPUT_CHARS", len(sentence) * 100)
    inp = engine._filing_input("NVDA")
    assert inp["fallback"] == "item" and inp["text"] == engine.clean_input(sentence * 100)
    assert len(profile_v3.split_chunks(inp["text"])) == 1


def test_the_cap_is_what_the_owner_was_shown():
    # 72 of the 4,954 companies with usable text are over it; a round is 8,139 first-attempt calls (13 Nov 2026).
    assert config.MAX_INPUT_CHARS == 240_000


def test_the_hash_pass_hashes_what_the_profile_call_sends(rawstore_tmp_db, monkeypatch):
    """``_input_hashes`` rebuilds the row by hand; a cap applied in one place only would restale every long filing
    on every run."""
    sentence = "We design and sell accelerated computing platforms.\n"
    _seed("NVDA", sentence * 400)
    monkeypatch.setattr(config, "MAX_INPUT_CHARS", len(sentence) * 100)
    monkeypatch.setattr(engine, "_context", lambda t, cik, source=None: {})
    sent = engine._prepare("NVDA", "Nvidia", "1")
    assert engine._input_hashes({"1": ["NVDA"]}, {"NVDA": "Nvidia"}) == {"1": sent["hash"]}
    assert sent["hash"] == profiles.input_hash("Nvidia", engine.clean_input(sentence * 100))


# ── a cut the cutter says is in another document ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ticker", sorted(CROSSREF_BODIES))
def test_a_crossref_cut_reads_the_whole_document_and_never_asks_the_locator(rawstore_tmp_db, monkeypatch, ticker):
    _seed(ticker, CROSSREF_BODIES[ticker], "crossref")
    monkeypatch.setattr(engine, "_whole_document", lambda t: {"text": WHOLE, "source": {"accession": "acc"}})
    monkeypatch.setattr(engine, "_cik_for", lambda ts: {t: t for t in ts})
    monkeypatch.setattr(engine, "_context", lambda t, cik, source=None: {})
    seen = []
    monkeypatch.setattr(profiles.deepseek, "complete",
                        lambda system, user, **k: seen.append(system) or v3_reply(["implants"]))
    inp = engine._filing_input(ticker)
    assert inp["fallback"] == "whole_document" and inp["locate"] is None
    assert inp["hash_text"] == inp["text"] == engine.clean_input(WHOLE)
    engine.build_profiles([ticker], usd_cap=1.0)
    assert len(seen) == 1 and seen[0] != locator.system_prompt()
    assert rawstore.get("relatedness_profiles", ticker)["label_source"]["input"] == "whole_document"


def test_a_long_crossref_body_is_still_not_profiled_from(rawstore_tmp_db, monkeypatch):
    """The label, not the length, says the section is elsewhere: a long page index is not a business description."""
    _seed("SNN", long_body(SNN_INDEX), "crossref")
    monkeypatch.setattr(engine, "_whole_document", lambda t: None)
    inp = engine._filing_input("SNN")
    assert inp["fallback"] not in engine.FALLBACKS and inp["locate"] is None


def test_a_suspect_ok_cut_still_asks_the_locator(rawstore_tmp_db, monkeypatch):
    """#835: the form reader's row carries the flag, and the flag keeps its meaning here."""
    _seed("NVDA", long_body("We design gpus"), "ok;suspect:S4")
    monkeypatch.setattr(engine, "_whole_document", lambda t: {"text": WHOLE, "source": None})
    inp = engine._filing_input("NVDA")
    assert inp["fallback"] == "item" and inp["locate"] == {"text": WHOLE, "source": None}


# ── the two note blocks ─────────────────────────────────────────────────────────────────────────────────────────

def test_the_prompt_carries_the_segment_note_and_the_concentration_note():
    user = profile_v3.build_user("Nvidia", "We design gpus.", {
        "segment_note": "business: Compute 100 USD",
        "segment_block": "We report two segments: Compute & Networking and Graphics.",
        "filing_sentences": ["One customer was 13% of revenue."],
        "concentration_block": "Sales to one direct customer represented 13% of total revenue."})
    assert "Segment note (same filing):\nbusiness: Compute 100 USD" in user          # the lines stay
    assert "Segment note as the filing writes it (same filing):\nWe report two segments" in user
    assert "- One customer was 13% of revenue." in user                               # the sentences stay
    assert "Concentration note as the filing writes it (same filing):\nSales to one direct customer" in user
    assert profile_v3.system_prompt().endswith("Prompt version: v3d.")


def test_a_note_block_is_joined_in_document_order_and_cut(monkeypatch):
    asked = []

    def notes(ciks, local_name=None, **k):
        asked.append((list(ciks), local_name))
        return [{"text": " first part "}, {"text": ""}, {"text": "x" * engine.MAX_NOTE_CHARS}]

    monkeypatch.setattr(read, "form_notes", notes)
    block = engine._note_block("1045810", "SegmentReportingDisclosureTextBlock")
    assert asked == [(["1045810"], "SegmentReportingDisclosureTextBlock")]
    assert block.startswith("first part\nxxx") and len(block) == engine.MAX_NOTE_CHARS


def test_the_extras_offer_both_blocks_and_leave_out_an_absent_one(layer, monkeypatch):
    monkeypatch.setattr(read, "form_notes", lambda ciks, local_name=None, **k: (
        [{"text": "We report one segment."}] if local_name == "SegmentReportingDisclosureTextBlock" else []))
    extras = engine._filing_extras("789019", BUSINESS)
    assert extras["segment_block"] == "We report one segment." and "concentration_block" not in extras


def test_a_stored_profile_reads_stale_after_the_version_bump():
    from RevenueChaser.module_relatedness import store
    old = store.cache_key("m", False, "", "v3c", "NVDA", "h")
    assert store.is_stale({"cache_key": old}, {"model": "m", "thinking": False, "effort": "",
                                               "prompt_version": config.PROMPT_VERSION})
