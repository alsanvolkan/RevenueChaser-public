"""E6 (#372): R1 (drivers) is a LOGGED rule — weight 0, never a direction vote.

It reads the stored ``estimate_snapshots`` ``0q`` row (D14) and turns the 30-day EPS trend and the revision counts
into a −100..+100 read. Until it is wired into the composite (a later decision) the board only surfaces it in the
drawer, so the card's direction, confidence and action must be byte-identical with R1 available and unavailable.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar, rawstore
from RevenueChaser.module_earnings_radar import stored_inputs
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline, nrc_from_card
from RevenueChaser.module_earnings_radar.rules import rule1_drivers
from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW

SESSION = date(2026, 10, 14)
NOW = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc)

FULL = {"eps_trend_current": 1.05, "eps_trend_7d": 1.02, "eps_trend_30d": 1.00, "eps_trend_60d": 0.95,
        "eps_trend_90d": 0.90, "eps_rev_up_7d": 2, "eps_rev_up_30d": 5, "eps_rev_down_7d": 1,
        "eps_rev_down_30d": 1}


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


@pytest.fixture(autouse=True)
def _store(rawstore_tmp_db):
    return rawstore_tmp_db


# ── the rule ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_full_row_scores_the_hand_computed_value():
    r = rule1_drivers.score(FULL)
    # trend_30 = (1.05 - 1.00) / 1.00 = +5% -> 60 * 1 = 60; net 5-1 = 4 of 6 -> 40 * 2/3 = 26.7
    assert (r.score, r.available) == (86.7, True)
    assert r.coverage == 1.0
    assert r.reasons == ["EPS estimate +5.0% over 30 days", "30-day revisions 5 up / 1 down"]
    assert r.flags == []
    assert r.raw_metrics["trend_30"] == pytest.approx(0.05)
    assert r.raw_metrics["eps_trend_current"] == 1.05
    assert sorted(r.raw_metrics) == sorted([*rule1_drivers.FIELDS, *rule1_drivers.TREND_KEYS])


def test_a_falling_estimate_with_cuts_is_the_full_negative():
    r = rule1_drivers.score(dict(FULL, eps_trend_current=0.95, eps_rev_up_30d=0, eps_rev_down_30d=6))
    assert r.score == -100.0                       # -5% trend -> -60, 0 up / 6 down -> -40
    assert r.available is True


def test_the_trend_is_clamped_so_one_extreme_estimate_cannot_run_away():
    r = rule1_drivers.score(dict(FULL, eps_trend_current=1.50))
    assert r.raw_metrics["trend_30"] == pytest.approx(0.5)
    assert r.score == 60.0 + 26.7                  # the trend term saturates at +1


def test_the_denominator_floor_keeps_a_near_zero_estimate_from_exploding():
    """``max(abs(t30), 0.10)``: a 0.004 print against a 0.00 one is a +4% trend, not a division by zero."""
    r = rule1_drivers.score(dict(FULL, eps_trend_current=0.004, eps_trend_30d=0.0,
                                 eps_rev_up_30d=0, eps_rev_down_30d=0))
    assert r.raw_metrics["trend_30"] == pytest.approx(0.04)
    assert r.score == 48.0                         # 60 * (0.04 / 0.05)


def test_no_row_is_unavailable_and_never_zero():
    for row in (None, {}):
        r = rule1_drivers.score(row)
        assert (r.score, r.available, r.coverage) == (None, False, 0)
        assert r.reasons == ["no estimate snapshot"]
        assert r.raw_metrics == {}


@pytest.mark.parametrize("field", rule1_drivers.REQUIRED)
def test_an_incomplete_required_field_is_unavailable(field):
    r = rule1_drivers.score({k: v for k, v in FULL.items() if k != field})
    assert (r.score, r.available, r.coverage) == (None, False, 0)
    assert r.reasons == ["estimate trend incomplete"]


def test_nan_counts_as_missing():
    r = rule1_drivers.score(dict(FULL, eps_trend_30d=float("nan")))
    assert r.available is False and r.score is None
    assert r.reasons == ["estimate trend incomplete"]


def test_coverage_is_the_share_of_the_nine_fields_present():
    row = {k: v for k, v in FULL.items() if k not in ("eps_trend_90d", "eps_rev_down_7d")}
    r = rule1_drivers.score(row)
    assert r.available is True                     # the four required fields are there
    assert r.coverage == pytest.approx(7 / 9)
    assert r.raw_metrics["trend_90"] is None


def test_the_rule_has_no_weight_of_its_own():
    from RevenueChaser.core.config import RULE_WEIGHTS
    assert RULE_WEIGHTS["rule1_drivers"] == 0.0


# ── the stored row ───────────────────────────────────────────────────────────────────────────────────────────────

def _seed(row=FULL, ticker="AAPL", session=SESSION, period="0q"):
    rawstore.write_rows("estimate_snapshots", [{"ticker": ticker, "session_date": session, "period": period,
                                                "source": "yfinance", **row}])


def test_estimates_reads_the_zero_quarter_row_of_the_newest_session():
    _seed()
    rawstore.write_rows("estimate_snapshots", [
        {"ticker": "AAPL", "session_date": date(2026, 10, 13), "period": "0q", "eps_trend_current": 9.9},
        {"ticker": "AAPL", "session_date": SESSION, "period": "+1q", "eps_trend_current": 8.8}])
    rows, session = stored_inputs.estimates(["AAPL"])
    assert session == SESSION
    assert rows["AAPL"]["eps_trend_current"] == pytest.approx(1.05)


def test_estimates_ignores_a_session_older_than_the_lag():
    _seed(session=date(2026, 10, 1))               # 9 sessions back, MAX_LAG is 5
    assert stored_inputs.estimates(["AAPL"]) == ({}, None)


def test_estimates_of_an_empty_store_is_empty():
    assert stored_inputs.estimates(["AAPL"]) == ({}, None)


# ── the pipeline ─────────────────────────────────────────────────────────────────────────────────────────────────

def _pipeline():
    fake = _FakeUW()
    p = EarningsPipeline(["AAPL"], uw=fake, cal_meta={"AAPL": {
        "earnings_date": "2026-11-05", "full_name": "Aple", "sector": "Technology",
        "expected_move_perc": 0.05}})
    p._screener_cache = {"AAPL": {"close": "100", "call_volume": "0", "put_volume": "0",
                                  "total_open_interest": "0"}}
    p._dgs10_cache = {}
    return p


def test_r1_is_logged_only_so_the_card_does_not_move():
    without = _pipeline()
    without._warm_estimates()                      # the store is still empty: this is the R1-off card
    card_b = without._score("AAPL")

    _seed()
    with_r1 = _pipeline()
    with_r1._warm_estimates()
    card_a = with_r1._score("AAPL")

    assert (card_a.direction_score, card_a.confidence_score, card_a.final_action) == \
           (card_b.direction_score, card_b.confidence_score, card_b.final_action)
    # The thesis is cut at 200 characters and NRC reads "beat"/"miss" from it: R1's text must not enter it
    # (review S5), or a weight-0 rule could still move NRC.
    assert card_a.key_thesis == card_b.key_thesis
    assert "estimate" not in card_a.key_thesis.lower()
    assert nrc_from_card(card_a) == nrc_from_card(card_b)
    a = card_a.rule_audit["rule1_drivers"]
    assert (a["available"], a["label"], a["weight"], a["contribution"]) == (True, "Drivers", 0.0, 0.0)
    assert a["score"] == 86.7
    b = card_b.rule_audit["rule1_drivers"]
    assert b["available"] is False and b["score"] is None
    assert with_r1.input_sources["estimates"] == {"stored": 1, "missing": 0}
    assert without.input_sources["estimates"] == {"stored": 0, "missing": 1}


def test_an_incomplete_estimate_row_stays_out_of_the_thesis():
    """'estimate trend incomplete' passes the thesis filter's "no "/"neutral" words, so R1 is skipped by name."""
    _seed({**FULL, "eps_trend_30d": None})
    p = _pipeline()
    p._warm_estimates()
    card = p._score("AAPL")
    assert card.rule_audit["rule1_drivers"]["available"] is False
    assert "estimate" not in (card.key_thesis or "").lower()


def test_the_drawer_names_r1_as_logged_with_no_weight(tmp_path):
    """The board's decision drawer lists R1 with its role, so a reader sees it carries 0% (spec E6)."""
    from RevenueChaser.module_earnings_radar.report import export_html
    _seed()
    p = _pipeline()
    p._warm_estimates()
    card = p._score("AAPL")
    html = export_html([card], [], {}, filename=str(tmp_path / "e.html")).read_text(encoding="utf-8")
    assert "logged (0%)" in html
    assert '"role": "reserved"' not in html
