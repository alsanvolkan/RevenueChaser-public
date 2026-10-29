"""Swing board -> forecast ledger (#128). Offline; the store is a temp DuckDB."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.core.tests.d01_seed import seed_d01
from RevenueChaser.module_swing_potential import config, freeze

D = date(2026, 10, 2)
POST_CLOSE = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)      # 17:00 ET
INTRADAY = datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc)        # 12:00 ET


def _cand(ticker, quality, bucket="minervini+weinstein", **kw):
    c = {"ticker": ticker, "price": 20.0, "market_cap": 3.0e9, "sector": "Industrials",
         "bucket": bucket, "quality": quality, "stop": 18.0, "target": 24.0}
    c.update(kw)
    return c


def _payload(candidates, **kw):
    p = {"is_fixture": False, "no_data": False, "data_quality": {"spy_stale": False},
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
    return rawstore.query("SELECT * FROM forecast_ledger WHERE source = 'swing' ORDER BY rank")


@pytest.fixture
def seeded(rawstore_tmp_db):
    for t in ("SPY", "AAA", "BBB", "CCC"):
        _seed(t)
    ledger.take_notes()
    return rawstore_tmp_db


# ── the mapping ──────────────────────────────────────────────────────────────

def test_picks_map_the_shown_candidates_column_by_column():
    rows = freeze.picks([_cand("AAA", 91.5, "minervini+weinstein"),
                         _cand("BBB", None, "qullamaggie", stop=None, target=None)])
    assert rows[0] == {"subject": "AAA", "rank": 1, "score": 91.5, "bucket": "minervini+weinstein",
                       "market_cap": 3.0e9, "sector": "Industrials", "shown_price": 20.0, "stop": 18.0,
                       "target": 24.0}
    assert rows[1]["rank"] == 2 and rows[1]["score"] is None and rows[1]["bucket"] == "qullamaggie"
    assert rows[1]["stop"] is None and rows[1]["target"] is None


def test_rank_is_the_one_based_position_in_candidates():
    assert [r["rank"] for r in freeze.picks([_cand("A", 3), _cand("B", 2), _cand("C", 1)])] == [1, 2, 3]


# ── model version ────────────────────────────────────────────────────────────

# Spelled out on purpose: this is the config the board scores with today. Editing any threshold in
# config.py fails this test until ``SELECTION_VERSION`` is bumped and this block is updated with it.
GOLDEN_CONFIG = {
    "MIN_PRICE": 5.0, "MIN_DOLLAR_VOLUME": 250000, "RS_MIN": 70, "ABOVE_52W_LOW": 1.25, "WITHIN_52W_HIGH": 0.75, "VOL_CONFIRM": 1.5,
    "VOL_WINDOW": 5, "VOL_AVG_DAYS": 20, "BREAKOUT_HIGH_PCT": 0.98, "MOVE_MIN": 0.25, "ADR_MIN": 0.03, "W_12_1": 0.5, "W_PROX": 0.25,
    "W_CONS": 0.25, "CONSISTENCY_LOOKBACK": 126, "STOP_ATR": 2.0, "TARGET_R": 2.0, "ATR_PERIOD": 14,
    "Q_WEIGHTS": {"minervini": 40, "weinstein": 30, "qullamaggie": 30},
    "Q_RULE_TOTALS": {"minervini": 8, "weinstein": 5, "qullamaggie": 6},
}
GOLDEN_VERSION = "5"                # 5: the Weinstein volume window (Swing review finding 3, PR 6)
GOLDEN_MODEL_VERSION = "5+c94564a9"


def test_the_effective_config_is_the_golden_config():
    assert freeze.effective_config() == GOLDEN_CONFIG


def test_the_model_version_is_the_golden_version():
    assert config.SELECTION_VERSION == GOLDEN_VERSION
    assert freeze.model_version() == GOLDEN_MODEL_VERSION


# ── the whole freeze ─────────────────────────────────────────────────────────

def test_a_post_close_fetch_freezes_the_shown_picks(seeded):
    out = freeze.freeze_payload(_payload([_cand("AAA", 90.0), _cand("BBB", 80.0)]), run_id="job1",
                                now=POST_CLOSE)
    assert (out.frozen, out.source, out.label) == (2, "swing", "Swing")
    rows = _stored()
    assert [(r["subject"], r["rank"], r["forecast_date"], r["target_date"]) for r in rows] == [
        ("AAA", 1, D, D), ("BBB", 2, D, D)]
    assert {r["run_id"] for r in rows} == {"job1"}
    assert {r["model_version"] for r in rows} == {freeze.model_version()}
    assert ledger.take_notes() == ["Froze 2 Swing rows for 2026-10-02."]


def test_a_second_fetch_the_same_day_changes_nothing(seeded):
    freeze.freeze_payload(_payload([_cand("AAA", 90.0), _cand("BBB", 80.0)]), run_id="one", now=POST_CLOSE)
    before = _stored()
    out = freeze.freeze_payload(_payload([_cand("CCC", 99.0), _cand("AAA", 50.0)]), run_id="two",
                                now=POST_CLOSE)
    assert out.frozen == 0
    assert _stored() == before


def test_an_intraday_run_freezes_nothing(seeded):
    out = freeze.freeze_payload(_payload([_cand("AAA", 90.0)]), run_id="x", now=INTRADAY)
    assert out.frozen == 0 and rawstore.count("forecast_ledger") == 0
    assert "session window" in ledger.take_notes()[0]


def test_a_post_close_run_on_bars_cached_intraday_freezes_nothing(rawstore_tmp_db):
    """Option B: the 23:00 run reuses bars an 11:35 ET Fetch cached; the day is skipped and named."""
    for t in ("SPY", "AAA"):
        _seed(t, cached_at=_close_plus(-265))
    ledger.take_notes()
    out = freeze.freeze_payload(_payload([_cand("AAA", 90.0)]), run_id="x", now=POST_CLOSE)
    assert out.frozen == 0 and rawstore.count("forecast_ledger") == 0
    (note,) = ledger.take_notes()
    assert "2026-10-02" in note and "before the close + 30 min" in note


def test_one_halted_candidate_skips_the_whole_board_and_names_it(seeded):
    """All-or-nothing: BBB's newest bar is a day old and was written after the close, so no later Fetch
    changes it. The board is not frozen, and the note says why and does not promise a fix."""
    _seed("BBB", last_day="2026-10-01")                  # written at close + 45 min by default
    out = freeze.freeze_payload(_payload([_cand("AAA", 90.0), _cand("BBB", 80.0), _cand("CCC", 70.0)]),
                                run_id="x", now=POST_CLOSE)
    assert out.frozen == 0 and rawstore.count("forecast_ledger") == 0
    (note,) = ledger.take_notes()
    assert note.startswith("Swing not frozen:") and "1 of 3" in note
    assert ("no 2026-10-02 bar after the close (a halt or a gap in the daily bars; a later Fetch will not "
            "change it unless the daily bars are read again): BBB") in note
    assert "AAA" not in note and "CCC" not in note and "later Fetch can" not in note


def test_a_candidate_still_cached_intraday_skips_the_whole_board_and_says_a_refetch_fixes_it(seeded):
    """Option A refetches such bars at load time; one still partial here (the refetch failed) refuses the
    board, and the note says a later Fetch can complete the day."""
    _seed("CCC", cached_at=_close_plus(-265))
    out = freeze.freeze_payload(_payload([_cand("AAA", 90.0), _cand("CCC", 70.0)]), run_id="x", now=POST_CLOSE)
    assert out.frozen == 0 and rawstore.count("forecast_ledger") == 0
    (note,) = ledger.take_notes()
    assert ("need their 2026-10-02 bar (none in the prices store read past the close + 30 min yet; "
            "a later Fetch can complete the day once the builder's daily bars have run): CCC") in note
    assert "1 of 2" in note and "reuses" not in note


def test_a_candidate_without_its_bar_after_daily_bars_ran_is_named_as_such(seeded):
    """Last bar D-1, written that evening, while D01 holds SPY's D bar: Daily Bars ran and BBB got none (a halt).
    D01 is append-only, so another Fetch cannot complete the day (#434 review S2)."""
    _seed("BBB", last_day="2026-10-01", cached_at=_close_plus(-1200))
    out = freeze.freeze_payload(_payload([_cand("AAA", 90.0), _cand("BBB", 80.0)]), run_id="x", now=POST_CLOSE)
    assert out.frozen == 0
    (note,) = ledger.take_notes()
    assert ("no 2026-10-02 bar after the close (a halt or a gap in the daily bars; "
            "a later Fetch will not change it unless the daily bars are read again): BBB") in note
    assert "can complete the day" not in note


def test_the_candidates_are_named_in_board_rank_order(seeded):
    for t in ("CCC", "AAA"):
        _seed(t, cached_at=_close_plus(-265))
    out = freeze.freeze_payload(_payload([_cand("CCC", 90.0), _cand("BBB", 80.0), _cand("AAA", 70.0)]),
                                run_id="x", now=POST_CLOSE)
    assert out.frozen == 0 and "CCC, AAA" in ledger.take_notes()[0]


def test_a_complete_board_is_frozen_whole_after_a_refused_run(seeded):
    """The refusal writes nothing, so a later run with the bars fixed freezes the full board."""
    _seed("BBB", last_day="2026-10-01")
    picks = [_cand("AAA", 90.0), _cand("BBB", 80.0), _cand("CCC", 70.0)]
    assert freeze.freeze_payload(_payload(picks), run_id="a", now=POST_CLOSE).frozen == 0
    _seed("BBB")
    out = freeze.freeze_payload(_payload(picks), run_id="b", now=POST_CLOSE)
    assert out.frozen == 3 and [r["subject"] for r in _stored()] == ["AAA", "BBB", "CCC"]


def test_a_stale_spy_freezes_nothing(seeded):
    out = freeze.freeze_payload(_payload([_cand("AAA", 90.0)], data_quality={"spy_stale": True}),
                                run_id="x", now=POST_CLOSE)
    assert out.frozen == 0 and rawstore.count("forecast_ledger") == 0


@pytest.mark.parametrize("kw", [{"is_fixture": True}, {"no_data": True}])
def test_a_fixture_or_no_data_payload_freezes_nothing(seeded, kw):
    out = freeze.freeze_payload(_payload([_cand("AAA", 90.0)], **kw), run_id="x", now=POST_CLOSE)
    assert out.frozen == 0 and rawstore.count("forecast_ledger") == 0


def test_an_empty_payload_freezes_nothing(seeded):
    assert freeze.freeze_payload(_payload([]), run_id="x", now=POST_CLOSE).frozen == 0


def test_no_spy_bars_freeze_nothing(rawstore_tmp_db):
    _seed("AAA")
    ledger.take_notes()
    out = freeze.freeze_payload(_payload([_cand("AAA", 90.0)]), run_id="x", now=POST_CLOSE)
    assert out.frozen == 0 and "no SPY" in ledger.take_notes()[0]


def test_the_run_at_00_30_utc_is_still_the_previous_new_york_session(rawstore_tmp_db):
    for t in ("SPY", "AAA"):
        _seed(t, last_day="2026-10-26", cached_at=cal.session_close(date(2026, 10, 26)).timestamp() + 2700)
    ledger.take_notes()
    out = freeze.freeze_payload(_payload([_cand("AAA", 90.0)]), run_id="x",
                                now=datetime(2026, 10, 27, 0, 30, tzinfo=timezone.utc))
    assert out.forecast_date == date(2026, 10, 26)
    assert _stored()[0]["forecast_date"] == date(2026, 10, 26)


def test_the_freeze_uses_the_clock_seam_when_no_time_is_passed(seeded, monkeypatch):
    monkeypatch.setattr(cal, "_now", lambda: POST_CLOSE)
    assert freeze.freeze_payload(_payload([_cand("AAA", 90.0)]), run_id="x").frozen == 1
