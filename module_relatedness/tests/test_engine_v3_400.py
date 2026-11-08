"""#400 Step 1 in the engine: the v3 profile is written, with its fallbacks, the shell rule and the context.

Every writer call is a fake ``complete`` at the provider boundary (``deepseek.complete``), so the path from the filing
text to the stored row is the real one: the input chain, the chunking, the parse, the merge, the attribute rows and the
JSON columns.
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import config, engine, profile_v3, profiles, store
from RevenueChaser.module_relatedness.tests.test_relatedness import long_body

REPLY = {"summary": "Makes gpus.", "segments": [{"name": "Compute", "share": 0.8, "source": "table",
                                                "products": ["gpu"], "end_markets": ["data center"]}],
         "customers": [{"name": "Dell", "type": "oem", "share": 0.1, "quote": "Dell accounted for 10%."}],
         "suppliers": [{"name": "TSMC", "type": "foundry", "quote": "We rely on TSMC."}],
         "named_competitors": ["AMD"], "business_model": "product", "channel": "indirect", "geography": "global",
         "stage": "mature", "keywords": ["ai", "gpu"]}


class _Calls(list):
    fake = None


@pytest.fixture
def calls(monkeypatch):
    """Record every writer call (system, user) and answer ``REPLY`` (or ``calls.reply``)."""
    seen = _Calls()

    def fake(system, user, **kw):
        seen.append({"system": system, "user": user, **kw})
        reply = fake.reply(user) if callable(fake.reply) else fake.reply
        return json.dumps(reply)

    fake.reply = REPLY
    monkeypatch.setattr(profiles.deepseek, "complete", fake)
    monkeypatch.setattr(engine, "_cik_for", lambda ts: {t: t for t in ts})
    monkeypatch.setattr(engine, "_filing_source", lambda t: None)
    monkeypatch.setattr(engine, "_previous_filing", lambda t: None)
    monkeypatch.setattr(engine, "_whole_document", lambda t: None)
    monkeypatch.setattr(engine, "_context", lambda t, cik, source=None: {})
    seen.fake = fake
    return seen


def _text(monkeypatch, text):
    monkeypatch.setattr(engine, "_filing_text", lambda t: (t, text))


def test_a_v3_profile_is_stored_whole_with_its_attribute_rows(rawstore_tmp_db, monkeypatch, calls):
    _text(monkeypatch, long_body("we make gpus"))
    engine.build_profiles(["NVDA"], usd_cap=1.0)
    assert len(calls) == 1 and calls[0]["json_mode"] is True and calls[0]["thinking"] is False
    assert calls[0]["system"] == profile_v3.system_prompt()
    row = rawstore.get("relatedness_profiles", "NVDA")
    assert row["status"] == "ok" and row["prompt_version"] == "v3"
    assert row["profile_json"]["customers"][0] == {"name": "Dell", "type": "oem", "quote": "Dell accounted for 10%.",
                                                   "share": 0.1}
    assert row["label_source"]["summary"] == "chunk:0" and row["label_source"]["input"] == "item"
    prof = store.load_profiles(["NVDA"])["NVDA"]
    assert prof == profile_v3.to_attributes(row["profile_json"])
    assert prof["products"] == ["gpu"] and prof["customer_types"] == ["oem"]


def test_the_whole_item_is_read_no_12k_cut(rawstore_tmp_db, monkeypatch, calls):
    text = long_body("we make gpus") * 20
    assert 12_000 < len(text) <= profile_v3.CHUNK_MAX
    _text(monkeypatch, text)
    engine.build_profiles(["NVDA"], usd_cap=1.0)
    assert len(calls) == 1 and calls[0]["user"].endswith(text)


def test_a_long_item_is_read_in_chunks_and_merged(rawstore_tmp_db, monkeypatch, calls):
    from RevenueChaser.module_relatedness.tests.test_profile_v3_chunks_400 import conglomerate
    text = conglomerate()
    _text(monkeypatch, text)

    def per_chunk(user):
        part = int(user.split(", part ")[1].split(" of ")[0])
        return {**REPLY, "summary": f"part {part}", "segments": [{"name": f"Seg {part}", "share": 0.5}],
                "keywords": ["common", f"k{part}"]}

    calls.fake.reply = per_chunk
    engine.build_profiles(["HON"], usd_cap=1.0)
    n = len(profile_v3.split_chunks(text))
    assert n >= 3 and len(calls) == n
    row = rawstore.get("relatedness_profiles", "HON")
    assert row["profile_json"]["summary"] == "part 1"
    assert [s["name"] for s in row["profile_json"]["segments"]] == [f"Seg {i}" for i in range(1, n + 1)]
    assert row["profile_json"]["keywords"][0] == "common"
    assert row["label_source"]["segments"] == "union"
    # The cache key's input hash is over every chunk's exact prompt.
    users = [c["user"] for c in calls]
    assert store.input_hash(row["cache_key"]) == store.prompt_hash(profile_v3.SYSTEM, "\n\n".join(users))


def test_a_short_item_falls_back_to_the_previous_filing_then_the_whole_document(rawstore_tmp_db, monkeypatch,
                                                                                calls):
    _text(monkeypatch, "Item 1. Business. See page 4.")
    monkeypatch.setattr(engine, "_previous_filing", lambda t: {"text": long_body("last year we made gpus"),
                                                               "source": {"cik": "1", "accession": "old"}})
    engine.build_profiles(["NVDA"], usd_cap=1.0)
    row = rawstore.get("relatedness_profiles", "NVDA")
    assert row["status"] == "ok" and row["label_source"]["input"] == "previous_filing"
    assert "last year we made gpus" in calls[-1]["user"]
    assert store.profile_source(row["cache_key"])["accession"] == "old"

    monkeypatch.setattr(engine, "_previous_filing", lambda t: None)
    monkeypatch.setattr(engine, "_whole_document", lambda t: {"text": long_body("the whole 10-K"), "source": None})
    engine.build_profiles(["AMD"], usd_cap=1.0)
    row = rawstore.get("relatedness_profiles", "AMD")
    assert row["label_source"]["input"] == "whole_document" and "the whole 10-K" in calls[-1]["user"]


def test_no_text_anywhere_is_still_no_text_and_costs_nothing(rawstore_tmp_db, monkeypatch, calls):
    _text(monkeypatch, "")
    engine.build_profiles(["NVDA"], usd_cap=1.0)
    assert calls == [] and rawstore.get("relatedness_profiles", "NVDA")["status"] == "no_text"


def test_sic_6770_is_a_shell_with_no_call_and_no_profile(rawstore_tmp_db, monkeypatch, calls):
    _text(monkeypatch, long_body("blank check company"))
    monkeypatch.setattr(engine, "_context", lambda t, cik, source=None: {"sic": "6770"})
    engine.build_profiles(["SPAC"], usd_cap=1.0)
    assert calls == []
    row = rawstore.get("relatedness_profiles", "SPAC")
    assert row["status"] == "shell"
    assert not any(store.load_profiles(["SPAC"])["SPAC"].values())


def test_a_shell_stage_reply_stores_no_profile_and_is_not_asked_again(rawstore_tmp_db, monkeypatch, calls):
    _text(monkeypatch, long_body("we have no operations"))
    calls.fake.reply = {**REPLY, "stage": "shell"}
    engine.build_profiles(["SPAC"], usd_cap=1.0)
    row = rawstore.get("relatedness_profiles", "SPAC")
    assert row["status"] == "shell" and row["profile_json"]["stage"] == "shell"
    assert not any(store.load_profiles(["SPAC"])["SPAC"].values())
    engine.build_profiles(["SPAC"], usd_cap=1.0)
    assert len(calls) == 1                         # done: the same input is not sent again


def test_a_shell_is_neither_a_peer_nor_pending(rawstore_tmp_db, monkeypatch, calls):
    monkeypatch.setattr(engine.vc_engine, "fetch_corpus", lambda *a, **k: None)
    monkeypatch.setattr(engine, "_payload", lambda profiles, thinking=None, stale=None, model=None:
                        {"as_of": "x", "companies": [{"ticker": t} for t in profiles], "peers": {}, "n": len(profiles)})
    monkeypatch.setattr(engine, "_record_history", lambda *a, **k: None)
    _text(monkeypatch, long_body("blank check"))
    monkeypatch.setattr(engine, "_context", lambda t, cik, source=None: {"sic": "6770"})
    payload = engine.run(tickers=["SPAC"], usd_cap=1.0)
    assert payload["n"] == 0 and payload["shell"] == 1 and payload["pending"] == 0


def test_the_prompt_carries_the_context(rawstore_tmp_db, monkeypatch, calls):
    _text(monkeypatch, long_body("we make gpus"))
    monkeypatch.setattr(engine, "_context", lambda t, cik, source=None: {"sic": "3674", "sector": "Technology",
                                                            "segment_note": "Compute: 47,000",
                                                            "candidates": ["Dell", "HP"]})
    engine.build_profiles(["NVDA"], usd_cap=1.0)
    user = calls[0]["user"]
    assert "SIC: 3674" in user and "Sector: Technology" in user and "Compute: 47,000" in user and "Dell; HP" in user


def test_an_unparseable_reply_is_retried_once_uncached(rawstore_tmp_db, monkeypatch, calls):
    _text(monkeypatch, long_body("we make gpus"))
    answers = iter(["not json", json.dumps(REPLY)])
    monkeypatch.setattr(profiles.deepseek, "complete",
                        lambda system, user, **kw: calls.append(kw) or next(answers))
    engine.build_profiles(["NVDA"], usd_cap=1.0)
    assert [c["use_cache"] for c in calls] == [True, False]
    assert rawstore.get("relatedness_profiles", "NVDA")["status"] == "ok"


def test_the_board_mode_has_no_thinking():
    assert not hasattr(config, "THINKING") and not hasattr(config, "THINKING_SAMPLE")
    assert not hasattr(config, "PILOT_N") and not hasattr(config, "MAX_INPUT_CHARS")
    assert config.PROMPT_VERSION == "v3"
    assert store.mode_stamp()["thinking"] is False
