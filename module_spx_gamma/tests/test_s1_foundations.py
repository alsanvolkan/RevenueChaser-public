"""SPX Gamma S1 (#176): brier 2/3 + BSS, NY calendar, split spot request, raw-day manifest, isolation."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from RevenueChaser.module_spx_gamma import brier, config, run as spx_run, snapshot as snap
from RevenueChaser.module_spx_gamma.snapshot import UWApiError

from .conftest import as_uw, make_raw

GOOD = {"Consolidation": 0.5, "Upside": 0.3, "Downside": 0.2}


# ── G1: tests never touch the real tree ────────────────────────────────────
def test_data_root_is_redirected_away_from_the_module_tree():
    module_tree = Path(config.__file__).resolve().parent
    assert module_tree not in config.DATA_ROOT.resolve().parents
    assert brier.BRIER_FILE.resolve().parent == config.DATA_ROOT.resolve()
    assert module_tree not in spx_run.HISTORY_PATH.resolve().parents


# ── #69: Brier reference, label validation, skill score ────────────────────
def test_equal_weight_reference_is_two_thirds():
    assert brier.EQUAL_WEIGHT_REFERENCE == pytest.approx(2 / 3)
    # one-hot outcome vs uniform 1/3: (1 - 1/3)^2 + 2 * (1/3)^2
    assert brier.EQUAL_WEIGHT_REFERENCE == pytest.approx((1 - 1 / 3) ** 2 + 2 * (1 / 3) ** 2)


def test_uniform_forecast_scores_exactly_the_reference(tmp_path):
    p = tmp_path / "b.json"
    brier.record_probs("2026-08-21", {c: 1 / 3 for c in brier.CLASSES}, path=p)
    rec = brier.score_date("2026-08-21", "Upside", path=p)["records"][0]
    assert rec["brier"] == pytest.approx(brier.EQUAL_WEIGHT_REFERENCE)


def test_skill_score_zero_for_uniform_one_for_perfect(tmp_path):
    p = tmp_path / "b.json"
    brier.record_probs("2026-08-21", {c: 1 / 3 for c in brier.CLASSES}, path=p)
    brier.score_date("2026-08-21", "Upside", path=p)
    assert brier.scorecard(path=p)["skill_score"] == pytest.approx(0.0)
    q = tmp_path / "c.json"
    brier.record_probs("2026-08-21", {"Consolidation": 1.0, "Upside": 0.0, "Downside": 0.0}, path=q)
    brier.score_date("2026-08-21", "Consolidation", path=q)
    assert brier.scorecard(path=q)["skill_score"] == pytest.approx(1.0)


def test_skill_score_none_when_nothing_scored(tmp_path):
    assert brier.scorecard(path=tmp_path / "none.json")["skill_score"] is None


def test_verdict_names_the_skill_score(tmp_path):
    p = tmp_path / "b.json"
    brier.record_probs("2026-08-21", GOOD, path=p)
    brier.score_date("2026-08-21", "Upside", path=p)
    assert "skill" in brier.scorecard(path=p)["verdict"].lower()


def test_score_date_rejects_an_unknown_label(tmp_path):
    p = tmp_path / "b.json"
    brier.record_probs("2026-08-21", GOOD, path=p)
    with pytest.raises(ValueError):
        brier.score_date("2026-08-21", "Sideways", path=p)
    assert brier.load_scores(p)["records"][0]["outcome"] is None


@pytest.mark.parametrize("probs", [
    {"Consolidation": 0.5, "Upside": 0.3},                                  # missing class
    {"Consolidation": 0.5, "Upside": 0.3, "Downside": 0.5},                 # sums to 1.3
    {"Consolidation": 1.2, "Upside": -0.1, "Downside": -0.1},               # out of range
    {"Consolidation": None, "Upside": 0.5, "Downside": 0.5},                # None
    {"Consolidation": float("nan"), "Upside": 0.5, "Downside": 0.5},        # NaN
])
def test_record_probs_rejects_invalid_probabilities(tmp_path, probs):
    p = tmp_path / "b.json"
    with pytest.raises(ValueError):
        brier.record_probs("2026-08-21", probs, path=p)
    assert not p.exists()


# ── G3/G8/G17: New York calendar ───────────────────────────────────────────
def test_fallback_day_skips_a_holiday():
    # Tue 2026-09-08 follows Labor Day (Mon 09-07): the prior session is Fri 09-04.
    assert snap._previous_est_trading_day("2026-09-08") == "2026-09-04"


def test_fallback_day_plain_weekday_and_weekend():
    assert snap._previous_est_trading_day("2026-08-17") == "2026-08-14"  # Mon -> Fri
    assert snap._previous_est_trading_day("2026-08-14") == "2026-08-13"


def test_fallback_day_outside_the_calendar_table_still_skips_weekends():
    assert snap._previous_est_trading_day("2031-03-03") == "2031-02-28"  # Mon -> Fri


def test_backfill_days_exclude_today_and_holidays():
    today = date(2026, 9, 9)  # Wed after Labor Day
    days = spx_run._missing_backfill_days({}, today, 4)
    assert days == ["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08"]
    assert "2026-09-09" not in days and "2026-09-07" not in days


def test_backfill_days_skip_recorded_days():
    today = date(2026, 8, 19)
    days = spx_run._missing_backfill_days({"2026-08-18": {}, "2026-08-17": {}}, today, 3)
    assert days == ["2026-08-14"]


def test_today_ny_not_the_machine_date_is_used(monkeypatch):
    monkeypatch.setattr(spx_run, "today_ny", lambda: date(2026, 8, 19))
    assert spx_run._backfill_candidates(3) == ["2026-08-14", "2026-08-17", "2026-08-18"]


# ── G13: split spot-exposure request ───────────────────────────────────────
class _SpotClient:
    """Serves 500 rows per spot-exposures call, honouring min/max strike."""

    def __init__(self, full_rows_per_side=500):
        self.calls = []
        self.n = full_rows_per_side

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path.endswith("/gex-levels"):
            return {"data": {"call_wall": "7900", "gamma_flip": "7820", "gamma_magnet": "7900", "put_wall": "7750"}}
        if path.endswith("/greek-exposure/expiry"):
            return {"data": [{"expiry": "2026-08-14", "dte": 0}]}
        if path.endswith("/spot-exposures/strike"):
            lo, hi = params["min_strike"], params["max_strike"]
            rows = [{"strike": str(lo + i), "call_gamma_oi": "1"} for i in range(min(self.n, hi - lo + 1))]
            return {"data": rows}
        return {"data": []}


@pytest.fixture
def _candles(monkeypatch):
    monkeypatch.setattr(snap, "stored_spx_candles", lambda asof=None, ticker=None: [
        {"date": "2026-08-14", "open": 1.0, "high": 2.0, "low": 0.5, "close": 7785.76, "volume": 1.0}])
    monkeypatch.setattr(snap, "fetch_vix_family", lambda: {"tickers": {}, "vix": 15.0})
    monkeypatch.setattr(snap, "fetch_cftc_tff", lambda: None)


def test_spot_exposure_is_two_calls_below_and_above_spot(tmp_path, _candles):
    c = _SpotClient()
    snap.fetch_snapshot(as_uw(c), "2026-08-14", data_root=tmp_path)
    calls = [p for path, p in c.calls if path.endswith("/spot-exposures/strike")]
    assert len(calls) == 2
    lo, hi = int(7785.76 * 0.75), int(7785.76 * 1.25)
    below, above = calls
    assert below["min_strike"] == lo and below["max_strike"] <= 7785
    assert above["min_strike"] >= 7785 and above["max_strike"] == hi
    assert below["limit"] == above["limit"] == 500


def test_spot_exposure_rows_are_merged_and_deduplicated(tmp_path, _candles):
    c = _SpotClient(full_rows_per_side=50)
    raw = snap.fetch_snapshot(as_uw(c), "2026-08-14", data_root=tmp_path)
    strikes = [r["strike"] for r in raw["spot_exposure"]["data"]]
    assert len(strikes) == len(set(strikes)) == 100


def test_limit_hit_is_warned_and_recorded(tmp_path, _candles):
    c = _SpotClient(full_rows_per_side=500)
    snap.fetch_snapshot(as_uw(c), "2026-08-14", data_root=tmp_path)
    man = json.loads((tmp_path / "2026-08-14" / "manifest.json").read_text(encoding="utf-8"))
    assert any("limit" in w and "spot_exposure" in w for w in man["warnings"])
    f = man["files"]["spot_exposures_strike.json"]
    assert f["limit_hit"] is True and f["rows"] == 1000


def test_no_limit_warning_when_below_the_cap(tmp_path, _candles):
    c = _SpotClient(full_rows_per_side=40)
    snap.fetch_snapshot(as_uw(c), "2026-08-14", data_root=tmp_path)
    man = json.loads((tmp_path / "2026-08-14" / "manifest.json").read_text(encoding="utf-8"))
    assert not any("limit" in w for w in man["warnings"])
    assert man["files"]["spot_exposures_strike.json"]["limit_hit"] is False


def test_one_half_failing_keeps_the_other_and_warns(tmp_path, _candles):
    class _C(_SpotClient):
        def get(self, path, params=None):
            if path.endswith("/spot-exposures/strike") and params["min_strike"] > 7000:
                raise UWApiError(path, 500, "boom")
            return super().get(path, params)

    raw = snap.fetch_snapshot(as_uw(_C(full_rows_per_side=10)), "2026-08-14", data_root=tmp_path)
    assert len(raw["spot_exposure"]["data"]) == 10
    man = json.loads((tmp_path / "2026-08-14" / "manifest.json").read_text(encoding="utf-8"))
    assert any("spot_exposure" in w and "partial" in w for w in man["warnings"])


# ── raw-day manifest ───────────────────────────────────────────────────────
def test_manifest_records_requested_and_effective_dates(tmp_path, _candles):
    class _Fallback(_SpotClient):
        def get(self, path, params=None):
            if path.endswith("/gex-levels") and params.get("date") == "2026-08-17":
                raise UWApiError(path, 422, "future")
            return super().get(path, params)

    raw = snap.fetch_snapshot(as_uw(_Fallback()), "2026-08-17", data_root=tmp_path)
    man = json.loads((tmp_path / "2026-08-14" / "manifest.json").read_text(encoding="utf-8"))
    assert man["requested_date"] == "2026-08-17"
    assert man["effective_date"] == man["asof_date"] == "2026-08-14"
    assert raw["_meta"]["requested_date"] == "2026-08-17"
    assert raw["_meta"]["effective_date"] == "2026-08-14"


def test_manifest_marks_each_file_as_of_or_live(tmp_path, _candles):
    snap.fetch_snapshot(as_uw(_SpotClient(40)), "2026-08-14", data_root=tmp_path)
    files = json.loads((tmp_path / "2026-08-14" / "manifest.json").read_text(encoding="utf-8"))["files"]
    assert files["gex_levels.json"]["as_of"] is True
    assert files["spot_exposures_strike.json"]["as_of"] is True
    for live in ("interpolated_iv.json", "max_pain.json", "option_chain.json"):
        if live in files:
            assert files[live]["as_of"] is False, live
    assert files["gex_levels.json"]["rows"] is None or isinstance(files["gex_levels.json"]["rows"], int)


def test_manifest_row_counts(tmp_path, _candles):
    snap.fetch_snapshot(as_uw(_SpotClient(40)), "2026-08-14", data_root=tmp_path)
    files = json.loads((tmp_path / "2026-08-14" / "manifest.json").read_text(encoding="utf-8"))["files"]
    assert files["spot_exposures_strike.json"]["rows"] == 80
    assert files["greek_exposure_expiry.json"]["rows"] == 1


def test_backfill_never_overwrites_a_complete_day(tmp_path, _candles):
    day = tmp_path / "2026-08-14"
    day.mkdir()
    (day / "manifest.json").write_text('{"asof_date": "2026-08-14"}', encoding="utf-8")
    (day / "gex_levels.json").write_text('{"keep": 1}', encoding="utf-8")
    c = _SpotClient()
    assert snap.fetch_snapshot(as_uw(c), "2026-08-14", data_root=tmp_path, overwrite=False) is None
    assert c.calls == []
    assert (day / "gex_levels.json").read_text(encoding="utf-8") == '{"keep": 1}'


def test_overwrite_default_still_replaces(tmp_path, _candles):
    day = tmp_path / "2026-08-14"
    day.mkdir()
    (day / "manifest.json").write_text('{"asof_date": "2026-08-14"}', encoding="utf-8")
    (day / "gex_levels.json").write_text('{"keep": 1}', encoding="utf-8")
    snap.fetch_snapshot(as_uw(_SpotClient()), "2026-08-14", data_root=tmp_path)
    assert "call_wall" in (day / "gex_levels.json").read_text(encoding="utf-8")


# ── run(): effective date, isolation, backfill off the critical path ───────
def _patch_run(monkeypatch, order, raw):
    monkeypatch.setattr(spx_run, "UWClient", lambda *a, **k: object())

    def _fs(*a, **k):
        order.append("fetch")
        return raw

    monkeypatch.setattr(spx_run, "fetch_snapshot", _fs)
    monkeypatch.setattr(spx_run, "render_html", lambda *a, **k: "<html></html>")
    monkeypatch.setattr(spx_run, "render_markdown", lambda *a, **k: "# md")
    monkeypatch.setattr(spx_run, "_export_chart", lambda fig, d, n: (None, None, ""))

    class _Fig:
        pass

    monkeypatch.setattr(spx_run.position_map, "build", lambda *a, **k: _Fig())
    monkeypatch.setattr(spx_run, "build_tornado_fig", lambda *a: _Fig())
    monkeypatch.setattr(spx_run.position_map, "build_envelope_fig", lambda *a: _Fig())


def test_backfill_runs_after_the_main_pipeline(tmp_path, monkeypatch):
    order = []
    _patch_run(monkeypatch, order, make_raw())
    monkeypatch.setattr(spx_run, "_backfill_history", lambda *a, **k: order.append("backfill"))
    spx_run.run("2026-08-14", data_root=tmp_path, token="x")
    assert order == ["fetch", "backfill"]


def test_backfill_failure_does_not_fail_the_run(tmp_path, monkeypatch):
    order = []
    _patch_run(monkeypatch, order, make_raw())

    def _boom(*a, **k):
        raise RuntimeError("uw down")

    monkeypatch.setattr(spx_run, "_backfill_history", _boom)
    out = spx_run.run("2026-08-14", data_root=tmp_path, token="x")
    assert out["latest"]["asof_date"] == "2026-08-14"


def test_fallback_day_is_filed_under_its_effective_date(tmp_path, monkeypatch):
    raw = make_raw()
    raw["_meta"] = {"requested_date": "2026-08-17", "effective_date": "2026-08-14"}
    order = []
    _patch_run(monkeypatch, order, raw)
    monkeypatch.setattr(spx_run, "_backfill_history", lambda *a, **k: None)
    out = spx_run.run("2026-08-17", data_root=tmp_path, token="x")
    assert (tmp_path / "parsed" / "2026-08-14" / "snapshot.json").exists()
    assert not (tmp_path / "parsed" / "2026-08-17").exists()
    assert not (tmp_path / "reports" / "2026-08-17").exists()
    assert out["latest"]["asof_date"] == "2026-08-14"
    assert out["latest"]["requested_date"] == "2026-08-17"
    assert out["latest"]["effective_date"] == "2026-08-14"
    hist = json.loads((tmp_path / "spx_gamma_gex_history.json").read_text(encoding="utf-8"))
    assert list(hist) == ["2026-08-14"]


def test_run_with_data_root_writes_brier_and_history_inside_it(tmp_path, monkeypatch):
    _patch_run(monkeypatch, [], make_raw())
    monkeypatch.setattr(spx_run, "_backfill_history", lambda *a, **k: None)
    real_brier = brier.BRIER_FILE
    before = real_brier.read_text(encoding="utf-8") if real_brier.exists() else None
    spx_run.run("2026-08-14", data_root=tmp_path, token="x")
    assert (tmp_path / "brier.json").exists()
    assert (tmp_path / "spx_gamma_gex_history.json").exists()
    after = real_brier.read_text(encoding="utf-8") if real_brier.exists() else None
    assert before == after


def test_report_shows_reference_and_skill_score(monkeypatch):
    from RevenueChaser.module_spx_gamma import render
    card = {"average": 0.5, "scored_count": 3, "verdict": "v", "calibrated": True, "records": [],
            "equal_weight_reference": 2 / 3, "skill_score": 0.25}
    monkeypatch.setattr(render, "scorecard", lambda *a, **k: card)
    monkeypatch.setattr(spx_run, "_load_history", lambda *a, **k: {})
    from .conftest import make_snapshot
    html = render.render_html(make_snapshot())
    md = render.render_markdown(make_snapshot())
    assert "0.6667" in html and "Skill score:</span> +0.25" in html
    assert "Equal-weight ref: 0.6667" in md and "Skill score: +0.25" in md
