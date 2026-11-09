"""``rawstore.sync_rows``: the incremental write of a board table (plan 2026-11-08 §10)."""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore


def _tickers():
    return {r["ticker"]: r["multiplier"] for r in
            rawstore.query("SELECT ticker, multiplier FROM swing_scores", db="swing_potential")}


def test_upserts_and_retires_only_the_dropped_keys(rawstore_tmp_db):
    rawstore.sync_rows("swing_scores", [{"ticker": "A", "multiplier": 1}, {"ticker": "B", "multiplier": 2}])
    assert rawstore.sync_rows("swing_scores", [{"ticker": "B", "multiplier": 3}, {"ticker": "C", "multiplier": 4}]) == 1
    assert _tickers() == {"B": 3, "C": 4}


def test_an_empty_run_raises_or_with_allow_empty_keeps_everything(rawstore_tmp_db):
    rawstore.sync_rows("swing_scores", [{"ticker": "A", "multiplier": 1}])
    with pytest.raises(rawstore.RawStoreError):
        rawstore.sync_rows("swing_scores", [])
    assert rawstore.sync_rows("swing_scores", [], allow_empty=True) == 0
    assert _tickers() == {"A": 1}


def test_a_history_table_is_refused(rawstore_tmp_db):
    with pytest.raises(rawstore.RawStoreError):
        rawstore.sync_rows("forecast_ledger", [{"source": "x"}])
