"""Relatedness durable tables (Phase 6 schema) — populate + round-trip."""
from __future__ import annotations

from RevenueChaser.core import rawstore


def test_llm_cache_and_profiles(rawstore_tmp_db):
    key = "flash|think0|low|v1|AAPL|abc"
    rawstore.write_rows("llm_cache", [{
        "cache_key": key, "ticker": "AAPL", "cik": "320193",
        "model": "deepseek-flash", "thinking": 0, "effort": "low",
        "prompt_version": "v1", "input_hash": "abc", "response": {"segments": []},
        "fetched_at": 1.0, "status": "ok"}])
    assert rawstore.get("llm_cache", key)["response"] == {"segments": []}

    rawstore.write_rows("relatedness_profiles", [{
        "ticker": "AAPL", "cik": "320193", "model": "deepseek-flash",
        "prompt_version": "v1", "fetched_at": 1.0, "cache_key": key,
        "status": "ok"}])
    assert rawstore.get("relatedness_profiles", "AAPL")["cik"] == "320193"


def test_relatedness_attributes_and_scores(rawstore_tmp_db):
    rawstore.replace_all("relatedness_attributes", [
        {"ticker": "AAPL", "field": "primary_product", "value": "iPhone"},
        {"ticker": "AAPL", "field": "customer_count", "value": "1.5e9",
         "value_num": 1.5e9}])
    assert rawstore.query(
        "SELECT count(*) n FROM relatedness_attributes")[0]["n"] == 2

    rawstore.replace_all("relatedness_scores", [
        {"ticker": "AAPL", "peer": "MSFT", "score": 0.42}])
    row = rawstore.query(
        "SELECT score FROM relatedness_scores WHERE ticker='AAPL' AND peer='MSFT'")[0]
    assert row["score"] == 0.42
