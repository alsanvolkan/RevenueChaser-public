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


class TestPerGroupZScores:
    def _snapshot(self, ticker, group, ratio, momentum):
        return ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker=ticker,
                                   group_name=group, rs_ratio=ratio, rs_momentum=momentum)

    def test_large_group_uses_its_own_z_score(self):
        # Eight sector ETFs with a tight spread and one theme ETF far away.
        # If the sector group used the pooled distribution, its z-scores would be compressed by the outlier.
        sectors = [self._snapshot(f"X{i}", "sector", float(i), 0.0) for i in range(1, 9)]
        theme = [self._snapshot("THEME", "theme", 100.0, 0.0)]
        all_snaps = sectors + theme
        normalize_z_scores(all_snaps)

        sector_zs = [s.rs_ratio_z for s in sectors]

        # The sector group has 8 finite ratios -> per-group z-scores.
        assert max(sector_zs) > 1.5, "sector group z-score should reflect its own spread"

        # Compute what the pooled z-score would have been for the top sector.
        pooled_z = zscore_normalize([s.rs_ratio for s in all_snaps])
        top_sector_pooled = pooled_z[7]  # sectors[7] has ratio 8.0
        assert max(sector_zs) != top_sector_pooled, "sector z-score must differ from pooled z-score"

    def test_small_group_falls_back_to_pooled_z_score(self):
        # Two sector ETFs and eight theme ETFs -> sector group is below the threshold, so it pools.
        sectors = [self._snapshot("XLK", "sector", 2.0, 0.0), self._snapshot("XLE", "sector", 1.0, 0.0)]
        themes = [self._snapshot(f"T{i}", "theme", float(i + 2), 0.0) for i in range(8)]
        all_snaps = sectors + themes
        normalize_z_scores(all_snaps)

        # Compute what the pooled z-score would be for the sector ETFs.
        pooled_z = zscore_normalize([s.rs_ratio for s in all_snaps])
        assert sectors[0].rs_ratio_z == pooled_z[0]
        assert sectors[1].rs_ratio_z == pooled_z[1]

    def test_group_z_threshold_is_per_dimension(self):
        # A group with 8 finite ratios but only 2 finite momentums gets group z for ratio, pooled for momentum.
        sectors = [self._snapshot(f"X{i}", "sector", float(i), None) for i in range(1, 9)]
        sectors[0].rs_momentum = 1.0
        sectors[1].rs_momentum = -1.0
        normalize_z_scores(sectors)

        # Ratio: group z (8 finite values).
        assert sectors[-1].rs_ratio_z is not None
        assert sectors[-1].rs_ratio_z > 1.0

        # Momentum: the sector group has only 2 finite values, so it uses the pooled cross-section, not the
        # group's ratio-sized basis. Pin the pooled values exactly and the six non-finite gaps as no-z.
        pooled = zscore_normalize([1.0, -1.0])
        assert [sectors[0].rs_momentum_z, sectors[1].rs_momentum_z] == pooled
        assert pooled[0] > 0 > pooled[1]
        assert all(s.rs_momentum_z is None for s in sectors[2:])
