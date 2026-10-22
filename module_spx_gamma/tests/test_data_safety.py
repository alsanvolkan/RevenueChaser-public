"""SPX durable data must never be wiped by a corrupt read or a fallback (S-1, S-2).

- S-1: a corrupt / torn ``history.json`` or ``brier.json`` used to read as ``{}``
  and the next write replaced the whole history with one record. Writes are now
  atomic, and a decode error preserves the bad file and refuses the write.
- S-2: when gex-levels falls back to the previous session, the promotion used to
  ``rmtree`` that session's complete raw day and put the hybrid in its place.
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.module_spx_gamma import brier, run, safe_json
from RevenueChaser.module_spx_gamma import snapshot as snap
from RevenueChaser.module_spx_gamma.snapshot import UWApiError

from .conftest import as_uw

TORN = '{"2026-08-13": {"spot": 7700.0}, "2026-08-14": {"sp'


@pytest.fixture
def spx_root(tmp_path, monkeypatch):
    """A temp SPX_GAMMA_DATA_ROOT; the module constants are bound at import."""
    root = tmp_path / "spx_gamma"
    monkeypatch.setenv("SPX_GAMMA_DATA_ROOT", str(root))
    monkeypatch.setattr(run, "HISTORY_PATH", root / "spx_gamma_gex_history.json")
    monkeypatch.setattr(brier, "BRIER_FILE", root / "brier.json")
    return root


def _corrupt_copies(path):
    return sorted(path.parent.glob(path.name + ".corrupt-*"))


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


class TestHistoryCorruption:
    def test_torn_history_is_preserved_not_overwritten(self, spx_root, caplog):
        run.HISTORY_PATH.parent.mkdir(parents=True)
        run.HISTORY_PATH.write_text(TORN, encoding="utf-8")
        with pytest.raises(safe_json.CorruptJsonError):
            run._record_history({"current_level": 7800.0}, "2026-08-17")
        copies = _corrupt_copies(run.HISTORY_PATH)
        assert len(copies) == 1 and copies[0].read_text(encoding="utf-8") == TORN
        # Nothing single-record was written in its place.
        assert not run.HISTORY_PATH.exists()
        assert "corrupt" in caplog.text.lower()

    def test_readers_degrade_to_empty_without_touching_the_file(self, spx_root):
        run.HISTORY_PATH.parent.mkdir(parents=True)
        run.HISTORY_PATH.write_text(TORN, encoding="utf-8")
        assert run._load_history() == {}
        assert run.HISTORY_PATH.read_text(encoding="utf-8") == TORN

    def test_good_history_round_trips(self, spx_root):
        run._record_history({"current_level": 7800.0}, "2026-08-14")
        run._record_history({"current_level": 7810.0}, "2026-08-17")
        assert sorted(run._load_history()) == ["2026-08-14", "2026-08-17"]
        assert [f.name for f in spx_root.iterdir()] == ["spx_gamma_gex_history.json"]


class TestBrierCorruption:
    def test_torn_brier_is_preserved_not_overwritten(self, spx_root):
        spx_root.mkdir(parents=True)
        brier.BRIER_FILE.write_text(TORN, encoding="utf-8")
        with pytest.raises(safe_json.CorruptJsonError):
            brier.record_probs("2026-08-17", {"Consolidation": 0.5, "Upside": 0.3,
                                              "Downside": 0.2})
        copies = _corrupt_copies(brier.BRIER_FILE)
        assert len(copies) == 1 and copies[0].read_text(encoding="utf-8") == TORN
        assert not brier.BRIER_FILE.exists()

    def test_score_date_refuses_too(self, spx_root):
        spx_root.mkdir(parents=True)
        brier.BRIER_FILE.write_text(TORN, encoding="utf-8")
        with pytest.raises(safe_json.CorruptJsonError):
            brier.score_date("2026-08-14", "Upside")
        assert _corrupt_copies(brier.BRIER_FILE)

    def test_scorecard_display_degrades_to_empty(self, spx_root):
        spx_root.mkdir(parents=True)
        brier.BRIER_FILE.write_text(TORN, encoding="utf-8")
        card = brier.scorecard()
        assert card is not None
        assert brier.BRIER_FILE.read_text(encoding="utf-8") == TORN


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
    def _seed_complete(self, raw_root, day="2026-08-14"):
        d = raw_root / day
        d.mkdir(parents=True)
        (d / "gex_levels.json").write_text('{"data": {"call_wall": "1"}}', encoding="utf-8")
        (d / "manifest.json").write_text('{"asof_date": "%s"}' % day, encoding="utf-8")
        return d

    def test_fallback_never_replaces_a_complete_previous_day(self, tmp_path, _no_candle_network):
        raw_root = tmp_path / "raw"
        good = self._seed_complete(raw_root)
        # Monday 2026-08-17 is rejected -> falls back to Friday 2026-08-14.
        snap.fetch_snapshot(as_uw(_FallbackClient("2026-08-17")), "2026-08-17", data_root=raw_root)
        assert '"call_wall": "1"' in (good / "gex_levels.json").read_text(encoding="utf-8")
        assert (good / "manifest.json").read_text(encoding="utf-8") == '{"asof_date": "2026-08-14"}'
        assert [p.name for p in raw_root.iterdir()] == ["2026-08-14"], "no staging left behind"

    def test_fallback_is_written_under_its_true_date_when_absent(self, tmp_path, _no_candle_network):
        raw_root = tmp_path / "raw"
        snap.fetch_snapshot(as_uw(_FallbackClient("2026-08-17")), "2026-08-17", data_root=raw_root)
        assert [p.name for p in raw_root.iterdir()] == ["2026-08-14"]
        man = json.loads((raw_root / "2026-08-14" / "manifest.json").read_text(encoding="utf-8"))
        assert man["asof_date"] == "2026-08-14"

    def test_fallback_replaces_an_incomplete_previous_day(self, tmp_path, _no_candle_network):
        """A day without a manifest is a leftover, not a complete snapshot."""
        raw_root = tmp_path / "raw"
        d = raw_root / "2026-08-14"
        d.mkdir(parents=True)
        (d / "gex_levels.json").write_text('{"data": {"call_wall": "1"}}', encoding="utf-8")
        snap.fetch_snapshot(as_uw(_FallbackClient("2026-08-17")), "2026-08-17", data_root=raw_root)
        assert (d / "manifest.json").exists()


# ── SPX-4 (M-SPX-1, #376): a Refresh never rewrites the Brier record ───────

from .test_s2_fetch import DAY as _S2_DAY, _fetch as _s2_fetch, _no_network as _s2_no_network  # noqa: E402

_PROBS = {"Consolidation": 0.5, "Upside": 0.3, "Downside": 0.2}


def _stored_day(tmp_path, monkeypatch):
    """One stored raw day under ``tmp_path`` built offline, then the network is cut."""
    _s2_fetch(tmp_path / "raw")
    _s2_no_network(monkeypatch)
    return tmp_path / "brier.json", tmp_path / "spx_gamma_gex_history.json"


class TestRefreshKeepsBrier:
    def test_refresh_leaves_brier_byte_identical(self, tmp_path, monkeypatch):
        brier_path, _ = _stored_day(tmp_path, monkeypatch)
        brier.record_probs("2026-08-13", _PROBS, path=brier_path)
        brier.score_date("2026-08-13", "Upside", path=brier_path)
        before = brier_path.read_bytes()
        run.refresh(_S2_DAY, data_root=tmp_path)
        assert brier_path.read_bytes() == before

    def test_refresh_does_not_create_a_brier_record(self, tmp_path, monkeypatch):
        brier_path, _ = _stored_day(tmp_path, monkeypatch)
        run.refresh(_S2_DAY, data_root=tmp_path)
        assert not brier_path.exists()

    def test_refresh_keeps_a_scored_record_of_the_same_day(self, tmp_path, monkeypatch):
        brier_path, _ = _stored_day(tmp_path, monkeypatch)
        brier.record_probs(_S2_DAY, _PROBS, path=brier_path)
        brier.score_date(_S2_DAY, "Downside", path=brier_path)
        run.refresh(_S2_DAY, data_root=tmp_path)
        rec = next(r for r in brier.load_scores(brier_path)["records"] if r["date"] == _S2_DAY)
        assert rec["outcome"] == "Downside" and rec["brier"] is not None

    def test_gex_history_unchanged_after_refresh_of_an_unchanged_day(self, tmp_path, monkeypatch):
        _, history_path = _stored_day(tmp_path, monkeypatch)
        run.refresh(_S2_DAY, data_root=tmp_path)
        first = history_path.read_bytes()
        run.refresh(_S2_DAY, data_root=tmp_path)
        assert history_path.read_bytes() == first
        assert _S2_DAY in json.loads(first)


class TestRecordProbsKeepsScores:
    def test_record_probs_keeps_a_scored_record(self, tmp_path):
        p = tmp_path / "brier.json"
        brier.record_probs("2026-08-14", _PROBS, path=p)
        scored = brier.score_date("2026-08-14", "Upside", path=p)
        brier.record_probs("2026-08-14", {"Consolidation": 0.2, "Upside": 0.2, "Downside": 0.6}, path=p)
        rec = brier.load_scores(p)["records"][0]
        assert rec["outcome"] == "Upside"
        assert rec["brier"] == scored["records"][0]["brier"]
        # the probabilities the score was computed from stay with it
        assert rec["scenario_probs"] == _PROBS

    def test_record_probs_still_replaces_an_unscored_record(self, tmp_path):
        p = tmp_path / "brier.json"
        brier.record_probs("2026-08-14", _PROBS, path=p)
        new = {"Consolidation": 0.2, "Upside": 0.2, "Downside": 0.6}
        brier.record_probs("2026-08-14", new, path=p)
        assert brier.load_scores(p)["records"][0]["scenario_probs"] == new
