"""SPX Gamma S2 (#177): one fetch layer, injected inputs, one normalize, Refresh without network."""
from __future__ import annotations

import importlib.util
import json

import httpx
import pytest

from RevenueChaser.module_spx_gamma import raw_day, run as spx_run, snapshot as snap
from RevenueChaser.module_spx_gamma.normalize import normalize_snapshot

from .conftest import FakeUW, make_raw

DAY = "2026-08-14"
CANDLES = [{"date": DAY, "open": 1.0, "high": 2.0, "low": 0.5, "close": 7785.76, "volume": 1.0}]
VIX = {"tickers": {}, "vix": 15.5}
CFTC = {"report_date": "2026-08-11", "open_interest": 1, "asset_manager": {"long": 1, "short": 2, "net": -1, "change": 0}}


def _handler(path, params):
    if path.endswith("/gex-levels"):
        return {"data": {"call_wall": "7900", "gamma_flip": "7820.63", "gamma_magnet": "7900", "put_wall": "7750"}}
    if path.endswith("/greek-exposure/expiry"):
        return {"data": [{"expiry": DAY, "dte": 0, "call_gex": "5", "put_gex": "-1"}]}
    if path.endswith("/greek-exposure/strike"):
        return {"data": [{"strike": "7800", "call_gex": "3", "put_gex": "-1"}]}
    if path.endswith("/flow-per-strike"):
        return [{"strike": "7800", "call_volume": 1}]          # a bare list, unlike the rest
    if path.endswith("/spot-exposures/strike"):
        return {"data": [{"strike": str(params["min_strike"]), "call_gamma_oi": "1"}]}
    if path.endswith("/strike-expiry"):
        return {"data": [{"expiry": DAY, "strike": "7800", "call_gex": "1", "put_gex": "-1"}]}
    return {"data": []}


def _fetch(tmp_path, handler=_handler, **kw):
    uw = FakeUW(handler, prefix="")
    raw = snap.fetch_snapshot(uw, DAY, data_root=tmp_path,
                              candle_fetcher=lambda *a, **k: list(CANDLES), **kw)
    return uw, raw


# ── one fetch layer ─────────────────────────────────────────────────────────
def test_the_module_has_no_client_of_its_own():
    assert importlib.util.find_spec("RevenueChaser.module_spx_gamma.uw_client") is None


def test_every_uw_call_goes_through_get_result_with_a_core_path(tmp_path, monkeypatch):
    def _no_http(*a, **k):
        raise AssertionError("bare httpx call")

    monkeypatch.setattr(httpx, "get", _no_http)
    uw, _ = _fetch(tmp_path)
    assert uw.paths and all(p.startswith("/stock/SPX/") for p in uw.paths)


def test_flow_per_strike_bare_list_is_kept(tmp_path):
    _, raw = _fetch(tmp_path)
    assert raw["flow_per_strike"] == {"data": [{"strike": "7800", "call_volume": 1}]}
    assert (tmp_path / DAY / "flow_per_strike.json").exists()


def test_flow_per_strike_failure_is_only_a_warning(tmp_path):
    def h(path, params):
        if path.endswith("/flow-per-strike"):
            raise snap.UWApiError(path, 500, "boom")
        return _handler(path, params)

    _fetch(tmp_path, h)
    man = json.loads((tmp_path / DAY / "manifest.json").read_text(encoding="utf-8"))
    assert any(w.startswith("flow_per_strike") for w in man["warnings"])


def test_a_malformed_payload_is_a_failed_call(tmp_path):
    def h(path, params):
        if path.endswith("/interpolated-iv"):
            return {"nope": 1}                                   # no 'data'
        return _handler(path, params)

    _, raw = _fetch(tmp_path, h)
    assert "interpolated_iv" not in raw
    man = json.loads((tmp_path / DAY / "manifest.json").read_text(encoding="utf-8"))
    assert any("interpolated_iv" in w and "malformed" in w for w in man["warnings"])


def test_a_mandatory_failure_still_names_the_status(tmp_path):
    def h(path, params):
        if path.endswith("/gex-levels"):
            raise snap.UWApiError(path, 404, "gone")
        return _handler(path, params)

    with pytest.raises(snap.SnapshotError) as exc:
        _fetch(tmp_path, h)
    assert "HTTP 404" in str(exc.value)


# ── injected inputs ─────────────────────────────────────────────────────────
def test_candles_are_injected_and_vix_and_cot_are_not_fetched(tmp_path):
    """PR 3: the snapshot asks no source for the VIX family or the COT; run.build_day reads them from the store."""
    _, raw = _fetch(tmp_path)
    assert "vix" not in raw and "cftc" not in raw
    assert raw["ohlc"]["data"] == CANDLES
    assert raw["ohlc"]["source"].startswith("rcdata:market_series")


def test_an_injected_fetcher_failing_is_a_warning_not_an_abort(tmp_path):
    def boom(*a, **k):
        raise RuntimeError("offline")

    uw = FakeUW(_handler, prefix="")
    raw = snap.fetch_snapshot(uw, DAY, data_root=tmp_path, candle_fetcher=boom)
    man = json.loads((tmp_path / DAY / "manifest.json").read_text(encoding="utf-8"))
    assert any(w.startswith("SPX closes read failed") for w in man["warnings"])
    assert raw["ohlc"]["source"] == "unavailable"


