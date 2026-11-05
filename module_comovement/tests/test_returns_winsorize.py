"""What ``returns.winsorize_mad`` does, pinned before it was vectorised (#434 follow-up).

The clipping itself is not in question -- Pearson on log returns is dominated by a handful of days,
so one +80% print would detach a name from its own group (review 4b). What this file pins is the
exact behaviour, because the implementation changed from a per-column Python loop to one pass over
the frame and the numbers have to come out identical: measured 36.7 s of a 97 s run on homesrv,
5 Nov 2026, over ~5 100 names.

The traps a vectorised form can fall into are each a test here: a column with no spread must be
left alone rather than clipped to its own median, a NaN bound must not propagate into the data, and
the caller's frame must not be mutated.
"""
import numpy as np
import pandas as pd

from RevenueChaser.module_comovement import returns

K = 5.0


def _loop_winsorize(ret: pd.DataFrame, k: float) -> pd.DataFrame:
    """The per-column loop this replaced, kept as the oracle for the equality test below."""
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


def test_a_column_is_clipped_to_its_median_plus_minus_k_scaled_mad():
    x = pd.Series([0.0, 0.01, -0.01, 0.02, -0.02, 0.80])     # one wild print
    got = returns.winsorize_mad(pd.DataFrame({"A": x}), K)["A"]
    med = x.median()
    half = K * 1.4826 * float((x - med).abs().median())
    assert got.max() <= med + half + 1e-12, "the +80% print is pulled in"
    assert got.iloc[:5].tolist() == x.iloc[:5].tolist(), "the ordinary days are untouched"


def test_a_column_with_no_spread_is_left_alone_not_clipped_to_its_median():
    """MAD 0 means there is nothing to clip against. Bounds of median +- 0 would flatten the column
    to a single value, which is the mistake a vectorised form makes if it does not exclude these."""
    flat = pd.DataFrame({"FLAT": [0.02] * 6, "ONE": [np.nan] * 5 + [0.5]})
    got = returns.winsorize_mad(flat, K)
    assert got["FLAT"].tolist() == [0.02] * 6
    assert got["ONE"].dropna().tolist() == [0.5], "a single observation has no spread either"


def test_an_all_nan_column_survives_as_all_nan():
    got = returns.winsorize_mad(pd.DataFrame({"GONE": [np.nan] * 4}), K)
    assert got["GONE"].isna().all(), "a NaN bound must not become a number"


def test_nan_days_stay_nan_and_do_not_move_the_bounds():
    x = pd.Series([0.0, np.nan, 0.01, -0.01, np.nan, 0.60])
    got = returns.winsorize_mad(pd.DataFrame({"A": x}), K)["A"]
    assert got.isna().tolist() == x.isna().tolist(), "the holes stay where they were"
    assert got.iloc[-1] < 0.60, "and the wild print is still clipped"


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
    frame["FLAT"] = pd.Series([0.01] * n)
    frame["ALLNAN"] = pd.Series([np.nan] * n)
    frame["HOLES"] = pd.Series(np.where(rng.random(n) < 0.3, np.nan, rng.normal(0, 0.03, n)))
    frame["ONEOBS"] = pd.Series([np.nan] * (n - 1) + [0.4])
    frame["NEGMED"] = pd.Series(rng.normal(-0.05, 0.01, n))
    ret = pd.DataFrame(frame)
    pd.testing.assert_frame_equal(returns.winsorize_mad(ret, K), _loop_winsorize(ret, K))
