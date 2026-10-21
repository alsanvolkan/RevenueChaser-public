"""Tests for snapshot fetch/persist + SHA-256 manifest."""
from __future__ import annotations

import json

import pytest

from RevenueChaser.module_spx_gamma import snapshot as snap
from RevenueChaser.module_spx_gamma.snapshot import UWApiError

from .conftest import as_uw


class _FakeClient:
    def __init__(self, failures=None):
        self.failures = failures or {}
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path in self.failures:
            raise UWApiError(path, 422, "boom")
        if path.endswith("/gex-levels"):
            return {"data": {"call_wall": "7900", "gamma_flip": "7820.63", "gamma_magnet": "7900", "put_wall": "7750"}}
        if path.endswith("/greek-exposure/expiry"):
            return {"data": [{"expiry": "2026-08-14", "dte": 0}]}
        if path.endswith("/spot-exposures/strike"):
            return {"data": [{"strike": "7900", "call_charm_oi": "1"}]}
        if path.endswith("/strike-expiry"):
            return {"data": [{"expiry": "2026-08-14", "strike": "7900", "call_gex": "1", "put_gex": "-1"}]}
        if path.endswith("/interpolated-iv"):
            return {"data": []}
        return {"data": []}


def _candles():
    return [{"date": "2026-08-14", "open": 1.0, "high": 2.0, "low": 0.5, "close": 7785.76, "volume": 1.0}]


def _candles_fn(asof=None, ticker=None):
    return _candles()


def test_fetch_snapshot_spot_exposure_window(tmp_path, monkeypatch):
    monkeypatch.setattr(snap, "fetch_spx_candles", _candles_fn)
    client = _FakeClient()
    snap.fetch_snapshot(as_uw(client), "2026-08-14", data_root=tmp_path)
    spot_calls = [c for c in client.calls if c[0].endswith("/spot-exposures/strike")]
    assert spot_calls, "spot-exposure must be fetched when spot is available"
    # Two calls (below / above spot, #176 G13) that together span the +/-25% window.
    assert len(spot_calls) == 2
    assert spot_calls[0][1]["min_strike"] == int(7785.76 * 0.75)
    assert spot_calls[1][1]["max_strike"] == int(7785.76 * 1.25)


def test_fetch_snapshot_0dte_only(tmp_path, monkeypatch):
    monkeypatch.setattr(snap, "fetch_spx_candles", _candles_fn)
    client = _FakeClient()
    snap.fetch_snapshot(as_uw(client), "2026-08-14", data_root=tmp_path)
    se_calls = [c for c in client.calls if "strike-expiry" in c[0]]
    assert len(se_calls) == 1  # only the 0DTE (same-day) expiry
    assert se_calls[0][1]["expiry"] == "2026-08-14"


def test_manifest_has_sha256(tmp_path, monkeypatch):
    monkeypatch.setattr(snap, "fetch_spx_candles", _candles_fn)
    client = _FakeClient()
    snap.fetch_snapshot(as_uw(client), "2026-08-14", data_root=tmp_path)
    manifest = json.loads((tmp_path / "2026-08-14" / "manifest.json").read_text(encoding="utf-8"))
    assert "sha256" in manifest
    assert "gex_levels.json" in manifest["sha256"]
    assert manifest["candle_source"] == "yfinance (^GSPC)"
    assert manifest["spot_exposure_mode"] == "open_interest"


def test_mandatory_failure_aborts(tmp_path, monkeypatch):
    monkeypatch.setattr(snap, "fetch_spx_candles", _candles_fn)
    client = _FakeClient(failures={"/api/stock/SPX/gex-levels": True})
    with pytest.raises(snap.SnapshotError):
        snap.fetch_snapshot(as_uw(client), "2026-08-14", data_root=tmp_path)
