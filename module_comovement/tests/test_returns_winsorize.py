"""The behaviour ``returns.winsorize_mad`` must keep now that it winsorises the whole frame in one
pass instead of looping per name (#459).

What the clipping is for is tested next door (``test_comovement.py``: the spike is clipped, the body
is kept, a flat column is left alone, NaNs stay NaNs, and a correlation broken by one print comes
back). This file pins only what the rewrite could break, so it holds the two tests that no other
test can fail on:

- **MAD 0 on a column that is not constant.** A halted name prints flat for most of the window and
  then gaps. Its median and its MAD are both 0, so bounds of ``median ± 0`` would clip that one real
  move away. The ``spread`` mask is the only thing stopping that, and a *constant* column cannot
  show it: clipping a constant to ``[med, med]`` is a no-op either way. Removing the mask leaves
  every other winsorize test in this package green.
- **Equality with the loop** on a frame holding every hard case at once.
"""
import numpy as np
import pandas as pd
import pytest

from RevenueChaser.module_comovement import returns

K = 5.0


def _loop_winsorize(ret: pd.DataFrame, k: float) -> pd.DataFrame:
    """The per-column loop this replaced, kept as the oracle."""
    out = ret.copy()
    for col in out.columns:
        x = out[col]
        med = x.median()
        mad = (x - med).abs().median()
        if not np.isfinite(mad) or float(mad) == 0.0:
            continue
        half = k * 1.4826 * float(mad)
        out[col] = x.clip(med - half, med + half)
    return out


@pytest.mark.parametrize("name,values", [
    # The one that matters: flat for most of the window, then one real move. median 0, MAD 0.
    ("halted_then_gapped", [0.0, 0.0, 0.0, 0.0, 0.0, 0.30]),
    ("mostly_flat_both_ways", [0.0, 0.0, 0.0, 0.0, -0.25, 0.30]),
    ("constant", [0.02] * 6),
    ("one_observation", [np.nan] * 5 + [0.5]),
    ("all_nan", [np.nan] * 6),
])
def test_a_column_with_no_spread_is_left_alone_whether_or_not_it_is_constant(name, values):
    """MAD 0 means there is nothing to clip against, so the column is handed back as it came."""
    got = returns.winsorize_mad(pd.DataFrame({name: values}), K)[name]
    want = pd.Series(values, name=name)
    assert (got.isna() == want.isna()).all(), "the holes stay where they were"
    assert got.dropna().tolist() == want.dropna().tolist(), "and no value is moved"


def test_the_callers_frame_is_not_mutated():
    ret = pd.DataFrame({"A": [0.0, 0.01, -0.01, 0.90]})
    before = ret.copy()
    returns.winsorize_mad(ret, K)
    pd.testing.assert_frame_equal(ret, before)


def test_the_index_the_columns_and_their_order_are_kept():
    idx = pd.to_datetime(["2026-10-26", "2026-10-27", "2026-10-28"])
    ret = pd.DataFrame({"B": [0.1, 0.0, -0.1], "A": [0.2, 0.0, -0.2]}, index=idx)
    got = returns.winsorize_mad(ret, K)
    assert list(got.columns) == ["B", "A"] and got.index.equals(idx)


def test_an_empty_or_missing_frame_is_handed_back():
    assert returns.winsorize_mad(pd.DataFrame(), K).empty
    assert returns.winsorize_mad(None, K).empty


def test_it_matches_the_per_column_loop_it_replaced_on_a_frame_of_every_hard_case():
    """The equality that makes the rewrite safe: same numbers, same NaNs, column by column."""
    rng = np.random.default_rng(434)
    n = 120
    frame = {f"N{i}": pd.Series(rng.normal(0, 0.02, n)) for i in range(40)}
    frame["WILD"] = pd.Series(np.r_[rng.normal(0, 0.02, n - 1), 0.9])
    frame["CONSTANT"] = pd.Series([0.01] * n)
    # MAD 0 but NOT constant: the case the mask exists for.
    frame["HALTED"] = pd.Series([0.0] * (n - 1) + [0.30])
    frame["HALTED_BOTH"] = pd.Series([0.0] * (n - 2) + [-0.25, 0.30])
    frame["ALLNAN"] = pd.Series([np.nan] * n)
    frame["HOLES"] = pd.Series(np.where(rng.random(n) < 0.3, np.nan, rng.normal(0, 0.03, n)))
    frame["ONEOBS"] = pd.Series([np.nan] * (n - 1) + [0.4])
    frame["NEGMED"] = pd.Series(rng.normal(-0.05, 0.01, n))
    ret = pd.DataFrame(frame)
    pd.testing.assert_frame_equal(returns.winsorize_mad(ret, K), _loop_winsorize(ret, K))
