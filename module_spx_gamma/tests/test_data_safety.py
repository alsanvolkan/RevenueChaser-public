"""SPX durable data must never be wiped by a fallback or a Refresh (S-1, S-2, SPX-4, F2).

- S-1 (torn ``history.json`` / ``brier.json``) is gone with the files: the records are rows in
  ``boards/spx_gamma.duckdb`` now. ``safe_json`` stays for the history pull's own run files.
- S-2: when gex-levels falls back to the previous session, the stored complete day is kept.
- A Refresh leaves every table of the board file unchanged but the latest pointer (review §9).
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.module_spx_gamma import brier, run, safe_json
from RevenueChaser.module_spx_gamma import snapshot as snap
from RevenueChaser.module_spx_gamma.snapshot import UWApiError
from RevenueChaser.module_spx_gamma import store as spx_store

from .conftest import as_uw, store_day

TORN = '{"2026-08-13": {"spot": 7700.0}, "2026-08-14": {"sp'


class TestAtomicJson:
    def test_write_is_atomic_and_leaves_no_temp(self, tmp_path):
        p = tmp_path / "a.json"
        safe_json.write_json_atomic(p, {"x": 1})
        assert json.loads(p.read_text(encoding="utf-8")) == {"x": 1}
        assert [f.name for f in tmp_path.iterdir()] == ["a.json"]

    def test_failed_serialisation_keeps_the_old_file(self, tmp_path):
        p = tmp_path / "a.json"
        p.write_text('{"keep": true}', encoding="utf-8")
        with pytest.raises(TypeError):
            safe_json.write_json_atomic(p, {"bad": object()})
        assert json.loads(p.read_text(encoding="utf-8")) == {"keep": True}
        assert [f.name for f in tmp_path.iterdir()] == ["a.json"]

    def test_missing_file_reads_as_default(self, tmp_path):
        assert safe_json.load_json(tmp_path / "none.json", {}) == {}

    def test_corrupt_file_raises(self, tmp_path):
        p = tmp_path / "a.json"
        p.write_text(TORN, encoding="utf-8")
        with pytest.raises(safe_json.CorruptJsonError):
            safe_json.load_json(p, {})


class TestGexRecord:
    def test_good_history_round_trips(self):
        run._record_history({"current_level": 7800.0}, "2026-08-14")
        run._record_history({"current_level": 7810.0}, "2026-08-17")
        assert sorted(run._load_history()) == ["2026-08-14", "2026-08-17"]
        assert run._load_history()["2026-08-17"]["spot"] == 7810.0


# ── S-2: fallback promotion ─────────────────────────────────────────────────

GEX = "/api/stock/SPX/gex-levels"


class _FallbackClient:
    """gex-levels rejects ``bad_date`` (422), every other call succeeds."""

    def __init__(self, bad_date: str):
        self.bad_date = bad_date

    def get(self, path, params=None):
        params = params or {}
        if path == GEX and params.get("date") == self.bad_date:
            raise UWApiError(path, 422, "date in the future")
        if path == GEX:
            return {"data": {"call_wall": "9999", "gamma_flip": "7820.63",
                             "gamma_magnet": "7900", "put_wall": "7750"}}
        return {"data": []}


@pytest.fixture
def _no_candle_network(monkeypatch):
    monkeypatch.setattr(snap, "stored_spx_candles", lambda asof=None, ticker=None: [
        {"date": "2026-08-14", "open": 1.0, "high": 2.0, "low": 0.5,
         "close": 7785.76, "volume": 1.0}])


class TestFallbackPromotion:
    def test_fallback_never_replaces_a_complete_previous_day(self, tmp_path, _no_candle_network):
        staging = tmp_path / "staging"
        store_day("2026-08-14", {"asof_date": "2026-08-14"}, {"gex_levels.json": '{"data": {"call_wall": "1"}}'})
        # Monday 2026-08-17 is rejected -> falls back to Friday 2026-08-14.
        snap.fetch_snapshot(as_uw(_FallbackClient("2026-08-17")), "2026-08-17", data_root=staging)
        assert spx_store.raw_files("2026-08-14") == {"gex_levels.json": '{"data": {"call_wall": "1"}}'}
        assert spx_store.raw_manifest("2026-08-14") == {"asof_date": "2026-08-14"}
        assert spx_store.stored_days() == ["2026-08-14"]
        assert list(staging.iterdir()) == [], "no staging left behind"

    def test_fallback_is_written_under_its_true_date_when_absent(self, tmp_path, _no_candle_network):
        staging = tmp_path / "staging"
        snap.fetch_snapshot(as_uw(_FallbackClient("2026-08-17")), "2026-08-17", data_root=staging)
        assert spx_store.stored_days() == ["2026-08-14"]
        man = spx_store.raw_manifest("2026-08-14")
        assert man["asof_date"] == "2026-08-14"
        assert list(staging.iterdir()) == []


# ── SPX-4 (M-SPX-1, #376): a Refresh never rewrites the Brier record ───────

from .test_s2_fetch import DAY as _S2_DAY, _fetch as _s2_fetch, _no_network as _s2_no_network  # noqa: E402

_PROBS = {"Consolidation": 0.5, "Upside": 0.3, "Downside": 0.2}


def _stored_day(tmp_path, monkeypatch):
    """One stored raw day built offline, then the network is cut."""
    _s2_fetch(tmp_path / "staging")
    _s2_no_network(monkeypatch)


def _tables() -> dict:
    """Every row of every board-file table but the latest pointer, which a Refresh of the newest day moves."""
    from RevenueChaser.core import rawstore
    out = {}
    for t in (spx_store.RAW_DAYS, spx_store.RAW_FILES, spx_store.BRIER, spx_store.GEX, spx_store.META):
        cols = [c for c in rawstore._columns_of(rawstore.MANIFEST[t])]
        where = " WHERE key <> 'latest'" if t == spx_store.META else ""
        out[t] = rawstore.query(f"SELECT {', '.join(cols)} FROM {t}{where} ORDER BY ALL", db="spx_gamma")
    return out


class TestRefreshKeepsTheBoardFile:
    def test_refresh_leaves_every_table_unchanged(self, tmp_path, monkeypatch):
        _stored_day(tmp_path, monkeypatch)
        brier.record_probs("2026-08-13", _PROBS)
        brier.score_date("2026-08-13", "Upside")
        run._record_history({"current_level": 7800.0}, _S2_DAY)
        before = _tables()
        run.refresh(_S2_DAY, data_root=tmp_path)
        assert _tables() == before

    def test_refresh_does_not_create_a_brier_or_gex_record(self, tmp_path, monkeypatch):
        _stored_day(tmp_path, monkeypatch)
        run.refresh(_S2_DAY, data_root=tmp_path)
        assert spx_store.brier_records() == [] and spx_store.gex_days() == {}

    def test_refresh_keeps_a_scored_record_of_the_same_day(self, tmp_path, monkeypatch):
        _stored_day(tmp_path, monkeypatch)
        brier.record_probs(_S2_DAY, _PROBS)
        brier.score_date(_S2_DAY, "Downside")
        run.refresh(_S2_DAY, data_root=tmp_path)
        rec = next(r for r in brier.load_scores()["records"] if r["date"] == _S2_DAY)
        assert rec["outcome"] == "Downside" and rec["brier"] is not None


class TestRecordProbsKeepsScores:
    def test_record_probs_keeps_a_scored_record(self, tmp_path):
        brier.record_probs("2026-08-14", _PROBS)
        scored = brier.score_date("2026-08-14", "Upside")
        brier.record_probs("2026-08-14", {"Consolidation": 0.2, "Upside": 0.2, "Downside": 0.6})
        rec = brier.load_scores()["records"][0]
        assert rec["outcome"] == "Upside"
        assert rec["brier"] == scored["records"][0]["brier"]
        # the probabilities the score was computed from stay with it
        assert rec["scenario_probs"] == _PROBS

    def test_record_probs_still_replaces_an_unscored_record(self, tmp_path):
        brier.record_probs("2026-08-14", _PROBS)
        new = {"Consolidation": 0.2, "Upside": 0.2, "Downside": 0.6}
        brier.record_probs("2026-08-14", new)
        assert brier.load_scores()["records"][0]["scenario_probs"] == new
