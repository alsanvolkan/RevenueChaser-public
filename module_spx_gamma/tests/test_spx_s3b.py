"""S3b (#38): the SPX scenario is frozen in ``forecast_ledger`` and scored in ``brier.json``.

avo's cards (2026-10-25): one ledger row per Fetch day (``source`` 'spx_gamma', ``subject`` 'SPX', ``target_type``
'scenario_3class', the three probabilities in ``probs``); it resolves on the last session of its week, or of the
next week when the forecast day is already that session; the realized class reads the ``^GSPC`` close of
``index_levels_daily`` (D03); the Brier score stays in ``brier.json`` and only a Fetch writes it; older records
are scored from their parsed snapshot. Offline: temp store, temp data root, the clock passed in.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_spx_gamma import brier, freeze, scenarios
from RevenueChaser.module_spx_gamma import run as spx_run

from .conftest import make_raw, make_snapshot

DAY = "2026-08-14"                  # make_raw's effective date, a Friday
AFTER_DAY_CLOSE = datetime(2026, 8, 14, 21, 0, tzinfo=timezone.utc)      # 17:00 ET
PROBS = {"Consolidation": 0.45, "Upside": 0.25, "Downside": 0.30}
BANDS = scenarios.scenario_bands(make_snapshot())           # consolidation 7820.63 - 7920


def _close(day: str, close: float) -> None:
    rawstore.write_rows("index_levels_daily", [{
        "symbol": "^GSPC", "session_date": date.fromisoformat(day), "open": close, "high": close,
        "low": close, "close": close, "volume": 1.0, "source": "yfinance", "fetched_at": 1.0,
        "run_id": "seed", "run_at": 1.0}])


def _ledger():
    return rawstore.query("SELECT * FROM forecast_ledger WHERE source = 'spx_gamma'")


# ── the weekly target ────────────────────────────────────────────────────────

@pytest.mark.parametrize("day, target", [
    (date(2026, 8, 10), date(2026, 8, 14)),     # Monday -> that Friday
    (date(2026, 8, 13), date(2026, 8, 14)),     # Thursday -> that Friday
    (date(2026, 8, 14), date(2026, 8, 21)),     # Friday is the week's last session -> next Friday
    (date(2026, 8, 15), date(2026, 8, 21)),     # a Saturday (never a session) -> next week's
    (date(2026, 4, 1), date(2026, 4, 2)),       # Good Friday 2026-04-03: the week ends on Thursday
    (date(2026, 4, 2), date(2026, 4, 10)),      # ... and from that Thursday it is the next week's Friday
])
def test_week_target_is_the_last_session_of_the_week_after_the_forecast_day(day, target):
    assert freeze.week_target(day) == target


def test_week_target_is_none_past_the_holiday_table():
    assert freeze.week_target(date(2031, 6, 2)) is None


# ── model version ────────────────────────────────────────────────────────────

def test_model_version_is_pinned():
    """A probability or band-offset change must bump SELECTION_VERSION; this golden fails until it does."""
    assert freeze.model_version() == "spx-s3b-1+b19957aa"


def test_model_version_changes_with_a_probability(monkeypatch):
    before = freeze.model_version()
    monkeypatch.setattr(scenarios, "UP_P", 0.26)
    assert freeze.model_version() != before


def test_model_version_changes_with_a_band_offset(monkeypatch):
    before = freeze.model_version()
    real = scenarios.scenario_bands

    def wider(snap):
        b = real(snap)
        return None if b is None else {**b, "upside": (b["upside"][0], b["upside"][1] + 1)}
    monkeypatch.setattr(scenarios, "scenario_bands", wider)
    assert freeze.model_version() != before


# ── record + score in brier.json ─────────────────────────────────────────────

def test_record_probs_keeps_the_target_and_bands(tmp_path):
    p = tmp_path / "brier.json"
    brier.record_probs(DAY, PROBS, path=p, target_date="2026-08-21", bands=BANDS)
    [rec] = brier.load_scores(p)["records"]
    assert rec["target_date"] == "2026-08-21"
    assert rec["bands"]["consolidation"] == list(BANDS["consolidation"])


@pytest.mark.parametrize("close, realized", [(7870.0, "Consolidation"), (7950.0, "Upside"), (7700.0, "Downside")])
def test_score_due_scores_a_record_whose_target_close_is_stored(tmp_path, rawstore_tmp_db, close, realized):
    p = tmp_path / "brier.json"
    brier.record_probs(DAY, PROBS, path=p, target_date="2026-08-21", bands=BANDS)
    _close("2026-08-21", close)
    assert freeze.score_due(p, tmp_path) == 1
    [rec] = brier.load_scores(p)["records"]
    assert rec["outcome"] == realized
    assert rec["brier"] == pytest.approx(brier.brier(PROBS, realized))
    assert brier.load_scores(p)["n"] == 1


def test_score_due_leaves_a_record_without_its_target_close(tmp_path, rawstore_tmp_db):
    p = tmp_path / "brier.json"
    brier.record_probs(DAY, PROBS, path=p, target_date="2026-08-21", bands=BANDS)
    _close("2026-08-20", 7950.0)                 # the day before the target: not the target close
    before = p.read_bytes()
    assert freeze.score_due(p, tmp_path) == 0
    assert p.read_bytes() == before              # nothing to score, nothing written
    assert brier.load_scores(p)["records"][0]["outcome"] is None


def test_score_due_never_rescores_a_scored_record(tmp_path, rawstore_tmp_db):
    p = tmp_path / "brier.json"
    brier.record_probs(DAY, PROBS, path=p, target_date="2026-08-21", bands=BANDS)
    _close("2026-08-21", 7950.0)
    freeze.score_due(p, tmp_path)
    before = p.read_bytes()
    assert freeze.score_due(p, tmp_path) == 0
    assert p.read_bytes() == before


def test_score_due_scores_an_old_record_from_its_parsed_snapshot(tmp_path, rawstore_tmp_db):
    """A record from before S3b has no target or bands: both are derived (week rule, parsed snapshot)."""
    p = tmp_path / "brier.json"
    brier.record_probs(DAY, PROBS, path=p)                       # the old shape
    parsed = tmp_path / "parsed" / DAY
    parsed.mkdir(parents=True)
    (parsed / "snapshot.json").write_text(json.dumps(make_snapshot(), default=str), encoding="utf-8")
    _close("2026-08-21", 7700.0)
    assert freeze.score_due(p, tmp_path) == 1
    assert brier.load_scores(p)["records"][0]["outcome"] == "Downside"


def test_score_due_skips_an_old_record_without_its_parsed_snapshot(tmp_path, rawstore_tmp_db):
    p = tmp_path / "brier.json"
    brier.record_probs(DAY, PROBS, path=p)
    _close("2026-08-21", 7700.0)
    assert freeze.score_due(p, tmp_path) == 0


def test_score_due_without_a_brier_file_writes_nothing(tmp_path, rawstore_tmp_db):
    assert freeze.score_due(tmp_path / "brier.json", tmp_path) == 0
    assert not (tmp_path / "brier.json").exists()


# ── the job: build_day ───────────────────────────────────────────────────────

@pytest.fixture
def after_close(monkeypatch):
    """The Fetch runs after the close of DAY: its effective day is the latest complete session."""
    real = ledger.session_gate
    monkeypatch.setattr(ledger, "session_gate", lambda now=None: real(AFTER_DAY_CLOSE))


def test_a_fetch_freezes_one_scenario_row(tmp_path, rawstore_tmp_db, after_close):
    ledger.take_notes()
    spx_run.build_day(make_raw(), DAY, tmp_path, write_png=False)
    [row] = _ledger()
    assert (row["subject"], row["target_type"]) == ("SPX", "scenario_3class")
    assert row["forecast_date"] == date(2026, 8, 14) and row["target_date"] == date(2026, 8, 21)
    assert json.loads(row["probs"]) == PROBS
    assert row["model_version"] == freeze.model_version()
    assert row["shown_price"] == make_snapshot()["current_level"]
    assert any("Froze 1 SPX scenario rows for 2026-08-14" in n for n in ledger.take_notes())
    rec = brier.load_scores(tmp_path / "brier.json")["records"][0]
    assert rec["target_date"] == "2026-08-21" and rec["bands"] is not None


def test_a_second_fetch_the_same_day_adds_no_row(tmp_path, rawstore_tmp_db, after_close):
    spx_run.build_day(make_raw(), DAY, tmp_path, write_png=False)
    spx_run.build_day(make_raw(), DAY, tmp_path, write_png=False)
    assert len(_ledger()) == 1


def test_a_fetch_of_a_past_day_records_but_does_not_freeze(tmp_path, rawstore_tmp_db):
    """The real clock is long past DAY, so DAY is not the latest complete session (or the run is in session)."""
    ledger.take_notes()
    spx_run.build_day(make_raw(), DAY, tmp_path, write_png=False)
    assert _ledger() == []
    assert any(n.startswith("SPX scenario not frozen") for n in ledger.take_notes())
    assert brier.load_scores(tmp_path / "brier.json")["records"][0]["date"] == DAY


def test_a_refresh_neither_freezes_nor_scores(tmp_path, rawstore_tmp_db, after_close):
    p = tmp_path / "brier.json"
    brier.record_probs("2026-08-07", PROBS, path=p, target_date="2026-08-14", bands=BANDS)
    _close("2026-08-14", 7950.0)
    before = p.read_bytes()
    spx_run.build_day(make_raw(), DAY, tmp_path, record=False, write_png=False)
    assert _ledger() == []
    assert p.read_bytes() == before


def test_a_fetch_scores_the_records_that_are_due(tmp_path, rawstore_tmp_db, after_close):
    p = tmp_path / "brier.json"
    brier.record_probs("2026-08-07", PROBS, path=p, target_date="2026-08-14", bands=BANDS)
    _close("2026-08-14", 7950.0)
    spx_run.build_day(make_raw(), DAY, tmp_path, write_png=False)
    recs = {r["date"]: r for r in brier.load_scores(p)["records"]}
    assert recs["2026-08-07"]["outcome"] == "Upside"
    assert recs[DAY]["outcome"] is None                      # its own target (2026-08-21) has no close yet
    assert brier.scorecard(p)["scored_count"] == 1


def test_the_fetch_report_shows_the_score_it_just_made(tmp_path, rawstore_tmp_db, after_close):
    p = tmp_path / "brier.json"
    brier.record_probs("2026-08-07", PROBS, path=p, target_date="2026-08-14", bands=BANDS)
    _close("2026-08-14", 7950.0)
    out = spx_run.build_day(make_raw(), DAY, tmp_path, write_png=False)
    html = out["paths"]["report_html"].read_text(encoding="utf-8")
    assert 'Scored weeks:</span> 1' in html


def test_a_store_failure_in_the_freeze_is_a_note_not_a_failed_build(tmp_path, rawstore_tmp_db, after_close,
                                                                    monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store locked")
    monkeypatch.setattr(ledger, "write_dated", boom)
    ledger.take_notes()
    out = spx_run.build_day(make_raw(), DAY, tmp_path, write_png=False)
    assert out["paths"]["report_html"].exists()
    assert any("SPX scenario not frozen" in n and "store locked" in n for n in ledger.take_notes())
