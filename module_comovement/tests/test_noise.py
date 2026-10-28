"""Noise controls — shuffled-returns null and factors above noise."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from RevenueChaser.module_comovement import cluster, noise
from RevenueChaser.module_comovement import returns


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


# ── h > 1: overlapping returns (Fable review of Co-movement, 28 Oct 2026, S1) ─
# Overlapping h-session returns are serially correlated, so a pair's sample r spreads about sqrt(1 + (h-1)(2h-1)/3h)
# times wider than at 1 day. Shuffling those returns row by row broke the overlap: the null was 1-day tight, and on
# pure noise every 10-day group "passed"; the Marchenko-Pastur edge read 250 overlapping rows as 250 independent ones.
def _noise_prices(n=120, t=250, h=10, seed=0, factor=0.0):
    rng = np.random.default_rng(seed)
    d = rng.normal(0, 0.01, size=(t + h, n)) + factor * rng.normal(0, 0.01, size=(t + h, 1))
    idx = pd.date_range("2025-01-01", periods=t + h, freq="B")
    return pd.DataFrame(100 * np.exp(d.cumsum(axis=0)), index=idx, columns=[f"N{i:03d}" for i in range(n)])


def _overlapping(prices, h, winsor=True):
    ret, _ = returns.log_returns(prices, window=len(prices) - h, min_obs=10, horizon=h)
    return returns.winsorize_mad(ret, 5.0) if winsor else ret


def _passed_share(real, null, cuts):
    _Z, _labels, corr, groups = _cluster_fn(real, cuts)
    judged = passed = 0
    for per in groups.values():
        for mem in per.values():
            base, r = noise.baseline_for(null, len(mem)), cluster.mean_intra_corr(corr, mem)
            if base is not None and r is not None:
                judged += 1
                passed += r > base
    return passed / judged


def test_at_ten_day_returns_noise_groups_mostly_fail_the_null():
    prices, cuts = _noise_prices(seed=4), (4, 12)
    real = _overlapping(prices, 10)
    row_shuffle = noise.selected_group_null(real, cuts, _cluster_fn, n_perm=5)
    assert _passed_share(real, row_shuffle, cuts) > 0.5          # the old null, kept visible
    daily = np.log(prices / prices.shift(1)).iloc[1:]
    null = noise.selected_group_null(real, cuts, _cluster_fn, n_perm=5, horizon=10, daily=daily, winsor_k=5.0)
    assert _passed_share(real, null, cuts) <= 0.15


def test_at_one_day_the_null_is_unchanged():
    ret = _noise_returns(seed=1)
    daily = pd.DataFrame(np.zeros((len(ret), ret.shape[1])), columns=ret.columns)
    assert noise.selected_group_null(ret, (4, 12), _cluster_fn, n_perm=2, horizon=1, daily=daily, winsor_k=5.0) \
        == noise.selected_group_null(ret, (4, 12), _cluster_fn, n_perm=2)


def test_without_the_daily_returns_a_longer_horizon_is_not_judged():
    real = _overlapping(_noise_prices(seed=4), 10)
    assert noise.selected_group_null(real, (4, 12), _cluster_fn, horizon=10) == {}
    short = np.zeros((len(real), real.shape[1]))                  # not ``horizon - 1`` rows more than ``ret``
    assert noise.selected_group_null(real, (4, 12), _cluster_fn, horizon=10, daily=pd.DataFrame(short)) == {}


def test_at_ten_day_returns_pure_noise_shows_no_common_move():
    ret = _overlapping(_noise_prices(n=200, seed=5), 10, winsor=False)
    assert noise.factors_above_noise(ret) >= 5                    # read as 250 independent rows
    assert noise.factors_above_noise(ret, horizon=10) == 0


def test_at_five_day_returns_a_real_common_move_still_counts():
    ret = _overlapping(_noise_prices(n=200, h=5, seed=6, factor=2.0), 5, winsor=False)
    assert noise.factors_above_noise(ret, horizon=5) == 1


def test_a_fetch_at_five_days_gives_the_null_the_daily_returns_under_its_matrix(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_comovement import engine
    from RevenueChaser.module_comovement.tests.test_settings import _market_run

    seen = {}
    real_null, real_factors = noise.selected_group_null, noise.factors_above_noise

    def null_spy(ret, cuts, fn, **kw):
        seen.update(kw, rows=len(ret), cols=list(ret.columns))
        return real_null(ret, cuts, fn, **kw)

    def factors_spy(ret, horizon=1):
        seen["factors_horizon"] = horizon
        return real_factors(ret, horizon=horizon)

    monkeypatch.setattr(engine.noise, "selected_group_null", null_spy)
    monkeypatch.setattr(engine.noise, "factors_above_noise", factors_spy)
    _t, kw = _market_run(persist=True)
    p = engine.run(horizon=5, **kw)
    assert seen["horizon"] == 5 and seen["factors_horizon"] == 5 and seen["winsor_k"] == 5.0
    assert seen["daily"].shape == (seen["rows"] + 4, len(seen["cols"]))
    assert list(seen["daily"].columns) == seen["cols"]
    assert any(c["baseline_r"] is not None for items in p["clusters"].values() for c in items)
