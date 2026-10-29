"""Fundamentals board -> forecast ledger (#128). Offline; the store is a temp DuckDB."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.core.tests.d01_seed import seed_d01
from RevenueChaser.module_fundamentals import config, freeze

D = date(2026, 10, 2)
POST_CLOSE = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)      # 17:00 ET
INTRADAY = datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc)        # 12:00 ET


def _cand(ticker, ro40, multiplier=2, **kw):
    c = {"ticker": ticker, "price": 42.0, "market_cap": 5.0e9, "sector": "Health Care",
         "passed_count": multiplier, "bucket": kw.pop("bucket", "quality"), "ro40": ro40}
    c.update(kw)
    return c


def _payload(candidates, **kw):
    p = {"no_data": False, "data_quality": {"spy_stale": False},
         "candidates": candidates}
    p.update(kw)
    return p


def _close_plus(minutes):
    return cal.session_close(D).timestamp() + minutes * 60


def _seed(ticker, last_day="2026-10-02", cached_at=None):
    """The bars in D01 (the picks and SPY, read first; #383 R3 C12/K8b) and in ``swing_ohlc`` (SPY's fallback)."""
    at = _close_plus(45) if cached_at is None else cached_at
    rawstore.write_rows("swing_ohlc", [
        {"ticker": ticker, "ts": f"{d}T00:00:00", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
         "volume": 100.0, "cached_at": at}
        for d in ("2026-09-30", "2026-10-01", last_day)])
    seed_d01(ticker, ("2026-09-30", "2026-10-01", last_day), at)


def _stored():
    return rawstore.query("SELECT * FROM forecast_ledger WHERE source = 'fundamentals' ORDER BY rank")


@pytest.fixture
def seeded(rawstore_tmp_db):
    for t in ("SPY", "AAA", "BBB"):
        _seed(t)
    ledger.take_notes()
    return rawstore_tmp_db


def test_picks_map_the_shown_candidates_column_by_column():
    rows = freeze.picks([_cand("AAA", 61.5, 3, bucket="rule_of_40+quality+momentum"),
                         _cand("BBB", None, 1, bucket="quality")])
    assert rows[0] == {"subject": "AAA", "rank": 1, "score": 61.5, "bucket": "rule_of_40+quality+momentum",
                       "market_cap": 5.0e9, "sector": "Health Care", "shown_price": 42.0, "stop": None,
                       "target": None}
    assert rows[1]["rank"] == 2 and rows[1]["score"] is None and rows[1]["bucket"] == "quality"


def test_rank_is_the_one_based_position_in_candidates():
    assert [r["rank"] for r in freeze.picks([_cand("A", 3), _cand("B", 2)])] == [1, 2]


# Spelled out on purpose: the thresholds the board scores with today. Editing one in config.py fails
# this test until ``SELECTION_VERSION`` is bumped and this block is updated with it.
GOLDEN_CONFIG = {
    "MIN_PRICE": 5.0, "MIN_DOLLAR_VOLUME": 250000, "RO40_MIN": 40.0, "GROWTH_MIN": 0.2, "ROIC_MIN": 0.15, "GM_MIN": 0.3,
    "ND_EBITDA_MAX": 3.0, "DIST_52W_MAX": 0.25, "RS_6M_MIN": 0.0, "ABOVE_200D": True,
    "DISTORTED_GROWTH": 2.0, "DISTORTED_REV_MIN": 10000000, "TAX_RATE": 0.21,
    "EXCLUDED_SECTORS": ["Financial Services", "Real Estate"], "RO40_BARRIER": 150.0,
}
GOLDEN_VERSION = "7"
GOLDEN_MODEL_VERSION = "7+e68896f3"


def test_the_effective_config_is_the_golden_config():
    assert freeze.effective_config() == GOLDEN_CONFIG


def test_the_model_version_is_the_golden_version():
    assert config.SELECTION_VERSION == GOLDEN_VERSION
    assert freeze.model_version() == GOLDEN_MODEL_VERSION


def test_a_post_close_fetch_freezes_the_shown_picks(seeded):
    out = freeze.freeze_payload(_payload([_cand("AAA", 70.0), _cand("BBB", 55.0)]), run_id="job1",
                                now=POST_CLOSE)
    assert (out.frozen, out.source, out.label) == (2, "fundamentals", "Fundamentals")
    rows = _stored()
    assert [(r["subject"], r["rank"], r["forecast_date"], r["target_date"]) for r in rows] == [
        ("AAA", 1, D, D), ("BBB", 2, D, D)]
    assert {r["model_version"] for r in rows} == {freeze.model_version()}
    assert ledger.take_notes() == ["Froze 2 Fundamentals rows for 2026-10-02."]


def test_a_second_fetch_the_same_day_with_another_set_changes_nothing(seeded):
    freeze.freeze_payload(_payload([_cand("AAA", 70.0)]), run_id="one", now=POST_CLOSE)
    before = _stored()
    assert freeze.freeze_payload(_payload([_cand("BBB", 99.0)]), run_id="two", now=POST_CLOSE).frozen == 0
    assert _stored() == before


def test_an_intraday_run_freezes_nothing(seeded):
    assert freeze.freeze_payload(_payload([_cand("AAA", 70.0)]), run_id="x", now=INTRADAY).frozen == 0
    assert rawstore.count("forecast_ledger") == 0
    assert "session window" in ledger.take_notes()[0]


def test_a_post_close_run_on_bars_cached_intraday_freezes_nothing(rawstore_tmp_db):
    for t in ("SPY", "AAA"):
        _seed(t, cached_at=_close_plus(-265))
    ledger.take_notes()
    out = freeze.freeze_payload(_payload([_cand("AAA", 70.0)]), run_id="x", now=POST_CLOSE)
    assert out.frozen == 0 and "before the close + 30 min" in ledger.take_notes()[0]


def test_one_candidate_without_the_session_bar_skips_the_whole_board(seeded):
    _seed("BBB", last_day="2026-10-01")
    out = freeze.freeze_payload(_payload([_cand("AAA", 70.0), _cand("BBB", 55.0)]), run_id="x", now=POST_CLOSE)
    assert out.frozen == 0 and rawstore.count("forecast_ledger") == 0
    (note,) = ledger.take_notes()
    assert note.startswith("Fundamentals not frozen:") and "1 of 2" in note
    assert "no 2026-10-02 bar after the close" in note and "BBB" in note and "AAA" not in note


def test_a_candidate_without_its_bar_after_daily_bars_ran_is_named_as_such(seeded):
    """D01 holds SPY's D bar and BBB's last bar is D-1: another Fetch cannot complete the day (#434 review S2)."""
    _seed("BBB", last_day="2026-10-01", cached_at=_close_plus(-1200))
    out = freeze.freeze_payload(_payload([_cand("AAA", 70.0), _cand("BBB", 55.0)]), run_id="x", now=POST_CLOSE)
    assert out.frozen == 0 and ("no 2026-10-02 bar after the close (a halt or a gap in the daily bars; a later "
                                "Fetch will not change it unless the daily bars are read again): BBB") in ledger.take_notes()[0]


def test_a_stale_spy_freezes_nothing(seeded):
    out = freeze.freeze_payload(_payload([_cand("AAA", 70.0)], data_quality={"spy_stale": True}),
                                run_id="x", now=POST_CLOSE)
    assert out.frozen == 0 and rawstore.count("forecast_ledger") == 0


@pytest.mark.parametrize("kw", [{"is_fixture": True}, {"no_data": True}])
def test_a_fixture_or_no_data_payload_freezes_nothing(seeded, kw):
    assert freeze.freeze_payload(_payload([_cand("AAA", 70.0)], **kw), run_id="x", now=POST_CLOSE).frozen == 0
