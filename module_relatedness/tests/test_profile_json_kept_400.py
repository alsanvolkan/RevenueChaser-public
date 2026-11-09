"""#400 prerequisite: a profile re-save never drops ``profile_json`` / ``label_source`` (schema 13, #401).

``relatedness_profiles`` is an ``upsert`` table and ``rawstore._apply_write`` sets every non-key column from the batch,
so a row written without a column sets that column to NULL. The engine re-saves a kept profile (an unusable answer
counts an attempt but keeps the stored content) and writes ``no_text`` / ``short_text`` rows; before this fix both
went through ``store.save_profile``, which sent neither JSON column, so a re-save wiped the v3 profile.
"""
from __future__ import annotations

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import config, engine, store

V3 = {"summary": "makes gpus", "segments": [{"name": "compute", "share": 0.9, "source": "table",
                                             "products": ["gpu"], "end_markets": ["data center"]}],
      "customers": [], "suppliers": [], "named_competitors": ["amd"], "business_model": "hardware",
      "channel": "direct", "geography": "global", "stage": "mature", "keywords": ["ai"]}
LABELS = {"summary": "chunk:0", "segments": "table"}
ATTRS = {f: ["x"] for f in config.PROFILE_FIELDS}


def _json_cols(ticker):
    row = rawstore.get("relatedness_profiles", ticker) or {}
    return row.get("profile_json"), row.get("label_source")


def test_save_profile_stores_the_v3_json_and_its_label_source(rawstore_tmp_db):
    store.save_profile("NVDA", "1045810", ATTRS, "ck", "m", profile_json=V3, label_source=LABELS)
    assert _json_cols("NVDA") == (V3, LABELS)


def test_a_save_without_the_json_keeps_the_stored_json(rawstore_tmp_db):
    """The attempt-count re-save and any other caller that does not name the columns carry the stored values."""
    store.save_profile("NVDA", "1045810", ATTRS, "ck", "m", profile_json=V3, label_source=LABELS)
    store.save_profile("NVDA", "1045810", ATTRS, "ck", "m", status="ok", attempts=2)
    assert _json_cols("NVDA") == (V3, LABELS)
    assert rawstore.get("relatedness_profiles", "NVDA")["attempts"] == 2


def test_a_row_written_by_another_path_keeps_its_json_on_a_plain_resave(rawstore_tmp_db):
    """The defect as found: a row carrying profile_json, then the old-style call with no JSON argument."""
    store.save_profile("NVDA", "1045810", ATTRS, "ck", "m")
    rawstore.execute("UPDATE relatedness_profiles SET profile_json = ?, label_source = ? WHERE ticker = 'NVDA'",
                     ['{"summary": "s"}', '{"summary": "chunk:0"}'], db="relatedness")
    store.save_profile("NVDA", "1045810", ATTRS, "ck2", "m", attempts=1)
    assert _json_cols("NVDA") == ({"summary": "s"}, {"summary": "chunk:0"})


def test_an_explicit_none_clears_the_json(rawstore_tmp_db):
    """A new profile that has no v3 JSON (a v2 writer) says so; it is not left beside a JSON of another profile."""
    store.save_profile("NVDA", "1045810", ATTRS, "ck", "m", profile_json=V3, label_source=LABELS)
    store.save_profile("NVDA", "1045810", ATTRS, "ck2", "m", profile_json=None, label_source=None)
    assert _json_cols("NVDA") == (None, None)


def test_an_unusable_answer_keeps_the_stored_v3_json(rawstore_tmp_db, monkeypatch):
    """Engine path: the second run's reply is unusable, the kept profile is re-saved with its attempt counted."""
    from RevenueChaser.module_relatedness import profiles
    from RevenueChaser.module_relatedness.tests.test_relatedness import long_body

    monkeypatch.setattr(engine, "_cik_for", lambda ts: {t: t for t in ts})
    monkeypatch.setattr(engine, "_filing_text", lambda t: (t, long_body("we make gpus")))
    monkeypatch.setattr(engine, "_filing_source", lambda t: None)
    store.save_profile("NVDA", "NVDA", ATTRS, "stale-key", "m", profile_json=V3, label_source=LABELS)

    def empty(*a, **k):
        return {f: [] for f in config.PROFILE_FIELDS}

    monkeypatch.setattr(profiles, "profile_for", empty)
    engine.build_profiles(["NVDA"], provider="deepseek", usd_cap=1.0)
    row = rawstore.get("relatedness_profiles", "NVDA")
    assert row["attempts"] == 1
    assert _json_cols("NVDA") == (V3, LABELS)
