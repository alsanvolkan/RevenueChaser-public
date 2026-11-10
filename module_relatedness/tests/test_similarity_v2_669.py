"""#669: Relatedness similarity v2 on local embeddings (plan 2026-10-30 Step 2).

The embedder is faked at its boundary (``embeddings._embed``): no model download, no network."""
from __future__ import annotations

import numpy as np
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import config, embeddings, engine, similarity, store
from RevenueChaser.module_relatedness.tests.test_relatedness import _prof, _seed_filings, _seed_universe


def _fake_embed(calls):
    def embed(texts):
        calls.append(list(texts))
        out = []
        for t in texts:
            t = t.lower()
            out.append([1.0, 0.0, 0.0] if "gpu" in t else [0.0, 1.0, 0.0] if "bread" in t else [0.0, 0.0, 1.0])
        return np.asarray(out, dtype="float32")
    return embed


def _pj(summary, segments=(), customers=(), business_model="product", stage="operating"):
    return {"summary": summary, "segments": list(segments), "customers": list(customers), "suppliers": [],
            "business_model": business_model, "channel": "direct", "stage": stage}


def test_segment_lines_carry_their_share_and_the_summary_weighs_one():
    ps = embeddings.pieces(_pj("Chips.", [{"name": "Compute", "share": 0.7, "products": ["gpu"]},
                                          {"name": "Gaming", "share": 0.3, "end_markets": ["pc"]}]))
    assert ps == [("Chips.", 1.0), ("Compute: gpu", 0.7), ("Gaming: pc", 0.3)]


def test_segments_without_a_share_split_one_weight_equally():
    ps = embeddings.pieces(_pj("", [{"name": "A"}, {"name": "B", "share": 0.5}]))
    assert ps == [("A", 0.5), ("B", 0.5)]
    assert embeddings.pieces({}) == []


