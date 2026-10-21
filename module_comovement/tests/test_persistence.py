"""Out-of-sample persistence — lagged formation, held-out test, matching."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from RevenueChaser.module_comovement import cluster, config, persistence


def _fn(ret, cuts):
    return cluster.clusters(ret, cuts, method="ward")


def _prices(n_rows=400, n_noise=30, block=8, block_until=None, seed=0):
    """Close prices: ``block`` names share one factor, the rest are noise.

    ``block_until`` ends the shared factor after that row (the block breaks
    up); ``None`` keeps it for the whole history.
    """
    rng = np.random.default_rng(seed)
    f = rng.normal(0, 0.01, n_rows)
    data = {}
    for i in range(block):
        shared = f.copy()
        if block_until is not None:
            shared[block_until:] = rng.normal(0, 0.01, n_rows - block_until)
        data[f"B{i}"] = shared + rng.normal(0, 0.002, n_rows)
    for i in range(n_noise):
        data[f"N{i:02d}"] = rng.normal(0, 0.01, n_rows)
    ret = pd.DataFrame(data, index=pd.date_range("2025-01-01", periods=n_rows, freq="B"))
    return 100 * np.exp(ret.cumsum())


BLOCK = {f"B{i}" for i in range(8)}


def _block_row(lag, cut):
    return next(g for g in lag[cut] if BLOCK <= set(g["members"]))


def test_a_lasting_block_holds_out_of_sample():
    lag = persistence.lagged_test(_prices(), (4,), 1, None, _fn)
    g = _block_row(lag, 4)
    assert g["held"] is True
    assert g["oos_r"] > g["oos_base"]


def test_a_block_that_broke_up_in_the_last_quarter_does_not_hold():
    # Together for the formation window, independent over the held-out quarter.
    lag = persistence.lagged_test(_prices(block_until=400 - config.OOS_HOLDOUT),
                                  (4,), 1, None, _fn)
    g = _block_row(lag, 4)
    assert g["held"] is False


def test_noise_groups_mostly_do_not_hold():
    lag = persistence.lagged_test(_prices(n_noise=60, block=0, seed=3), (6,), 1, None, _fn)
    judged = [g for g in lag[6] if g["held"] is not None]
    assert judged and sum(g["held"] for g in judged) <= 1


def test_the_lagged_window_never_sees_the_held_out_quarter(monkeypatch):
    seen = {}

    def spy(ret, cuts):
        seen["last"] = ret.index[-1] if hasattr(ret.index, "max") else None
        seen["rows"] = len(ret)
        return _fn(ret, cuts)

    prices = _prices()
    persistence.lagged_test(prices, (4,), 1, None, spy)
    assert seen["rows"] <= config.WINDOW
    # ``log_returns`` keeps the date index: the formation window ends exactly
    # OOS_HOLDOUT sessions before the last price.
    assert seen["last"] == prices.index[-1 - config.OOS_HOLDOUT]


def test_benchmark_column_never_joins_a_group():
    prices = _prices()
    prices["SPY"] = 100 * np.exp(np.random.default_rng(9).normal(0, 0.01, len(prices)).cumsum())
    for apply in (True, False):
        lag = persistence.lagged_test(prices, (4,), 1, "SPY", _fn, apply_bench=apply)
        assert all("SPY" not in g["members"] for g in lag[4])


def test_short_history_has_no_lagged_test():
    short = _prices(n_rows=config.OOS_HOLDOUT + config.MIN_OBS)
    assert persistence.lagged_test(short, (4,), 1, None, _fn) == {}
    assert persistence.lagged_test(pd.DataFrame(), (4,), 1, None, _fn) == {}


def test_match_reads_the_best_overlap():
    lag = [{"lag_id": "1", "members": ["A", "B", "C", "D"], "oos_r": 0.3,
            "oos_base": 0.1, "held": True},
           {"lag_id": "2", "members": ["E", "F"], "oos_r": 0.0,
            "oos_base": 0.1, "held": False}]
    same = persistence.match(["A", "B", "C"], lag)                 # J = 3/4
    assert same == {"jaccard": 0.75, "new": False, "held": True,
                    "oos_r": 0.3, "oos_base": 0.1}
    broke = persistence.match(["E", "F"], lag)
    assert broke["held"] is False and broke["new"] is False
    new = persistence.match(["A", "E", "X", "Y"], lag)            # best J = 1/5 (E, F)
    assert new["new"] is True and new["held"] is None
    assert new["jaccard"] == pytest.approx(1 / 5)
    assert persistence.match(["Z"], lag)["new"] is True           # no overlap
    assert persistence.match(["A"], None) is None                 # no lagged test
    assert persistence.match(["A"], []) is None


def test_summary_counts_only_judged_groups():
    lag = {20: [{"held": True}, {"held": False}, {"held": None}],
           80: [{"held": None}]}
    assert persistence.summary(lag) == {"20": {"held": 1, "total": 2}}
    assert persistence.summary({}) == {}


def test_random_names_baseline_is_reproducible_and_strict():
    rng = np.random.default_rng(0)
    corr = pd.DataFrame(rng.normal(size=(250, 40))).corr()
    a = persistence.random_names_baseline(corr, [2, 7, 10, 500], seed=1)
    assert a == persistence.random_names_baseline(corr, [2, 7, 10, 500], seed=1)
    assert set(a) == {2, 7, 10, 500}
    assert a[500] <= a[10] <= a[2]                     # bar falls as groups grow
    assert persistence.random_names_baseline(corr.iloc[:1, :1], [2]) == {}
