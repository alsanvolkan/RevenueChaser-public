"""``returns.correlation`` is five matrix products per block of columns, not ``DataFrame.corr``
walking the pairs (#459). Same answer, 18.56 s -> 3.14 s over 5 117 names.

It is NOT bit-identical -- a different summation order moves the last bits -- so pandas is the
oracle here and the tolerance is explicit. What these tests are really for:

- the **blocking**, which is the part with somewhere to go wrong. A frame wider than the block, with
  the block forced small, is the only way a mis-stitched ``out[lo:hi, :]`` shows up; on a 40-column
  frame with the production block of 512 it never would.
- **pairwise-complete** means and variances: each pair is measured on the rows where both columns
  are finite, so a column's own holes must not move another pair's answer.
- the states that are not a number: fewer jointly finite rows than ``min_periods``, and no spread.
"""
import numpy as np
import pandas as pd
import pytest

from RevenueChaser.module_comovement import returns

TOL = 1e-12


def _hard_frame(rows: int = 90, names: int = 37, seed: int = 459) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    base = rng.normal(0, 0.02, rows)
    frame = {}
    for i in range(names):
        # correlated with the base to a varying degree, so the matrix is not just noise
        frame[f"N{i}"] = pd.Series(0.3 * base + rng.normal(0, 0.02, rows) * (1 + i % 3))
    frame["HOLES"] = pd.Series(np.where(rng.random(rows) < 0.4, np.nan, rng.normal(0, 0.03, rows)))
    frame["FEWOBS"] = pd.Series([np.nan] * (rows - 4) + list(rng.normal(0, 0.02, 4)))
    frame["FLAT"] = pd.Series([0.01] * rows)
    frame["ALLNAN"] = pd.Series([np.nan] * rows)
    frame["ANTI"] = pd.Series(-base)
    frame["SAME"] = pd.Series(base)
    return pd.DataFrame(frame)


def _assert_matches_pandas(ret: pd.DataFrame, min_periods: int) -> None:
    got = returns.correlation(ret, min_periods)
    want = ret.corr(min_periods=min_periods)
    assert list(got.columns) == list(want.columns) and list(got.index) == list(want.index)
    g, w = got.to_numpy(), want.to_numpy()
    assert (np.isnan(g) == np.isnan(w)).all(), "the same cells are not a number"
    both = ~np.isnan(g)
    assert np.abs(g[both] - w[both]).max() < TOL


@pytest.mark.parametrize("min_periods", [2, 10, 60])
def test_it_matches_pandas_corr_on_a_frame_of_every_hard_case(min_periods):
    _assert_matches_pandas(_hard_frame(), min_periods)


@pytest.mark.parametrize("block", [1, 2, 7, 13])
def test_the_blocks_are_stitched_in_the_right_place(block, monkeypatch):
    """The production block is 512, so no realistic test frame crosses it. Forced small, a block
    written to the wrong rows of the answer shows up at once -- and the matrix stops being
    symmetric, which is the cheapest statement of "the stitching is right"."""
    monkeypatch.setattr(returns, "_CORR_BLOCK", block)
    ret = _hard_frame(rows=40, names=11)
    got = returns.correlation(ret, 2)
    _assert_matches_pandas(ret, 2)
    g = got.to_numpy()
    assert np.allclose(g, g.T, equal_nan=True), "a correlation matrix is symmetric"
    assert np.allclose(np.diag(g)[~np.isnan(np.diag(g))], 1.0), "a name correlates with itself"


def test_a_pair_with_fewer_jointly_finite_rows_than_min_periods_is_not_a_number():
    a = pd.Series([0.1, 0.2, -0.1, 0.3, -0.2, 0.15])
    # B overlaps A on exactly three rows; C overlaps on all six.
    b = pd.Series([0.1, np.nan, np.nan, 0.25, -0.15, np.nan])
    c = pd.Series([0.2, 0.1, -0.2, 0.1, -0.1, 0.05])
    got = returns.correlation(pd.DataFrame({"A": a, "B": b, "C": c}), 4)
    assert np.isnan(got.loc["A", "B"]), "three jointly finite rows is fewer than four"
    assert not np.isnan(got.loc["A", "C"]), "six is enough"
    assert np.isnan(got.loc["B", "B"]), "and a column is judged against itself by the same rule"


def test_a_column_with_no_spread_or_no_observations_is_not_a_number():
    ret = pd.DataFrame({"MOVES": [0.1, -0.1, 0.2, -0.2],
                        "FLAT": [0.01] * 4,
                        "ALLNAN": [np.nan] * 4})
    got = returns.correlation(ret, 2)
    assert np.isnan(got.loc["MOVES", "FLAT"]), "no spread, no correlation"
    assert np.isnan(got.loc["FLAT", "FLAT"])
    assert np.isnan(got.loc["MOVES", "ALLNAN"])
    assert got.loc["MOVES", "MOVES"] == pytest.approx(1.0)


