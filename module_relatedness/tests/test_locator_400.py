"""Item 1 found by the writer from the heading list (avo, 8 Nov 2026: the rule-based cut stays the default; only an
``overrun``, ``missing`` or ``short`` cut asks the writer where Business starts and ends, from the headings only).
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import engine, locator, profiles, store
from RevenueChaser.module_relatedness.tests.test_relatedness import long_body

BODY = long_body("We design gpus for data centers.")
DOC = ("Table of Contents\nItem 1. Business\nItem 1A. Risk Factors\nPart I\n"
       "Our Company\n" + BODY + "\nCompetition\n" + BODY + "\nRisk Factors\n" + "Risks are many. " * 400
       + "\nProperties\nWe own a campus.\n")
PROFILE = {"summary": "Makes gpus.", "segments": [{"name": "Compute", "products": ["gpu"]}], "stage": "mature",
           "keywords": ["gpu"]}


def test_headings_are_the_heading_lines_with_their_offsets():
    heads = locator.headings(DOC)
    names = [h for _o, h in heads]
    assert names[:5] == ["Table of Contents", "Item 1. Business", "Item 1A. Risk Factors", "Part I", "Our Company"]
    assert "Competition" in names and "Risk Factors" in names and "Properties" in names
    for off, h in heads:
        assert DOC[off:off + len(h)] == h
    assert not any("data centers" in h for h in names)          # prose is not a heading


def test_headings_are_capped():
    doc = "Section Heading Here\nbody text here.\n" * 2000
    assert len(locator.headings(doc)) == locator.MAX_HEADINGS


def test_the_prompt_sends_only_numbered_headings():
    user = locator.build_user("NVIDIA", "10-K", locator.headings(DOC))
    assert "0: Table of Contents" in user and "4: Our Company" in user
    assert "data centers" not in user and len(user) < 600


@pytest.mark.parametrize("raw, n, want", [
    ('{"start": 4, "end": 7}', 9, (4, 7)), ('{"start": 4, "end": null}', 9, (4, 9)),
    ('{"start": 7, "end": 4}', 9, None), ('{"start": -1, "end": 3}', 9, None), ('{"start": 4, "end": 99}', 9, None),
    ('{"start": null}', 9, None), ("junk", 9, None), ('{"start": "4", "end": "7"}', 9, (4, 7))])
def test_parse(raw, n, want):
    assert locator.parse(raw, n) == want


def test_cut_returns_the_section_between_the_headings():
    heads = locator.headings(DOC)
    names = [h for _o, h in heads]
    start, end = names.index("Our Company"), names.index("Risk Factors")
    text = locator.cut(DOC, heads, start, end)
    assert text.startswith("Our Company") and "Competition" in text and "Risks are many" not in text


# ── engine ──────────────────────────────────────────────────────────────────

@pytest.fixture
def world(monkeypatch):
    """An overrun Item (the rule cut ran into Risk Factors), the whole document on hand, and a writer that answers
    the locator from the heading list and the profile from the text."""
    seen = []

    def fake(system, user, **kw):
        seen.append({"system": system, "user": user, **kw})
        if system == locator.system_prompt():
            names = [line.split(": ", 1)[1] for line in user.split("Headings:\n", 1)[1].splitlines()]
            return json.dumps({"start": names.index("Our Company"), "end": names.index("Risk Factors")})
        return json.dumps(PROFILE)

    monkeypatch.setattr(profiles.deepseek, "complete", fake)
    monkeypatch.setattr(engine, "_cik_for", lambda ts: {t: t for t in ts})
    monkeypatch.setattr(engine, "_context", lambda t, cik, source=None: {})
    monkeypatch.setattr(engine, "_whole_document", lambda t: {"text": DOC, "source": {"accession": "acc"}})
    rawstore.write_rows("chain_filings", [{"ticker": "NVDA", "cik": "1", "form": "10-K", "filed": "2026-01-01",
                                           "accession": "acc", "primary_doc": "u",
                                           "item_text": DOC.split("Our Company\n", 1)[1], "quality": "overrun",
                                           "fetched_at": 1.0}])
    return seen


def _profile_calls(seen):
    return [c for c in seen if c["system"] != locator.system_prompt()]


def test_an_overrun_item_is_cut_where_the_writer_says(rawstore_tmp_db, world):
    engine.build_profiles(["NVDA"], usd_cap=1.0)
    loc = [c for c in world if c["system"] == locator.system_prompt()]
    assert len(loc) == 1 and loc[0]["json_mode"] is True and "We design gpus" not in loc[0]["user"]
    (call,) = _profile_calls(world)
    assert "Competition" in call["user"] and "Risks are many" not in call["user"]
    row = rawstore.get("relatedness_profiles", "NVDA")
    assert row["label_source"]["input"] == "llm_locator"


def test_an_ok_item_never_asks_the_locator(rawstore_tmp_db, world):
    rawstore.execute("UPDATE chain_filings SET quality = 'ok' WHERE ticker = 'NVDA'", db="edgar")
    engine.build_profiles(["NVDA"], usd_cap=1.0)
    assert all(c["system"] != locator.system_prompt() for c in world)
    assert rawstore.get("relatedness_profiles", "NVDA")["label_source"]["input"] == "item"


def test_a_located_profile_is_not_redone_on_the_next_run(rawstore_tmp_db, world):
    engine.build_profiles(["NVDA"], usd_cap=1.0)
    n = len(world)
    engine.build_profiles(["NVDA"], usd_cap=1.0)
    assert len(world) == n


def test_a_failed_locate_keeps_the_rule_cut(rawstore_tmp_db, world, monkeypatch):
    real = profiles.deepseek.complete

    def bad(system, user, **kw):
        if system == locator.system_prompt():
            world.append({"system": system, "user": user, **kw})
            return "no idea"
        return real(system, user, **kw)

    monkeypatch.setattr(profiles.deepseek, "complete", bad)
    engine.build_profiles(["NVDA"], usd_cap=1.0)
    assert rawstore.get("relatedness_profiles", "NVDA")["label_source"]["input"] == "item"
    (call,) = _profile_calls(world)
    assert "Risks are many" in call["user"]                 # the overrun text, as before


def test_the_run_names_the_located_companies(rawstore_tmp_db, world, monkeypatch):
    monkeypatch.setattr(engine.vc_engine, "fetch_corpus", lambda *a, **k: None)
    monkeypatch.setattr(engine, "_record_history", lambda *a, **k: None)
    payload = engine.run(tickers=["NVDA"], usd_cap=1.0)
    assert payload["llm_located"] == ["NVDA"]
    assert store.load_profiles(["NVDA"])["NVDA"]["products"] == ["gpu"]


def test_a_suspect_ok_cut_asks_the_locator_too():
    # #400 WP7: 24 of the 10-K reading report's 29 wrong cuts were labelled ok, so ASK_ON alone never asked.
    from RevenueChaser.module_relatedness import locator
    assert locator.asks("ok;suspect:S4") and locator.asks("overrun") and locator.asks("short;x")
    assert not locator.asks("ok") and not locator.asks(None)


def test_a_capped_cut_asks_the_locator():
    # #260: a slice cut at MAX_ITEM_CHARS is labelled capped, where it used to be ok;suspect:S8 (which asked).
    from RevenueChaser.module_relatedness import locator
    assert locator.asks("capped") and locator.asks("capped;x")
