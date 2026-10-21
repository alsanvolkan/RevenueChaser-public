"""#286: ``forecast_ledger`` carries MANIFEST ``"history": True``, so rawstore refuses a rewrite.

The ledger is the frozen record of what each board forecast on a day (#128, #62 D-3) and holds
no history of its own to rebuild from: a rewrite would lose the past. Before this flag the
append-only guarantee rested on ``core/ledger.py``'s discipline alone; now the store enforces it
(``update_columns`` / ``replace_all`` / ``delete`` / ``clear`` raise, and a duplicate key keeps the
first row).
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore

TABLE = "forecast_ledger"


def _forecast(**kw):
    row = {"source": "spx_gamma", "forecast_date": "2026-10-01", "subject": "SPX",
           "target_type": "pin_prob", "target_date": "2026-10-02", "frozen_at": 1.0,
           "run_id": None, "model_version": "v1", "rank": None, "score": 0.42,
           "bucket": None, "probs": {"5700": 0.3, "5750": 0.5}, "market_cap": None,
           "sector": None, "shown_price": 5730.5, "stop": None, "target": None}
    row.update(kw)
    return row


KEY = ("spx_gamma", "2026-10-01", "SPX", "pin_prob", "2026-10-02")


def test_the_manifest_flags_the_ledger_as_history():
    assert rawstore.MANIFEST[TABLE].get("history") is True


@pytest.fixture
def ledger(rawstore_tmp_db):
    """One frozen row already in the ledger."""
    rawstore.write_rows(TABLE, [_forecast()])
    return TABLE


def _row():
    return rawstore.get(TABLE, KEY)


def test_update_columns_raises(ledger):
    with pytest.raises(rawstore.RawStoreError, match="history"):
        rawstore.update_columns(TABLE, "subject", [{"subject": "SPX", "score": 99.0}], ["score"])
    assert _row()["score"] == 0.42


def test_replace_all_raises(ledger):
    with pytest.raises(rawstore.RawStoreError, match="history"):
        rawstore.replace_all(TABLE, [_forecast(score=9.0)])
    assert _row()["score"] == 0.42


def test_delete_raises(ledger):
    with pytest.raises(rawstore.RawStoreError, match="history"):
        rawstore.delete(TABLE, KEY)
    assert _row() is not None


def test_clear_raises(ledger):
    with pytest.raises(rawstore.RawStoreError, match="history"):
        rawstore.clear(TABLE)
    assert rawstore.count(TABLE) == 1


def test_a_second_write_with_the_same_key_keeps_the_first_row(ledger):
    rawstore.write_rows(TABLE, [_forecast(model_version="v2", score=0.9)])
    assert rawstore.count(TABLE) == 1
    row = _row()
    assert row["model_version"] == "v1" and row["score"] == 0.42


def test_a_new_key_is_still_written(ledger):
    rawstore.write_rows(TABLE, [_forecast(target_date="2026-10-03")])
    assert rawstore.count(TABLE) == 2
