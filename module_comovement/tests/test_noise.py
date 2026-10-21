"""Noise controls — shuffled-returns null and factors above noise."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from RevenueChaser.module_comovement import cluster, noise


def _noise_returns(n=120, t=250, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame(rng.normal(0, 0.01, size=(t, n)),
                        columns=[f"N{i:03d}" for i in range(n)])


def _cluster_fn(ret, cuts):
    return cluster.clusters(ret, cuts, method="ward")


def test_grid_point_is_the_largest_grid_value_at_or_below_the_size():
    assert noise.grid_point(1) is None
    assert noise.grid_point(2) == 2
    assert noise.grid_point(7) == 6
    assert noise.grid_point(10) == 10
    assert noise.grid_point(199) == 160
    assert noise.grid_point(5000) == 200


def test_groups_clustered_from_pure_noise_mostly_fail_the_null():
    # The review's regression: with the old random-names baseline, 100% of the
    # groups the pipeline selected from pure noise "passed". Against the
    # selected-group null they must pass about as often as q allows.
    cuts = (4, 12)
    null = noise.selected_group_null(_noise_returns(seed=1), cuts, _cluster_fn,
                                     n_perm=5, seed=0)
    real = _noise_returns(seed=2)
    _Z, _labels, corr, groups = _cluster_fn(real, cuts)
    judged = passed = 0
    for per in groups.values():
        for mem in per.values():
            base = noise.baseline_for(null, len(mem))
            if base is None:
                continue
            judged += 1
            passed += cluster.mean_intra_corr(corr, mem) > base
    assert judged >= 12
    assert passed / judged <= 0.15


def test_a_real_block_beats_the_null():
    ret = _noise_returns(seed=3)
    rng = np.random.default_rng(4)
    f = rng.normal(0, 0.01, len(ret))
    block = [f"N{i:03d}" for i in range(10)]
    for c in block:
        ret[c] = f + rng.normal(0, 0.006, len(ret))
    null = noise.selected_group_null(ret, (8,), _cluster_fn, n_perm=5, seed=0)
    _Z, _labels, corr, groups = _cluster_fn(ret, (8,))
    holder = next(m for m in groups[8].values() if set(block) <= set(m))
    assert cluster.mean_intra_corr(corr, holder) > noise.baseline_for(null, len(holder))


def test_selected_group_null_is_reproducible_and_empty_when_nothing_clusters():
    ret = _noise_returns(n=40, seed=5)
    a = noise.selected_group_null(ret, (4,), _cluster_fn, n_perm=2, seed=7)
    b = noise.selected_group_null(ret, (4,), _cluster_fn, n_perm=2, seed=7)
    assert a == b and a
    assert noise.selected_group_null(ret.iloc[:, :2], (2,), _cluster_fn) == {}
    assert noise.selected_group_null(None, (2,), _cluster_fn) == {}


def test_the_shuffle_keeps_each_column_and_breaks_the_links():
    rng = np.random.default_rng(6)
    f = rng.normal(size=300)
    ret = pd.DataFrame({"A": f, "B": f + 0.01 * rng.normal(size=300)})
    ret.loc[:9, "A"] = np.nan
    sh = noise._shuffle_columns(ret, np.random.default_rng(0))
    assert sorted(sh["B"]) == pytest.approx(sorted(ret["B"]))
    assert sh["A"].isna().sum() == 10
    assert abs(sh["A"].corr(sh["B"])) < 0.2


def test_baseline_for_reads_the_grid_and_errs_strict():
    null = {5: 0.30, 10: 0.20, 40: 0.10}
    assert noise.baseline_for(null, 12) == 0.20        # grid 10 -> the 10 point
    assert noise.baseline_for(null, 40) == 0.10
    assert noise.baseline_for(null, 500) == 0.10       # capped at 200 -> 40 point
    assert noise.baseline_for(null, 3) == 0.30         # below every point: strictest
    assert noise.baseline_for(null, 1) is None         # one name cannot co-move
    assert noise.baseline_for({}, 10) is None          # no null: not judged


def test_factors_above_noise_counts_real_common_moves():
    rng = np.random.default_rng(0)
    f = rng.normal(size=(250, 2))
    one = pd.DataFrame(f[:, :1] + 0.5 * rng.normal(size=(250, 60)))
    two = pd.DataFrame(np.hstack([f[:, :1] + 0.5 * rng.normal(size=(250, 30)),
                                  f[:, 1:] + 0.5 * rng.normal(size=(250, 30))]))
    ind = pd.DataFrame(rng.normal(size=(250, 60)))
    assert noise.factors_above_noise(one) == 1
    assert noise.factors_above_noise(two) == 2
    assert noise.factors_above_noise(ind) == 0         # pure noise: nothing real


def test_factors_above_noise_drops_columns_below_95pc_coverage():
    rng = np.random.default_rng(1)
    f = rng.normal(size=(250, 1))
    full = pd.DataFrame(f + 0.5 * rng.normal(size=(250, 20)))
    thin = full.copy()
    thin["THIN"] = rng.normal(size=250)
    thin.loc[thin.index[:25], "THIN"] = np.nan        # 90% coverage
    assert noise.factors_above_noise(thin) == noise.factors_above_noise(full) == 1


def test_factors_above_noise_none_when_too_few_rows_or_columns_remain():
    rng = np.random.default_rng(2)
    ret = pd.DataFrame(rng.normal(size=(250, 4)))
    assert noise.factors_above_noise(ret.iloc[:2]) is None          # 2 rows
    assert noise.factors_above_noise(ret.iloc[:, :2]) is None       # 2 columns
    assert noise.factors_above_noise(None) is None
