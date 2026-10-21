"""Tests for the Brier scorecard (honest probability audit)."""
from __future__ import annotations

from RevenueChaser.module_spx_gamma import brier
from .conftest import make_snapshot


def test_record_then_score(tmp_path):
    p = tmp_path / "brier.json"
    data = brier.record_probs("2026-08-21", {"Consolidation": 0.5, "Upside": 0.3, "Downside": 0.2}, path=p)
    assert len(data["records"]) == 1
    scored = brier.score_date("2026-08-21", "Upside", path=p)
    rec = scored["records"][0]
    assert rec["outcome"] == "Upside"
    # Brier = (0.5-0)^2 + (0.3-1)^2 + (0.2-0)^2
    assert abs(rec["brier"] - (0.25 + 0.49 + 0.04)) < 1e-9


def test_record_is_idempotent_per_date(tmp_path):
    p = tmp_path / "brier.json"
    brier.record_probs("2026-08-21", {"Consolidation": 0.5, "Upside": 0.3, "Downside": 0.2}, path=p)
    brier.record_probs("2026-08-21", {"Consolidation": 0.4, "Upside": 0.4, "Downside": 0.2}, path=p)
    data = brier.load_scores(p)
    assert len(data["records"]) == 1


def test_scorecard_training_mode(tmp_path):
    p = tmp_path / "brier.json"
    card = brier.scorecard(path=p)
    assert card["scored_count"] == 0
    assert card["calibrated"] is None
    assert "training mode" in card["verdict"]


def test_scorecard_uncalibrated_verdict(tmp_path):
    p = tmp_path / "brier.json"
    brier.record_probs("2026-08-21", {"Consolidation": 1.0, "Upside": 0.0, "Downside": 0.0}, path=p)
    brier.score_date("2026-08-21", "Upside", path=p)  # wrong call -> high Brier
    card = brier.scorecard(path=p)
    assert card["scored_count"] == 1
    assert card["average"] > card["equal_weight_reference"]
    assert card["calibrated"] is False
    assert "NOT yet calibrated" in card["verdict"]


def test_scorecard_calibrated_verdict(tmp_path):
    p = tmp_path / "brier.json"
    brier.record_probs("2026-08-21", {"Consolidation": 1.0, "Upside": 0.0, "Downside": 0.0}, path=p)
    brier.score_date("2026-08-21", "Consolidation", path=p)  # correct -> Brier 0
    card = brier.scorecard(path=p)
    assert card["average"] == 0.0
    assert card["calibrated"] is True
