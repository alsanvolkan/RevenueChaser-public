"""F1 (Fable report review, 5 Nov 2026): the Brier records are scored.

The 30 Oct report (p.19-20) resolves a week's scenario set on the NEXT Friday's official Cboe close, and states a
discrimination threshold: twice the standard error of the mean Brier. Before this change ``score_date`` had no
caller outside the tests, so the card showed 0 scored days forever.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_spx_gamma import brier
from RevenueChaser.module_spx_gamma.scenarios import realized_class

BANDS = {"consolidation": (7700.0, 7750.0), "upside": (7750.0, 7800.0), "downside": (7600.0, 7700.0)}
P = {"Consolidation": 0.35, "Upside": 0.30, "Downside": 0.35}


@pytest.mark.parametrize("asof, target", [
    ("2026-10-23", "2026-10-30"),   # a Friday resolves on the next Friday (the 30 Oct report's own example)
    ("2026-10-26", "2026-10-30"),   # a Monday resolves on that week's Friday
    ("2026-10-29", "2026-10-30"),   # Thursday -> Friday
    ("2026-10-31", "2026-11-06"),   # Saturday -> next Friday
])
def test_target_friday(asof, target):
    assert brier.target_friday(asof) == target


def test_record_keeps_bands_and_target(tmp_path):
    data = brier.record_probs("2026-10-23", P, bands=BANDS)
    rec = data["records"][0]
    assert rec["target"] == "2026-10-30"
    assert rec["bands"]["consolidation"] == [7700.0, 7750.0]


def test_score_pending_resolves_on_target_close(tmp_path):
    brier.record_probs("2026-10-23", P, bands=BANDS)
    closes = {"2026-10-26": 7760.0, "2026-10-30": 7650.0}
    out = brier.score_pending(closes)
    assert out["scored"] == ["2026-10-23"]
    rec = brier.load_scores()["records"][0]
    assert rec["outcome"] == "Downside" == realized_class(BANDS, 7650.0)
    assert rec["resolved_close"] == 7650.0 and rec["resolved_on"] == "2026-10-30"
    assert brier.load_scores()["n"] == 1


def test_score_pending_waits_for_the_target(tmp_path):
    brier.record_probs("2026-10-26", P, bands=BANDS)
    out = brier.score_pending({"2026-10-27": 7720.0, "2026-10-29": 7730.0})
    assert out["scored"] == []
    assert brier.load_scores()["records"][0]["outcome"] is None


def test_holiday_friday_uses_last_close_of_the_week(tmp_path):
    brier.record_probs("2026-11-23", P, bands=BANDS)    # target Friday 2026-11-27
    # the Friday close exists in this example; a missing Friday with a later close resolves on Thursday
    out = brier.score_pending({"2026-11-26": 7720.0, "2026-11-30": 7900.0})
    assert out["scored"] == ["2026-11-23"]
    rec = brier.load_scores()["records"][0]
    assert rec["resolved_on"] == "2026-11-26" and rec["outcome"] == "Consolidation"


def test_record_without_bands_is_not_scored(tmp_path):
    brier.record_probs("2026-10-23", P)
    assert brier.score_pending({"2026-10-30": 7650.0})["scored"] == []


def test_scored_record_is_not_rescored(tmp_path):
    brier.record_probs("2026-10-23", P, bands=BANDS)
    brier.score_pending({"2026-10-30": 7650.0})
    again = brier.score_pending({"2026-10-30": 7720.0})
    assert again["scored"] == []
    assert brier.load_scores()["records"][0]["outcome"] == "Downside"


def test_scorecard_two_se_threshold(tmp_path):
    for i, (asof, close) in enumerate([("2026-10-02", 7650.0), ("2026-10-09", 7720.0), ("2026-10-16", 7790.0)]):
        brier.record_probs(asof, P, bands=BANDS)
    brier.score_pending({"2026-10-09": 7650.0, "2026-10-16": 7720.0, "2026-10-23": 7790.0})
    card = brier.scorecard()
    scores = [r["brier"] for r in card["records"]]
    n = len(scores)
    mean = sum(scores) / n
    sd = (sum((s - mean) ** 2 for s in scores) / (n - 1)) ** 0.5
    assert card["scored_count"] == 3
    assert card["discrimination_threshold"] == pytest.approx(2 * sd / n ** 0.5)
    assert card["difference_vs_equal_weight"] == pytest.approx(mean - brier.EQUAL_WEIGHT_REFERENCE)
    assert card["discriminates"] is (abs(mean - brier.EQUAL_WEIGHT_REFERENCE) > 2 * sd / n ** 0.5)


def test_run_scores_pending_records_from_stored_closes(tmp_path, monkeypatch):
    """The Fetch run scores what it can from the stored SPX closes before it records today's set."""
    from RevenueChaser.module_spx_gamma import run, store_reads

    brier.record_probs("2026-10-23", P, bands=BANDS)
    rows = [{"date": "2026-10-29", "close": 7740.0}, {"date": "2026-10-30", "close": 7810.0}]
    monkeypatch.setattr(store_reads, "spx_candles", lambda day: {"stored": True, "rows": rows})
    assert run._score_brier("2026-11-04") == ["2026-10-23"]
    assert brier.load_scores()["records"][0]["outcome"] == "Upside"


def test_run_scoring_survives_missing_closes(tmp_path, monkeypatch):
    from RevenueChaser.module_spx_gamma import run, store_reads

    brier.record_probs("2026-10-23", P, bands=BANDS)
    monkeypatch.setattr(store_reads, "spx_candles", lambda day: {"stored": False, "rows": []})
    assert run._score_brier("2026-11-04") == []