def test_defaults_still_resolve_the_module_functions(tmp_path, monkeypatch):
    monkeypatch.setattr(snap, "stored_spx_candles", lambda asof=None, ticker=None: list(CANDLES))
    raw = snap.fetch_snapshot(FakeUW(_handler, prefix=""), DAY, data_root=tmp_path)
    assert raw["ohlc"]["data"] == CANDLES


# ── one normalize, deterministic ────────────────────────────────────────────
def test_normalize_is_deterministic_and_takes_its_stamp_from_the_raw_day():
    raw = make_raw()
    raw["_meta"] = {"requested_date": DAY, "effective_date": DAY, "api_snapshot_time": "2026-08-14T21:05:00Z"}
    a, b = normalize_snapshot(raw, DAY), normalize_snapshot(raw, DAY)
    assert a == b and a["api_snapshot_time"] == "2026-08-14T21:05:00Z"


def test_normalize_without_a_stamp_has_none_rather_than_now():
    assert normalize_snapshot(make_raw(), DAY)["api_snapshot_time"] is None


def test_load_raw_day_round_trips_the_fetch(tmp_path):
    _, raw = _fetch(tmp_path, fetch_all_expiries=True)
    loaded = raw_day.load_raw_day(tmp_path / DAY)
    assert loaded["_meta"]["effective_date"] == DAY
    assert loaded["_meta"]["api_snapshot_time"]
    # The render reads the window, VIX and COT from the market store for the day (PR 3), so the two dicts it is
    # handed are the same once the store's inputs are added to each.
    assert (normalize_snapshot(spx_run.with_store_inputs(loaded, DAY), DAY)
            == normalize_snapshot(spx_run.with_store_inputs(raw, DAY), DAY))


def test_load_raw_day_keys_the_zero_dte_file_by_its_expiry(tmp_path):
    _fetch(tmp_path)
    loaded = raw_day.load_raw_day(tmp_path / DAY)
    assert list(loaded["strike_expiry"]) == [DAY]


def test_load_raw_day_refuses_a_day_without_a_manifest(tmp_path):
    (tmp_path / DAY).mkdir()
    with pytest.raises(FileNotFoundError):
        raw_day.load_raw_day(tmp_path / DAY)


# ── Refresh: rebuild from the stored day, no network ───────────────────────
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(snap, "stored_spx_candles", boom)
    monkeypatch.setattr(spx_run, "UWClient", boom)


def test_refresh_rebuilds_the_latest_day_from_disk(tmp_path, monkeypatch):
    _fetch(tmp_path / "raw")
    _no_network(monkeypatch)
    out = spx_run.refresh(data_root=tmp_path)
    assert out["latest"]["asof_date"] == DAY
    assert (tmp_path / "parsed" / DAY / "snapshot.json").exists()
    assert (tmp_path / "reports" / DAY / "section.html").exists()
    assert (tmp_path / "latest.json").exists()


def test_refresh_of_a_named_day(tmp_path, monkeypatch):
    _fetch(tmp_path / "raw")
    _no_network(monkeypatch)
    assert spx_run.refresh(DAY, data_root=tmp_path)["latest"]["asof_date"] == DAY


def test_refresh_without_any_stored_day_returns_none(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    assert spx_run.refresh(data_root=tmp_path) is None


def test_refresh_does_not_backfill(tmp_path, monkeypatch):
    _fetch(tmp_path / "raw")
    _no_network(monkeypatch)
    monkeypatch.setattr(spx_run, "_backfill_history",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("backfill")))
    spx_run.refresh(data_root=tmp_path)


def test_refresh_keeps_the_raw_day_untouched(tmp_path, monkeypatch):
    _fetch(tmp_path / "raw")
    before = {p.name: p.read_bytes() for p in (tmp_path / "raw" / DAY).iterdir()}
    _no_network(monkeypatch)
    spx_run.refresh(data_root=tmp_path)
    assert {p.name: p.read_bytes() for p in (tmp_path / "raw" / DAY).iterdir()} == before


# ── the job passes the dashboard's client through ──────────────────────────
def test_run_uses_the_client_it_is_given(tmp_path, monkeypatch):
    seen = {}

    def _fs(client, *a, **k):
        seen["client"] = client
        return make_raw()

    monkeypatch.setattr(spx_run, "fetch_snapshot", _fs)
    monkeypatch.setattr(spx_run, "_backfill_history", lambda *a, **k: None)
    monkeypatch.setattr(spx_run, "UWClient", lambda *a, **k: (_ for _ in ()).throw(AssertionError("own client")))
    sentinel = FakeUW(_handler)
    sentinel.key = "k"
    spx_run.run(DAY, data_root=tmp_path, uw=sentinel)
    assert seen["client"] is sentinel


def test_run_without_a_client_or_key_refuses(tmp_path):
    with pytest.raises(RuntimeError, match="UNUSUAL_WHALES_API_KEY"):
        spx_run.run(DAY, data_root=tmp_path, token="")


def test_the_runner_hands_the_job_client_to_the_module(monkeypatch):
    from RevenueChaser.app import runners

    got = {}
    monkeypatch.setattr("RevenueChaser.module_spx_gamma.run.run", lambda asof, **k: got.update(asof=asof, **k))
    marker = object()
    runners.run_spxgamma(marker, DAY)
    assert got["uw"] is marker and got["asof"] == DAY
