"""Same-day freshness for Earnings Radar's Sector Rotation dependency.

``engine.rotation_quadrant_map`` must reuse a snapshot already stored for the
day (Sector Rotation board refreshed first), compute + persist one only when the
day is missing, and never touch the network when told ``compute=False``.
"""
from datetime import date

from RevenueChaser.module_sector_rotation.models import ETFRotationSnapshot
from RevenueChaser.module_sector_rotation import engine as rotation_engine
from RevenueChaser.module_sector_rotation import store as rotation_store


def _row(asof, ticker="ITA", quadrant="IMPROVING"):
    return {"as_of_date": asof, "ticker": ticker, "name": "x", "group": "theme",
            "close_price": 100.0, "rs_1w": 1.0, "rs_1m": 2.0, "rs_3m": 3.0,
            "rs_ratio": 1.0, "rs_momentum": 1.0, "rs_ratio_z": 1.0, "rs_momentum_z": 1.0,
            "quadrant": quadrant}


def _snap(ticker, quadrant):
    return ETFRotationSnapshot(
        as_of_date=date(2026, 9, 10), ticker=ticker, group_name="sector",
        close_price=100.0, rs_1w=1.0, rs_1m=2.0, rs_3m=3.0,
        rs_ratio=2.0, rs_momentum=1.0, rs_ratio_z=1.0, rs_momentum_z=0.5,
        rrg_quadrant=quadrant,
    )


class _BoomUW:
    """Any network call means the helper is doing something it should not."""

    def __getattr__(self, name):
        raise AssertionError(f"rotation_quadrant_map must not call {name}")


class TestStoredSameDayReused:
    def test_stored_snapshot_is_reused_without_engine(self, rawstore_tmp_db):
        asof = date(2026, 9, 10)
        rotation_store.save_snapshot([_snap("XLK", "LEADING"), _snap("XLF", "LAGGING")],
                                     as_of=asof)
        m = rotation_engine.rotation_quadrant_map(uw=_BoomUW(), as_of=asof)
        assert m == {"XLK": "LEADING", "XLF": "LAGGING"}
        assert len(m) == 2


class TestComputesAndPersistsWhenMissing:
    def test_missing_day_runs_engine_and_saves(self, rawstore_tmp_db, monkeypatch):
        asof = date(2026, 9, 10)

        class _StubEngine:
            def __init__(self, uw, benchmark="SPY"):
                self._uw = uw
                self._benchmark = benchmark
                _StubEngine.calls += 1

            def process_all(self):
                return [_snap("XLK", "LEADING"), _snap("XLF", "LAGGING")]

        _StubEngine.calls = 0
        monkeypatch.setattr(rotation_engine, "RotationEngine", _StubEngine)

        m = rotation_engine.rotation_quadrant_map(uw=object(), as_of=asof)
        assert m == {"XLK": "LEADING", "XLF": "LAGGING"}
        assert _StubEngine.calls == 1
        stored = rotation_store.load_snapshot(asof)
        assert stored is not None and len(stored["etfs"]) == 2

    def test_second_call_reuses_what_first_persisted(self, rawstore_tmp_db, monkeypatch):
        asof = date(2026, 9, 10)

        class _StubEngine:
            def __init__(self, uw, benchmark="SPY"):
                _StubEngine.calls += 1

            def process_all(self):
                return [_snap("XLK", "LEADING")]

        _StubEngine.calls = 0
        monkeypatch.setattr(rotation_engine, "RotationEngine", _StubEngine)
        rotation_engine.rotation_quadrant_map(uw=object(), as_of=asof)
        m = rotation_engine.rotation_quadrant_map(uw=_BoomUW(), as_of=asof)
        assert m == {"XLK": "LEADING"}
        assert _StubEngine.calls == 1  # persisted on run 1, reused on run 2


class TestReadOnly:
    def test_compute_false_never_runs_engine_or_writes(self, rawstore_tmp_db, monkeypatch):
        asof = date(2026, 9, 10)

        def _boom(uw=None, benchmark="SPY"):
            raise AssertionError("compute=False must not construct an engine")

        monkeypatch.setattr(rotation_engine, "RotationEngine", _boom)
        assert rotation_engine.rotation_quadrant_map(uw=_BoomUW(), as_of=asof, compute=False) == {}
        assert rotation_store.load_snapshot(asof) is None
