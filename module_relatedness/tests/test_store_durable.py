"""Durable Relatedness store — cache key, profile round-trip, scores."""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import store


def test_prompt_hash_and_cache_key_are_stable():
    h1 = store.prompt_hash("sys", "user")
    assert h1 == store.prompt_hash("sys", "user") and len(h1) == 64
    k1 = store.cache_key("m", True, "high", "v1", "aapl", h1)
    k2 = store.cache_key("m", True, "high", "v1", "AAPL", h1)
    assert k1 == k2  # ticker normalised, keys sorted


def test_profile_round_trip(rawstore_tmp_db):
    prof = {"products": ["gpu"], "end_markets": ["data center"],
            "business_model": [], "customer_types": [], "value_chain_role": [],
            "keywords": ["ai"]}
    store.save_profile("NVDA", "1045810", prof, "ck", "deepseek-flash")
    got = store.load_profiles(["NVDA"])
    assert got["NVDA"]["products"] == ["gpu"]
    assert got["NVDA"]["keywords"] == ["ai"]
    assert rawstore.get("relatedness_profiles", "NVDA")["cik"] == "1045810"

    # re-saving replaces attributes (no duplicate fields)
    store.save_profile("NVDA", "1045810", {**prof, "products": ["cpu"]}, "ck2", "m")
    assert store.load_profiles(["NVDA"])["NVDA"]["products"] == ["cpu"]


def test_mode_stamp(rawstore_tmp_db):
    assert store.get_mode() is None
    store.set_mode({"model": "m", "thinking": True, "effort": "high",
                    "prompt_version": "v1"})
    assert store.get_mode()["prompt_version"] == "v1"


def test_save_scores(rawstore_tmp_db):
    n = store.save_scores({"NVDA": [{"peer": "AMD", "score": 0.5},
                                    {"peer": "TSM", "score": 0.1}]})
    assert n == 2
    rows = rawstore.query("SELECT peer, score FROM relatedness_scores "
                          "WHERE ticker='NVDA' ORDER BY score DESC")
    assert [r["peer"] for r in rows] == ["AMD", "TSM"]


def _seed_scores():
    store.save_scores({"NVDA": [{"peer": "AMD", "score": 0.5}]})


def _stored_peers():
    return [r["peer"] for r in rawstore.query("SELECT peer FROM relatedness_scores ORDER BY peer")]


@pytest.mark.parametrize("peers", [{}, {"NVDA": [], "AMD": []}], ids=["no tickers", "no peers"])
def test_save_scores_empty_batch_keeps_the_last_good_scores_and_says_why(
        rawstore_tmp_db, caplog, peers):
    """A run that scored nothing (cold cache, peak-hours stop) must not wipe the last Fetch (#284)."""
    _seed_scores()
    with caplog.at_level("WARNING"):
        assert store.save_scores(peers) == 0
    assert _stored_peers() == ["AMD"]
    assert any("relatedness_scores" in r.getMessage() and "kept" in r.getMessage()
               for r in caplog.records)


def test_save_scores_empty_batch_on_a_cold_store_is_quiet_noop(rawstore_tmp_db):
    assert store.save_scores({}) == 0
    assert _stored_peers() == []


def test_save_scores_non_empty_batch_still_replaces_the_old_scores(rawstore_tmp_db):
    _seed_scores()
    assert store.save_scores({"TSM": [{"peer": "ASML", "score": 0.2}]}) == 1
    assert _stored_peers() == ["ASML"]
