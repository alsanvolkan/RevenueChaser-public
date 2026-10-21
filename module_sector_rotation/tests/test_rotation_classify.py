"""Tests for rotation engine quadrant classification + z-score alignment."""
from datetime import date

from RevenueChaser.module_sector_rotation.engine import (
    classify_quadrant,
    classify_rrg,
    normalize_z_scores,
    zscore_normalize,
)
from RevenueChaser.core.models.rotation import ETFRotationSnapshot


class TestClassifyQuadrant:
    def test_strict_boundaries(self):
        assert classify_quadrant(1.0, 1.0) == "LEADING"
        assert classify_quadrant(0.0, 0.0) == "LEADING"
        assert classify_quadrant(0.5, -0.5) == "WEAKENING"
        assert classify_quadrant(-0.5, 0.5) == "IMPROVING"
        assert classify_quadrant(-0.5, -0.5) == "LAGGING"

    def test_negative_near_zero_not_leading(self):
        # -0.0001 ratio with positive momentum must be IMPROVING, never LEADING
        assert classify_quadrant(-0.0001, 0.5) == "IMPROVING"
        assert classify_quadrant(-1e-12, -1e-12) == "LAGGING"

    def test_alias_matches_classify_rrg(self):
        for rz in (-1.0, 0.0, 1.0):
            for mz in (-1.0, 0.0, 1.0):
                assert classify_quadrant(rz, mz) == classify_rrg(rz, mz)


class TestNormalizeZAlignment:
    def test_none_snapshot_does_not_misalign(self):
        # A snapshot with None ratio/momentum must not shift subsequent z-scores.
        from RevenueChaser.module_sector_rotation.engine import RotationEngine
        eng = RotationEngine(benchmark="SPY")
        eng.snapshots = [
            ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker="A",
                                group_name="sector", rs_ratio=None, rs_momentum=None),
            ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker="B",
                                group_name="sector", rs_ratio=1.0, rs_momentum=1.0),
            ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker="C",
                                group_name="sector", rs_ratio=-1.0, rs_momentum=-1.0),
        ]
        normalize_z_scores(eng.snapshots)
        b = eng.snapshots[1]
        c = eng.snapshots[2]
        assert b.rs_ratio_z is not None and b.rs_momentum_z is not None
        assert c.rs_ratio_z is not None and c.rs_momentum_z is not None
        assert b.rs_ratio_z > c.rs_ratio_z, "B (1.0) must rank above C (-1.0)"
        assert b.rrg_quadrant == "LEADING"
        assert c.rrg_quadrant == "LAGGING"

    def test_nan_snapshot_does_not_poison_other_zscores(self):
        # One broken ticker (NaN ratio/momentum) must not NaN every other z-score.
        import math
        from RevenueChaser.module_sector_rotation.engine import RotationEngine
        eng = RotationEngine(benchmark="SPY")
        eng.snapshots = [
            ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker="BROKEN",
                                group_name="sector", rs_ratio=float("nan"),
                                rs_momentum=float("nan")),
            ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker="B",
                                group_name="sector", rs_ratio=1.0, rs_momentum=1.0),
            ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker="C",
                                group_name="sector", rs_ratio=-1.0, rs_momentum=-1.0),
        ]
        normalize_z_scores(eng.snapshots)
        broken, b, c = eng.snapshots
        assert broken.rs_ratio_z is None and broken.rs_momentum_z is None
        for s in (b, c):
            assert s.rs_ratio_z is not None and not math.isnan(s.rs_ratio_z)
            assert s.rs_momentum_z is not None and not math.isnan(s.rs_momentum_z)
        assert b.rs_ratio_z > c.rs_ratio_z
        assert b.rrg_quadrant == "LEADING"
        assert c.rrg_quadrant == "LAGGING"

    def test_zscore_normalize_drops_nan(self):
        import math
        out = zscore_normalize([1.0, float("nan"), -1.0])
        assert not any(isinstance(v, float) and math.isnan(v) for v in out)
        assert len(out) == 2, "NaN inputs are dropped from the z-score set"
        assert out[0] > out[1]
