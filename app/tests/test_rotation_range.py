"""Picking a rotation date range must actually narrow the board.

``run_rotation_latest`` backfilled the requested window and then rendered
``_load_rotation_snapshots()`` — every stored date — so whatever range you
picked, the map showed the full history. The picker looked broken because it
was a no-op.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.app import runners


def _row(d: str, ticker: str = "XLK") -> dict:
    return {
        "as_of_date": d, "ticker": ticker, "name": "IT", "group": "sector",
        "close_price": 1.0, "rs_1w": 0, "rs_1m": 0, "rs_3m": 0, "rs_ratio": 0,
        "rs_momentum": 0, "rs_ratio_z": 0, "rs_momentum_z": 0, "quadrant": "LEADING",
    }


@pytest.fixture
def seeded(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    dates = ["2026-07-01", "2026-07-15", "2026-08-12", "2026-08-19", "2026-09-02"]
    for d in dates:
        rawstore.write_rows("rotation_snapshots", [_row(d)])
    return dates


def _dates(snapshots) -> list[str]:
    return sorted({str(getattr(s, "as_of_date", "") or "") for s in snapshots})


class TestLoadRotationSnapshotsRange:
    def test_no_bounds_loads_everything(self, seeded):
        assert len(runners._load_rotation_snapshots()) == len(seeded)

    def test_bounds_narrow_the_result(self, seeded):
        snaps = runners._load_rotation_snapshots(date(2026, 8, 1), date(2026, 8, 31))
        assert len(snaps) == 2

    def test_bounds_are_inclusive(self, seeded):
        snaps = runners._load_rotation_snapshots(date(2026, 8, 12), date(2026, 8, 19))
        assert len(snaps) == 2

    def test_open_start_runs_from_the_first_stored_date(self, seeded):
        snaps = runners._load_rotation_snapshots(None, date(2026, 7, 15))
        assert len(snaps) == 2

    def test_open_end_runs_to_the_last_stored_date(self, seeded):
        snaps = runners._load_rotation_snapshots(date(2026, 8, 12), None)
        assert len(snaps) == 3

    def test_empty_window_returns_nothing(self, seeded):
        assert runners._load_rotation_snapshots(date(2026, 1, 1), date(2026, 1, 31)) == []

    def test_no_snapshots_at_all_is_not_an_error(self, rawstore_tmp_db):
        assert runners._load_rotation_snapshots(date(2026, 1, 1), date(2026, 2, 1)) == []


class TestRunRotationLatestUsesTheRange:
    def _patch(self, monkeypatch, captured):
        from RevenueChaser.module_sector_rotation import store as rotation_store
        monkeypatch.setattr(rotation_store, "backfill_weekly",
                            lambda uw, s, e: captured.update(backfill=(s, e)) or 0)
        monkeypatch.setattr(runners, "_load_rotation_snapshots",
                            lambda start=None, end=None:
                                captured.update(loaded=(start, end)) or [])
        import RevenueChaser.module_sector_rotation.report as rep
        monkeypatch.setattr(rep, "export_rotation_dashboard",
                            lambda *a, **kw: runners.ROTATION_LATEST)

    def test_the_selected_window_is_the_window_that_is_loaded(self, monkeypatch, rawstore_tmp_db):
        captured = {}
        self._patch(monkeypatch, captured)
        runners.run_rotation_latest(None, start="2026-08-01", end="2026-08-31")
        assert captured["loaded"] == (date(2026, 8, 1), date(2026, 8, 31))
        assert captured["backfill"] == (date(2026, 8, 1), date(2026, 8, 31))

    def test_a_reversed_range_is_normalised(self, monkeypatch, rawstore_tmp_db):
        captured = {}
        self._patch(monkeypatch, captured)
        runners.run_rotation_latest(None, start="2026-08-31", end="2026-08-01")
        assert captured["loaded"] == (date(2026, 8, 1), date(2026, 8, 31))

    def test_end_only_still_bounds_the_load(self, monkeypatch, rawstore_tmp_db):
        captured = {}
        self._patch(monkeypatch, captured)
        runners.run_rotation_latest(None, end="2026-08-31")
        assert captured["loaded"][1] == date(2026, 8, 31)

    def test_no_range_does_not_go_through_the_backfill_path(self, monkeypatch, rawstore_tmp_db):
        """Without a range it is a live single-snapshot scan, not a window load."""
        captured = {}
        self._patch(monkeypatch, captured)

        class _Engine:
            def __init__(self, uw, benchmark="SPY"):
                pass

            def process_all(self):
                return []

        import RevenueChaser.module_sector_rotation.engine as eng
        monkeypatch.setattr(eng, "RotationEngine", _Engine)
        monkeypatch.setattr(eng, "fetch_all_holdings", lambda uw, snaps: {})
        from RevenueChaser.app.jobs import JobMessage
        with pytest.raises(JobMessage, match="returned no ETF"):      # ROT-5: an empty live pull says so
            runners.run_rotation_latest(None)
        assert "backfill" not in captured
        assert "loaded" not in captured