def test_a_holed_column_does_not_move_another_pairs_answer():
    """Pairwise-complete: each pair is measured on its own jointly finite rows. If the means came
    from the whole column instead, adding holes to a third name would move A-B."""
    rng = np.random.default_rng(7)
    a = pd.Series(rng.normal(0, 0.02, 60))
    b = pd.Series(0.5 * a + rng.normal(0, 0.02, 60))
    plain = returns.correlation(pd.DataFrame({"A": a, "B": b}), 2).loc["A", "B"]
    holed = pd.Series(np.where(rng.random(60) < 0.5, np.nan, rng.normal(0, 0.05, 60)))
    with_third = returns.correlation(pd.DataFrame({"A": a, "B": b, "H": holed}), 2).loc["A", "B"]
    assert plain == pytest.approx(with_third, abs=TOL)


def test_the_perfect_cases_come_out_at_one_and_minus_one():
    base = pd.Series([0.1, -0.05, 0.2, -0.1, 0.15, -0.2])
    got = returns.correlation(pd.DataFrame({"A": base, "SAME": base * 3, "ANTI": -base}), 2)
    assert got.loc["A", "SAME"] == pytest.approx(1.0)
    assert got.loc["A", "ANTI"] == pytest.approx(-1.0)


def test_an_empty_or_missing_frame_is_an_empty_frame():
    assert returns.correlation(pd.DataFrame(), 2).empty
    assert returns.correlation(None, 2).empty


def test_one_column_is_its_own_diagonal():
    got = returns.correlation(pd.DataFrame({"A": [0.1, -0.1, 0.2]}), 2)
    assert got.shape == (1, 1) and got.iloc[0, 0] == pytest.approx(1.0)


# ── the variance floor, both ways (see returns._VAR_FLOOR) ──

def test_a_constant_column_is_flat_even_though_the_arithmetic_says_otherwise():
    """``sum(x^2)/n - mean^2`` on a constant column leaves a positive residue of about 1e-16 of the
    mean square, so a bare ``var > 0`` let a meaningless r through: measured -2.9e-09 against a
    neighbour where pandas gives NaN. The floor is relative to the mean square, which is what makes
    that residue recognisable as noise."""
    base = pd.Series([0.1, -0.05, 0.2, -0.1, 0.15, -0.2])
    ret = pd.DataFrame({"MOVES": base, "FLAT": pd.Series([0.01] * 6)})
    got = returns.correlation(ret, 2)
    assert np.isnan(got.loc["MOVES", "FLAT"]), "a constant column correlates with nothing"
    want = ret.corr(min_periods=2)
    assert np.isnan(want.loc["MOVES", "FLAT"]), "which is what pandas says too"


def test_a_tiny_but_real_variance_is_not_mistaken_for_flat():
    """The floor must not swallow a real signal. Log returns sit near zero, so even a very quiet
    name has a variance of the same order as its mean square and stays far above the floor."""
    rng = np.random.default_rng(11)
    quiet = pd.Series(rng.normal(0, 1e-8, 80))           # sd 1e-8, mean ~0
    other = pd.Series(rng.normal(0, 0.02, 80))
    got = returns.correlation(pd.DataFrame({"QUIET": quiet, "OTHER": other}), 2)
    assert not np.isnan(got.loc["QUIET", "OTHER"]), "a quiet name still has a correlation"
    _assert_matches_pandas(pd.DataFrame({"QUIET": quiet, "OTHER": other}), 2)


def test_the_floors_known_limit_is_a_column_whose_mean_dwarfs_its_spread():
    """The documented cost of a relative floor: a series centred far from zero with a spread more
    than ~1e6 times smaller reads as flat, where pandas still returns a number. Returns are never
    shaped like this -- prices are -- so this pins the limit rather than defending it."""
    rng = np.random.default_rng(12)
    level = pd.Series(100.0 + rng.normal(0, 1e-6, 60))   # mean 100, sd 1e-6
    other = pd.Series(rng.normal(0, 0.02, 60))
    got = returns.correlation(pd.DataFrame({"LEVEL": level, "OTHER": other}), 2)
    want = pd.DataFrame({"LEVEL": level, "OTHER": other}).corr(min_periods=2)
    assert np.isnan(got.loc["LEVEL", "OTHER"]), "the floor treats it as flat"
    assert not np.isnan(want.loc["LEVEL", "OTHER"]), "pandas does not -- this is the known limit"


def test_no_cell_escapes_minus_one_to_one_and_a_name_correlates_with_itself_exactly():
    """Rounding let 823 cells of the live matrix out above 1 (at most 1 + 4.4e-16) and left 1 616
    diagonals off 1.0. ``distance_matrix`` takes ``sqrt(2(1-r))``, so r above 1 is a NaN distance --
    harmless on the diagonal, which it zeroes, and not harmless anywhere else."""
    base = pd.Series([0.1, -0.05, 0.2, -0.1, 0.15, -0.2, 0.07, -0.03])
    ret = pd.DataFrame({"A": base, "SAME": base * 3.0, "ANTI": -base * 2.0, "NEAR": base + 1e-9,
                        "FEW": pd.Series([np.nan] * 6 + [0.1, -0.1])})
    got = returns.correlation(ret, 4).to_numpy()
    seen = got[~np.isnan(got)]
    assert seen.max() <= 1.0 and seen.min() >= -1.0, f"out of range: {seen.max()!r} {seen.min()!r}"
    diag = np.diag(got)
    assert (diag[~np.isnan(diag)] == 1.0).all(), "exactly 1.0, not 1 - 2e-16"
    assert np.isnan(diag[list(ret.columns).index("FEW")]), "unless the pair is not a number at all"
