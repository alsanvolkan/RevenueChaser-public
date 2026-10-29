"""Asymmetric Growth board -> forecast ledger (#128). Offline; the store is a temp DuckDB."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_asymmetric_growth import engine, freeze

D = date(2026, 10, 2)
POST_CLOSE = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)      # 17:00 ET
INTRADAY = datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc)        # 12:00 ET


def _card(ticker, points, **kw):
    c = {"ticker": ticker, "total_points": points, "price": 12.5, "market_cap": 8.0e8,
         "sector": "Information Technology"}
    c.update(kw)
    return c


def _result(cards, *, universe_total=None, excluded=0, errors=0, **kw):
    r = {
        "universe_total": len(cards) + excluded + errors if universe_total is None else universe_total,
        "summary": {"excluded_types": excluded, "score_errors": errors},
        "cards": cards,
    }
    r.update(kw)
    return r


def _spy_bar(minutes=45):
    """SPY's D01 bar for D, written ``minutes`` after the close: the freeze's anchor (#380 S4, decision 4)."""
    from RevenueChaser.core.tests.d01_seed import seed_d01
    seed_d01("SPY", [D], (cal.session_close(D) + timedelta(minutes=minutes)).timestamp())


def _no_bars():
    from RevenueChaser.core import rawstore
    with rawstore.connect(db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw")) as conn:
        conn.execute("DELETE FROM price_bars_daily")


def _stored():
    return rawstore.query("SELECT * FROM forecast_ledger WHERE source = 'asymmetric_growth' ORDER BY rank")


@pytest.fixture
def store(rawstore_tmp_db):
    ledger.take_notes()
    _spy_bar()
    return rawstore_tmp_db


def test_picks_map_the_shown_cards_column_by_column():
    rows = freeze.picks([_card("AAA", 9), _card("BBB", 6, price=None)])
    assert rows[0] == {"subject": "AAA", "rank": 1, "score": 9.0, "bucket": "tier:none|pts_9", "market_cap": 8.0e8,
                       "sector": "Information Technology", "shown_price": 12.5, "stop": None, "target": None}
    assert rows[1]["rank"] == 2 and rows[1]["bucket"] == "tier:none|pts_6_5" and rows[1]["shown_price"] is None


@pytest.mark.parametrize("points, bucket", [(11, "pts_9"), (9, "pts_9"), (8, "pts_8_7"), (7, "pts_8_7"),
                                            (6, "pts_6_5"), (5, "pts_6_5"), (4, "pts_lt5"), (0, "pts_lt5")])
def test_the_bucket_is_the_boards_points_bucket(points, bucket):
    # #380 S2: the tier leads the bucket (a card with no tier here); test_ag_tiers_380 covers tiers 3 and 2.
    assert freeze.picks([_card("AAA", points)])[0]["bucket"] == f"tier:none|{bucket}"


def test_rank_is_the_one_based_position_in_cards():
    assert [r["rank"] for r in freeze.picks([_card("A", 9), _card("B", 8), _card("C", 7)])] == [1, 2, 3]


# Spelled out on purpose: the thresholds the board scores with today. Editing one in engine.py fails
# this test until ``SELECTION_VERSION`` is bumped and this block is updated with it.
GOLDEN_CONFIG = {
    "THRESHOLDS": {
        "rev_growth": 0.2, "accelerating": True, "gross_margin": 0.4, "fcf": True,
        "net_debt_assets": 0.1, "runway": 2.0, "dist_52w": 0.1, "vol_ratio": 1.4,
        "above_sma50": True, "rd_intensity": 0.15, "dilution": 0.2,
    },
    "DEFAULT_MC_RANGE": [50000000, 5000000000],
    "RULES": {"stale_days": 470, "suspicious_growth": 5.0,           # #380 S1 (C18)
              "tier_min_passes": 2,                                   # #380 S2 (tiers)
              "not_comparable_sectors": ["Financial Services", "Real Estate"],   # #380 S6 (C17)
              "momentum_windows": {"high_52w": 252, "sma50": 50, "vol_3m": 63, "vol_10d": 10}},  # #380 S4
}
GOLDEN_VERSION = "7"
GOLDEN_MODEL_VERSION = "7+0dcadf0b"


def test_the_effective_config_is_the_golden_config():
    assert freeze.effective_config() == GOLDEN_CONFIG


def test_the_model_version_is_the_golden_version():
    assert engine.SELECTION_VERSION == GOLDEN_VERSION
    assert freeze.model_version() == GOLDEN_MODEL_VERSION


def test_the_score_error_ceiling_is_one_percent():
    assert engine.FREEZE_MAX_SCORE_ERROR_RATE == 0.01


# ── the whole freeze ─────────────────────────────────────────────────────────

def test_a_post_close_fetch_freezes_every_card(store):
    out = freeze.freeze_result(_result([_card("AAA", 9), _card("BBB", 7), _card("CCC", 3)]),
                               run_id="job1", now=POST_CLOSE)
    assert (out.frozen, out.source, out.label) == (3, "asymmetric_growth", "AG")
    rows = _stored()
    assert [(r["subject"], r["rank"], r["bucket"], r["forecast_date"], r["target_date"]) for r in rows] == [
        ("AAA", 1, "tier:none|pts_9", D, D), ("BBB", 2, "tier:none|pts_8_7", D, D), ("CCC", 3, "tier:none|pts_lt5", D, D)]
    assert {r["run_id"] for r in rows} == {"job1"}
    assert {r["model_version"] for r in rows} == {freeze.model_version()}
    assert ledger.take_notes() == ["Froze 3 AG rows for 2026-10-02."]


def test_a_second_fetch_the_same_day_with_another_set_changes_nothing(store):
    freeze.freeze_result(_result([_card("AAA", 9)]), run_id="one", now=POST_CLOSE)
    before = _stored()
    out = freeze.freeze_result(_result([_card("BBB", 9), _card("CCC", 8)]), run_id="two", now=POST_CLOSE)
    assert out.frozen == 0 and _stored() == before


def test_an_intraday_run_freezes_nothing(store):
    assert freeze.freeze_result(_result([_card("AAA", 9)]), run_id="x", now=INTRADAY).frozen == 0
    assert rawstore.count("forecast_ledger") == 0


def test_a_session_without_a_spy_d01_bar_freezes_nothing(store):
    """#380 S4 (decision 4): the anchor is SPY's own D01 bar for the session, not the universe snapshot."""
    _no_bars()
    out = freeze.freeze_result(_result([_card("AAA", 9)]), run_id="x", now=POST_CLOSE)
    assert out.frozen == 0 and rawstore.count("forecast_ledger") == 0
    (note,) = ledger.take_notes()
    assert "SPY D01 bar" in note and "2026-10-02" in note


def test_a_spy_bar_for_another_session_freezes_nothing(store):
    from RevenueChaser.core.tests.d01_seed import seed_d01
    seed_d01("SPY", [date(2026, 10, 1)], (cal.session_close(D) + timedelta(minutes=45)).timestamp())
    out = freeze.freeze_result(_result([_card("AAA", 9)]), run_id="x", now=POST_CLOSE)
    assert out.frozen == 0 and "2026-10-02" in ledger.take_notes()[0]


def test_a_spy_bar_written_before_the_close_plus_margin_freezes_nothing(store):
    _spy_bar(minutes=29)
    assert freeze.freeze_result(_result([_card("AAA", 9)]), run_id="x", now=POST_CLOSE).frozen == 0
    _spy_bar(minutes=30)
    assert freeze.freeze_result(_result([_card("AAA", 9)]), run_id="x", now=POST_CLOSE).frozen == 1


def test_an_empty_result_freezes_nothing(store):
    out = freeze.freeze_result(_result([], universe_total=4000), run_id="x", now=POST_CLOSE)
    assert out.frozen == 0 and "no candidates" in ledger.take_notes()[0]


def test_the_score_error_rate_ceiling(store):
    cards = [_card(f"T{i}", 9) for i in range(990)]
    # attempted = universe_total - excluded_types = 1010; 1% of that is 10.1, so 10 errors are allowed
    at_ceiling = _result(cards, universe_total=1050, excluded=40, errors=10)
    assert freeze.freeze_result(at_ceiling, run_id="x", now=POST_CLOSE).frozen == 990


def test_more_than_one_percent_score_errors_freezes_nothing(store):
    cards = [_card(f"T{i}", 9) for i in range(990)]
    over = _result(cards, universe_total=1050, excluded=40, errors=11)                  # 11 > 10.1
    out = freeze.freeze_result(over, run_id="x", now=POST_CLOSE)
    assert out.frozen == 0 and rawstore.count("forecast_ledger") == 0
    (note,) = ledger.take_notes()
    assert "scoring errors" in note and "11" in note


def test_the_attempted_count_excludes_the_excluded_types(store):
    """5 errors in 100 attempted tickers is over the ceiling even though the universe is 1,000."""
    cards = [_card(f"T{i}", 9) for i in range(95)]
    result = _result(cards, universe_total=1000, excluded=900, errors=5)
    assert freeze.freeze_result(result, run_id="x", now=POST_CLOSE).frozen == 0


def test_nothing_attempted_freezes_nothing(store):
    result = _result([_card("AAA", 9)], universe_total=10, excluded=10, errors=0)
    assert freeze.freeze_result(result, run_id="x", now=POST_CLOSE).frozen == 0


def test_a_missing_summary_is_not_a_crash(store):
    result = _result([_card("AAA", 9)])
    del result["summary"]
    assert freeze.freeze_result(result, run_id="x", now=POST_CLOSE).frozen == 1
