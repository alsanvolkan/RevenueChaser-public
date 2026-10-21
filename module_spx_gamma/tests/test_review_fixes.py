"""Review fixes for #176 / #177 (M3 M4 M5 M6 and the four L2 test gaps)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from RevenueChaser.module_spx_gamma import render, run as spx_run, snapshot as snap
from RevenueChaser.module_spx_gamma.normalize import compute_positioning_deltas, normalize_snapshot

from .conftest import FakeUW, make_raw, make_snapshot
from .test_s2_fetch import CANDLES, CFTC, DAY, VIX, _fetch, _handler, _no_network

ROOT = Path(__file__).resolve().parents[2]


# ── M3: the Refresh button reaches the SPX Refresh ─────────────────────────
def test_the_refresh_button_sends_spx_gamma_to_the_store_path():
    html = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert "'module_spx_gamma'" in html.split("var STORE_BACKED")[1].split("];")[0]


# ── M4: a fallback Fetch builds from the stored day, not the staging raw ───
def test_a_fallback_fetch_hands_back_the_stored_day(tmp_path):
    def with_iv(value):
        def h(path, params):
            if path.endswith("/interpolated-iv"):
                return {"data": [{"date": DAY, "volatility": value}]}
            return _handler(path, params)
        return h

    _fetch(tmp_path, with_iv(0.1))                                    # the complete stored day

    def fallback(path, params):
        if path.endswith("/gex-levels") and params.get("date") == "2026-08-17":
            raise snap.UWApiError(path, 422, "future")
        return with_iv(0.2)(path, params)

    uw = FakeUW(fallback, prefix="")
    raw = snap.fetch_snapshot(uw, "2026-08-17", data_root=tmp_path, vix_fetcher=lambda: dict(VIX),
                              cftc_fetcher=lambda: dict(CFTC), candle_fetcher=lambda *a, **k: list(CANDLES))
    assert raw["_meta"]["effective_date"] == DAY
    assert raw["interpolated_iv"]["data"][0]["volatility"] == 0.1          # the stored IV, not today's 0.2
    stored = json.loads((tmp_path / DAY / "interpolated_iv.json").read_text(encoding="utf-8"))
    assert stored["data"][0]["volatility"] == 0.1


# ── M5: the backfill repairs a missing history row from disk ───────────────
def test_backfill_records_the_history_row_of_a_complete_raw_day(tmp_path, monkeypatch):
    _fetch(tmp_path / "raw")
    _no_network(monkeypatch)
    monkeypatch.setattr(spx_run, "today_ny", lambda: date(2026, 8, 17))
    monkeypatch.setattr(spx_run.time if hasattr(spx_run, "time") else __import__("time"), "sleep", lambda s: None)

    class _NoCalls:
        def get_result(self, *a, **k):
            raise AssertionError("network used")

    spx_run._backfill_history(tmp_path, _NoCalls(), "SPX", "2026-08-17", days=1)
    history = json.loads((tmp_path / "spx_gamma_gex_history.json").read_text(encoding="utf-8"))
    assert DAY in history


# ── M6 / G17: deltas are relative to the day shown ─────────────────────────
HIST = {"2026-08-07": {"net_gex_oi": 50.0, "is_friday_close": True},
        "2026-08-12": {"net_gex_oi": 100.0},
        "2026-08-13": {"net_gex_oi": 300.0}}


def test_1d_is_the_newest_record_before_the_day_even_before_its_own_row_exists():
    out = compute_positioning_deltas(374.0, "net_gex_oi", HIST, asof="2026-08-14")
    assert out["delta_1d"] == 74.0 and out["delta_1w"] == 324.0


def test_the_days_own_row_and_newer_rows_do_not_leak_into_its_deltas():
    hist = {**HIST, "2026-08-14": {"net_gex_oi": 374.0, "is_friday_close": True},
            "2026-08-17": {"net_gex_oi": 999.0}}
    out = compute_positioning_deltas(374.0, "net_gex_oi", hist, asof="2026-08-14")
    assert out["delta_1d"] == 74.0 and out["delta_1w"] == 324.0         # not the Friday row of the day itself


def test_refresh_of_an_older_day_is_measured_against_earlier_records():
    out = compute_positioning_deltas(300.0, "net_gex_oi", HIST, asof="2026-08-13")
    assert out["delta_1d"] == 200.0


def test_render_passes_the_day_to_the_deltas(monkeypatch):
    seen = []
    real = render.compute_positioning_deltas
    monkeypatch.setattr(render, "compute_positioning_deltas",
                        lambda *a, **k: (seen.append(k.get("asof")), real(*a, **k))[1])
    render.build_context(make_snapshot())
    assert seen and set(seen) == {"2026-08-14"}


# ── L2-1: VIX and CFTC are marked live in the manifest ─────────────────────
def test_vix_and_cftc_are_live_files(tmp_path):
    _fetch(tmp_path)
    files = json.loads((tmp_path / DAY / "manifest.json").read_text(encoding="utf-8"))["files"]
    assert files["vix_family.json"]["as_of"] is False
    assert files["cftc_tff.json"]["as_of"] is False
    assert files["gex_levels.json"]["as_of"] is True


# ── L2-2: no regime shows the UNKNOWN chip, never NEGATIVE ─────────────────
def test_the_unknown_chip_is_rendered_without_a_regime():
    s = normalize_snapshot({}, DAY)
    assert render.build_context(s)["header_strip"]["source_chip"] == "UNKNOWN"
    assert ">UNKNOWN<" in render.render_html(s)


# ── L2-3: /run/spxgamma defaults to the New York day ───────────────────────
def test_run_spxgamma_defaults_to_the_new_york_date(monkeypatch):
    from RevenueChaser.app import dashboard

    seen = {}
    monkeypatch.setattr(dashboard, "today_ny", lambda: date(2026, 8, 14))
    monkeypatch.setattr(dashboard, "_submit", lambda module, params: seen.update(module=module, **params) or ("ok", 200))
    dashboard.app.config["TESTING"] = True
    dashboard.app.test_client().post("/run/spxgamma", headers={"X-RC-Panel": "cli"})
    assert seen == {"module": "spxgamma", "asof_date": "2026-08-14"}


# ── L2-4: refreshing an older day leaves latest.json on the newest day ─────
def test_refresh_of_an_older_day_keeps_latest_on_the_newest(tmp_path, monkeypatch):
    for day in ("2026-08-13", DAY):
        d = tmp_path / "raw" / day
        # one stored day per date: reuse the fetch with the day's own candle
        uw = FakeUW(_handler, prefix="")
        snap.fetch_snapshot(uw, day, data_root=tmp_path / "raw", vix_fetcher=lambda: dict(VIX),
                            cftc_fetcher=lambda: dict(CFTC),
                            candle_fetcher=lambda *a, _d=day, **k: [{**CANDLES[0], "date": _d}])
    _no_network(monkeypatch)
    spx_run.refresh(DAY, data_root=tmp_path)
    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))["asof_date"] == DAY
    spx_run.refresh("2026-08-13", data_root=tmp_path)
    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))["asof_date"] == DAY


# ── DF20: the header gamma flip has one source (the OI gex-levels gamma_flip) ──
def test_header_gamma_flip_has_no_second_fallback_key():
    s = make_snapshot()
    s["levels"].pop("gamma_flip_all", None)
    s["levels"]["regime_threshold"] = 7000.0
    assert render.build_context(s)["header_strip"]["gamma_flip"] == render.fmt_level(None)
