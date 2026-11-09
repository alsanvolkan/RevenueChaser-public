"""``relatedness_scores`` in ``boards/relatedness.duckdb`` is written incrementally (plan 2026-11-08 §10): the run's
pairs are upserted, only the pairs the run retired are deleted, and an empty run deletes nothing."""
from __future__ import annotations

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import store


def _pairs():
    assert rawstore._db_of("relatedness_scores") == "relatedness"   # read from the board file, not raw.duckdb
    return {(r["ticker"], r["peer"]): r["score"] for r in
            rawstore.query("SELECT ticker, peer, score FROM relatedness_scores", db="relatedness")}


def test_the_scores_live_in_the_relatedness_board_file(rawstore_tmp_db):
    store.save_scores({"AAA": [{"peer": "BBB", "score": 0.5}]})
    assert rawstore._db_of("relatedness_scores") == "relatedness"
    assert rawstore.board_db_path("relatedness").exists()
    assert _pairs() == {("AAA", "BBB"): 0.5}


def test_a_run_upserts_its_pairs_and_deletes_only_the_retired_ones(rawstore_tmp_db):
    store.save_scores({"AAA": [{"peer": "BBB", "score": 0.5}, {"peer": "CCC", "score": 0.4}],
                       "DDD": [{"peer": "EEE", "score": 0.3}]})
    store.save_scores({"AAA": [{"peer": "BBB", "score": 0.6}], "DDD": [{"peer": "EEE", "score": 0.3}]})
    assert _pairs() == {("AAA", "BBB"): 0.6, ("DDD", "EEE"): 0.3}


def test_an_empty_run_deletes_nothing(rawstore_tmp_db):
    store.save_scores({"AAA": [{"peer": "BBB", "score": 0.5}]})
    assert store.save_scores({}) == 0
    assert _pairs() == {("AAA", "BBB"): 0.5}
