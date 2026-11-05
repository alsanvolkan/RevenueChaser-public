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


# Not min_periods=2: with uncentred sums the cancellation grows as the overlap shrinks (measured up
# to 2.3e-08 at an overlap of two rows), so pinning TOL=1e-12 there would be a seed away from a red
# CI the diff cannot explain. Production floors min_periods at 15/20/60 (settings.effective).
@pytest.mark.parametrize("min_periods", [10, 60])
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
    # Symmetric to rounding, not exactly: the two triangles come from separate dgemm calls with
    # different accumulation orders (measured on the live frame: 28 946 cells differ, by at most
    # 1.55e-15). Harmless, because every reader takes the upper triangle -- squareform(checks=False),
    # cluster.mean_intra_corr and noise._null_samples all do.
    assert np.allclose(g, g.T, equal_nan=True, atol=1e-12), "symmetric to rounding"
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

def test_a_constant_column_is_flat_at_a_row_count_where_the_floor_is_load_bearing():
    """The row count is the test, and it has to be measured through this function rather than
    reasoned about: ``sum(x^2)/n - mean^2`` leaves a residue whose SIGN depends on the row count and
    on the order the matmuls accumulate. With the floor removed, this pair comes out NaN anyway at
    60 and 200 rows -- the residue happens to land negative, so ``var > 0`` rejects it and the test
    would pass either way -- and comes out as a number at 8 rows (4.2e-09) and 252 (1.8e-09). 252 is
    a window's length, so that is the one to pin: this fails if the floor goes."""
    rng = np.random.default_rng(1)
    ret = pd.DataFrame({"MOVES": rng.normal(0, 0.02, 252), "FLAT": [0.01] * 252})
    got = returns.correlation(ret, 2)
    assert np.isnan(got.loc["MOVES", "FLAT"]), "a constant column correlates with nothing"
    assert np.isnan(ret.corr(min_periods=2).loc["MOVES", "FLAT"]), "as pandas also says"


def test_a_tiny_but_real_variance_is_not_mistaken_for_flat():
    """The floor must not swallow a real signal. Log returns sit near zero, so even a very quiet
    name has a variance of the same order as its mean square and stays far above the floor."""
    rng = np.random.default_rng(11)
    f = pd.DataFrame({"QUIET": rng.normal(0, 1e-8, 80), "OTHER": rng.normal(0, 0.02, 80)})
    assert not np.isnan(returns.correlation(f, 2).loc["QUIET", "OTHER"])
    _assert_matches_pandas(f, 2)


def test_the_floors_known_limit_is_a_column_whose_mean_dwarfs_its_spread():
    """The documented cost of a relative floor, pinned rather than defended: a series centred far
    from zero with a spread more than ~1e6 times smaller reads as flat, where pandas still returns
    a number. Returns are never shaped like this -- price levels are, and ``log_returns`` is what
    feeds this function.

    Centring each column first would remove this, and was tried: it breaks
    ``test_a_store_only_run_matches_the_full_read`` (#379 COM-3), because the column mean differs
    between a windowed read and a full one and the two runs must agree byte for byte. See #469."""
    rng = np.random.default_rng(12)
    f = pd.DataFrame({"LEVEL": 100.0 + rng.normal(0, 1e-6, 60), "OTHER": rng.normal(0, 0.02, 60)})
    assert np.isnan(returns.correlation(f, 2).loc["LEVEL", "OTHER"]), "the floor treats it as flat"
    assert not np.isnan(f.corr(min_periods=2).loc["LEVEL", "OTHER"]), "pandas does not -- the limit"


def test_no_cell_escapes_minus_one_to_one_and_a_name_correlates_with_itself_exactly():
    """The clip is load-bearing, and this is the frame that proves it: two exactly proportional
    columns over 252 rows give an OFF-DIAGONAL r of 1.0000000000000013 without it. A seed scan found
    A scan of 400 seeds x 3 row counts x 4 ratios found 1 655 frames that do; this seed is one of
    them, at 1.0000000000000013. ``distance_matrix`` takes ``sqrt(2(1-r))``, so an off-diagonal r above 1 is a
    NaN distance, and ``linkage`` raises ValueError on a non-finite condensed matrix -- the board
    dies rather than wobbles. On the diagonal it is harmless, because that function zeroes it."""
    rng = np.random.default_rng(0)
    base = rng.normal(0, 0.02, 252)
    ret = pd.DataFrame({"A": base, "PROP": base * 3.0, "C": rng.normal(0, 0.02, 252),
                        "FEW": pd.Series([np.nan] * 250 + [0.1, -0.1])})
    got = returns.correlation(ret, 4).to_numpy()
    seen = got[~np.isnan(got)]
    assert seen.max() <= 1.0 and seen.min() >= -1.0, f"out of range: {seen.max()!r}"
    off = got.copy()
    np.fill_diagonal(off, 0.0)
    assert np.nanmax(off) <= 1.0, "an off-diagonal cell above 1 is a NaN distance and kills linkage"
    diag = np.diag(got)
    assert (diag[~np.isnan(diag)] == 1.0).all(), "exactly 1.0, not 1 - 2e-16"
    assert np.isnan(diag[list(ret.columns).index("FEW")]), "unless the pair is not a number at all"
