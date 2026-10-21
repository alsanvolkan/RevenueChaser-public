"""A failed SPX snapshot must explain itself and must not destroy the cache.

Two defects lived in one code path:
  - the abort message was "mandatory endpoint 'gex_levels' failed", which does
    not distinguish a dead API key from a date UW has no data for;
  - ``fetch_snapshot`` deleted the target directory *before* fetching, so a
    failed run wiped a good cached day and left an empty directory behind.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_spx_gamma import snapshot as snap
from RevenueChaser.module_spx_gamma.snapshot import UWApiError

from .conftest import as_uw

GEX = "/api/stock/SPX/gex-levels"


class _Client:
    """Fails gex-levels with a given status; everything else succeeds."""

    def __init__(self, status: int, detail: str = "boom", fail_paths=(GEX,)):
        self.status = status
        self.detail = detail
        self.fail_paths = fail_paths
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path in self.fail_paths:
            raise UWApiError(path, self.status, self.detail)
        if path.endswith("/gex-levels"):
            return {"data": {"call_wall": "7900", "gamma_flip": "7820.63",
                             "gamma_magnet": "7900", "put_wall": "7750"}}
        return {"data": []}


def _candles_fn(asof=None, ticker=None):
    return [{"date": "2026-08-14", "open": 1.0, "high": 2.0, "low": 0.5,
             "close": 7785.76, "volume": 1.0}]


@pytest.fixture(autouse=True)
def _no_candle_network(monkeypatch):
    monkeypatch.setattr(snap, "fetch_spx_candles", _candles_fn)


class TestMandatoryFailureMessage:
    def test_401_points_at_the_api_key_not_the_date(self):
        msg = snap._mandatory_failure_message("gex_levels", GEX, ["2026-08-14"], 401, "")
        assert "UNUSUAL_WHALES_API_KEY" in msg
        assert "https://unusualwhales.com/api" in msg
        assert "2026-08-14" not in msg

    def test_403_is_also_a_credential_message(self):
        assert "rejected" in snap._mandatory_failure_message(
            "gex_levels", GEX, ["2026-08-14"], 403, "").lower()

    def test_404_says_the_date_has_no_data(self):
        msg = snap._mandatory_failure_message("gex_levels", GEX, ["2026-06-01"], 404, "")
        assert "2026-06-01" in msg
        assert "recent" in msg

    def test_422_says_the_date_is_invalid(self):
        msg = snap._mandatory_failure_message("gex_levels", GEX, ["2026-08-15"], 422, "")
        assert "422" in msg
        assert "non-trading day" in msg

    def test_429_says_rate_limited(self):
        assert "rate-limit" in snap._mandatory_failure_message(
            "gex_levels", GEX, ["2026-08-14"], 429, "")

    def test_no_status_reads_as_a_transport_failure(self):
        msg = snap._mandatory_failure_message("gex_levels", GEX, ["2026-08-14"], None, "timeout")
        assert "network or timeout" in msg

    def test_unknown_status_still_names_endpoint_and_status(self):
        msg = snap._mandatory_failure_message("gex_levels", GEX, ["2026-08-14"], 500, "oops")
        assert "HTTP 500" in msg and "gex_levels" in msg

    def test_both_attempted_dates_are_named(self):
        msg = snap._mandatory_failure_message(
            "gex_levels", GEX, ["2026-08-15", "2026-08-14"], 404, "")
        assert "2026-08-15" in msg and "2026-08-14" in msg


class TestAbortMessageEndToEnd:
    def test_dead_key_surfaces_the_credential_message(self, tmp_path):
        with pytest.raises(snap.SnapshotError) as exc:
            snap.fetch_snapshot(as_uw(_Client(401)), "2026-08-14", data_root=tmp_path)
        assert "UNUSUAL_WHALES_API_KEY" in str(exc.value)

    def test_unavailable_date_surfaces_the_date_message(self, tmp_path):
        with pytest.raises(snap.SnapshotError) as exc:
            snap.fetch_snapshot(as_uw(_Client(404)), "2026-08-14", data_root=tmp_path)
        assert "404" in str(exc.value)
        assert "UNUSUAL_WHALES_API_KEY" not in str(exc.value)

    def test_the_old_opaque_message_is_gone(self, tmp_path):
        with pytest.raises(snap.SnapshotError) as exc:
            snap.fetch_snapshot(as_uw(_Client(422)), "2026-08-14", data_root=tmp_path)
        assert "aborting snapshot" not in str(exc.value)


class TestCacheIsNotDestroyedByAFailedFetch:
    def _seed(self, tmp_path, day="2026-08-14"):
        d = tmp_path / day
        d.mkdir(parents=True)
        (d / "gex_levels.json").write_text('{"data": {"call_wall": "1"}}', encoding="utf-8")
        return d

    def test_existing_snapshot_survives_a_failed_fetch(self, tmp_path):
        cached = self._seed(tmp_path)
        with pytest.raises(snap.SnapshotError):
            snap.fetch_snapshot(as_uw(_Client(401)), "2026-08-14", data_root=tmp_path)
        assert (cached / "gex_levels.json").exists(), "a failed fetch must not delete the cache"
        assert '"call_wall": "1"' in (cached / "gex_levels.json").read_text()

    def test_no_empty_directory_is_left_behind(self, tmp_path):
        with pytest.raises(snap.SnapshotError):
            snap.fetch_snapshot(as_uw(_Client(401)), "2026-08-14", data_root=tmp_path)
        assert not (tmp_path / "2026-08-14").exists()
        assert list(tmp_path.iterdir()) == [], "no staging directory may survive"

    def test_a_successful_fetch_replaces_the_cached_day(self, tmp_path):
        cached = self._seed(tmp_path)
        snap.fetch_snapshot(as_uw(_Client(200, fail_paths=())), "2026-08-14", data_root=tmp_path)
        assert (cached / "manifest.json").exists()
        assert '"call_wall": "1"' not in (cached / "gex_levels.json").read_text()

    def test_a_successful_fetch_leaves_no_staging_directory(self, tmp_path):
        snap.fetch_snapshot(as_uw(_Client(200, fail_paths=())), "2026-08-14", data_root=tmp_path)
        assert [p.name for p in tmp_path.iterdir()] == ["2026-08-14"]

    def test_stale_files_from_a_prior_run_are_not_carried_over(self, tmp_path):
        cached = self._seed(tmp_path)
        (cached / "leftover.json").write_text("{}", encoding="utf-8")
        snap.fetch_snapshot(as_uw(_Client(200, fail_paths=())), "2026-08-14", data_root=tmp_path)
        assert not (cached / "leftover.json").exists()