def test_a_vector_is_cached_and_made_again_only_when_its_text_changes(rawstore_tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(embeddings, "_embed", _fake_embed(calls))
    pjs = {"AAA": _pj("We sell gpu chips."), "BBB": _pj("We bake bread.")}
    first = embeddings.company_vectors(pjs)
    assert len(calls) == 1 and sorted(first) == ["AAA", "BBB"]
    row = rawstore.get("relatedness_embeddings", ("AAA", config.EMBED_MODEL))
    assert row["dim"] == 3 and len(bytes(row["vector"])) == embeddings.HASH_BYTES + 3 * 4

    again = embeddings.company_vectors(pjs)
    assert len(calls) == 1                                  # nothing changed: no embed call
    assert np.allclose(again["AAA"], first["AAA"])

    pjs["BBB"] = _pj("We make gpu boards now.")
    embeddings.company_vectors(pjs)
    assert calls[-1] == ["We make gpu boards now."]          # only the changed company
    assert rawstore.count("relatedness_embeddings") == 2     # replaced in place, not a second row


def test_a_refresh_embeds_in_memory_and_writes_no_vector(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr(embeddings, "_embed", _fake_embed([]))
    out = embeddings.company_vectors({"AAA": _pj("gpu")}, write=False)
    assert "AAA" in out
    assert rawstore.count("relatedness_embeddings") == 0


def test_party_names_match_across_case_punctuation_and_suffixes():
    assert similarity.party_name("Apple Inc.") == similarity.party_name("APPLE") == "apple"
    assert similarity.party_name("The Home Depot, Inc.") == "home depot"


def test_the_score_is_the_weighted_sum_of_the_three_signals():
    profiles = {t: _prof(products=["x"]) for t in ("A", "B", "C")}
    pjs = {"A": _pj("", customers=[{"name": "Apple Inc", "type": "oem"}]),
           "B": _pj("", customers=[{"name": "APPLE", "type": "oem"}]),
           "C": _pj("", business_model="subscription", stage="clinical")}
    v = {"A": np.asarray([1, 0], "float32"), "B": np.asarray([1, 0], "float32"), "C": np.asarray([0, 1], "float32")}
    sectors = {"A": "Technology", "B": "Technology", "C": "Healthcare"}
    peers, scores = similarity.blended_peer_lists(profiles, pjs, v, sectors, top=5)
    ab = config.W_DO * 1 + config.W_CHAIN * (config.W_CHAIN_TYPES * 1 + config.W_CHAIN_NAMES * 1) + config.W_STRUCT
    ac = config.W_STRUCT * 1 / 4                             # only the channel agrees
    got = {p["peer"]: p["score_raw"] for p in peers["A"]}
    assert got["B"] == pytest.approx(ab, abs=1e-5)
    assert got["C"] == pytest.approx(ac, abs=1e-5)
    assert [p["peer"] for p in peers["A"]] == ["B", "C"]
    assert len(scores) == 3                                  # the upper triangle of 3 tickers


def test_scoring_in_chunks_gives_the_same_peers_as_one_chunk():
    rng = np.random.default_rng(7)
    tickers = [f"T{i:02d}" for i in range(30)]
    profiles = {t: _prof(products=["x"]) for t in tickers}
    raw = {t: rng.random(4).astype("float32") for t in tickers}
    v = {t: a / np.linalg.norm(a) for t, a in raw.items()}
    one, s1 = similarity.blended_peer_lists(profiles, {}, v, None, top=5, chunk=1000)
    many, s2 = similarity.blended_peer_lists(profiles, {}, v, None, top=5, chunk=7)
    def order(peers):
        return {t: [p["peer"] for p in ps] for t, ps in peers.items()}
    assert order(one) == order(many)
    assert np.allclose(np.sort(s1), np.sort(s2))


TICKERS = ["GPU1", "GPU2", "BRD1"]


@pytest.fixture
def seeded(rawstore_tmp_db):
    _seed_universe(TICKERS)
    _seed_filings(TICKERS)
    summaries = {"GPU1": "We design gpu accelerators.", "GPU2": "Our gpu cards power games.",
                 "BRD1": "We bake bread for grocers."}
    for t in TICKERS:
        # The same tags for all three: the tag cosine cannot tell them apart, the embedding can.
        store.save_profile(t, "cik-" + t, _prof(products=["chips", "boards"], end_markets=["retail", "oem"]),
                           "k", "m", profile_json=_pj(summaries[t]))
    return TICKERS


def test_the_board_ranks_peers_on_the_embedding_and_stores_the_vectors(seeded, monkeypatch):
    monkeypatch.setattr(embeddings, "_embed", _fake_embed([]))
    payload = engine._payload(store.load_profiles(seeded), write=True)
    assert payload["similarity"] == "embedding"
    assert payload["peers"]["GPU1"][0]["peer"] == "GPU2"
    assert payload["peers"]["GPU1"][0]["shared"]            # the shared tags stay as evidence
    assert rawstore.count("relatedness_embeddings") == 3


def test_a_failing_embedder_falls_back_to_the_tag_cosine(seeded, monkeypatch):
    def boom(texts):
        raise RuntimeError("no model file")
    monkeypatch.setattr(embeddings, "_embed", boom)
    payload = engine._payload(store.load_profiles(seeded), write=True)
    assert payload["similarity"] == "tags"
    assert payload["peers"]["GPU1"]                          # the board still has peers
    assert rawstore.count("relatedness_embeddings") == 0


def test_profiles_without_a_v3_profile_keep_the_tag_cosine(rawstore_tmp_db, monkeypatch):
    _seed_universe(["AAA", "BBB"])
    for t in ("AAA", "BBB"):
        store.save_profile(t, "cik", _prof(products=["gpu", "cpu"], end_markets=["dc"]), "k", "m")

    def never(texts):
        raise AssertionError("no v3 text, nothing to embed")
    monkeypatch.setattr(embeddings, "_embed", never)
    payload = engine._payload(store.load_profiles(["AAA", "BBB"]), write=True)
    assert payload["similarity"] == "tags"
