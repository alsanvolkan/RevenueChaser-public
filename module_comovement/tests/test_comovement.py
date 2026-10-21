"""Co-movement core — returns, clustering, labeling, engine (no network)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time

import numpy as np
import pandas as pd
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_comovement.tests import d01_bars
from RevenueChaser.module_comovement import (cluster, config, engine, label, noise,
                                             ohlc, returns)


# ── config ──────────────────────────────────────────────────────────────────
def test_cuts_scale_with_universe_size():
    assert config.cuts_for(3000) == (20, 80, 250)
    assert config.cuts_for(800) == (10, 30, 80)
    assert config.cuts_for(120) == (5, 15, 40)


# ── returns ─────────────────────────────────────────────────────────────────
def test_log_returns_are_log_not_price_differences():
    prices = pd.DataFrame({"A": [100.0, 110.0, 121.0]})
    ret, dropped = returns.log_returns(prices, window=10, min_obs=1)
    assert dropped == []
    assert ret["A"].iloc[0] == pytest.approx(np.log(1.1))


def test_log_returns_horizon_is_overlapping():
    prices = pd.DataFrame({"A": [100.0, 110.0, 121.0]})
    ret, dropped = returns.log_returns(prices, window=10, min_obs=1, horizon=2)
    assert dropped == []
    assert len(ret) == 1
    assert ret["A"].iloc[0] == pytest.approx(np.log(121.0 / 100.0))


def test_short_history_is_dropped():
    prices = pd.DataFrame({
        "LONG": np.linspace(100, 120, 50),
        "SHORT": [100.0, 101.0, 102.0] + [np.nan] * 47,
    })
    ret, dropped = returns.log_returns(prices, window=50, min_obs=10)
    assert "SHORT" in dropped and "LONG" in ret.columns


def test_distance_is_clipped_and_nan_becomes_uncorrelated():
    corr = pd.DataFrame([[1.0, 0.5], [0.5, 1.0]], index=["A", "B"], columns=["A", "B"])
    d = returns.distance_matrix(corr)
    assert d[0, 1] == pytest.approx(np.sqrt(2 * (1 - 0.5)))
    assert d[0, 0] == 0.0 and d[1, 1] == 0.0

    # F9: an unknown correlation is r = 0, not r = 0.5. NaN -> distance 1.0 said
    # "half correlated" about a pair the matrix never measured.
    nan_corr = pd.DataFrame([[1.0, np.nan], [np.nan, 1.0]], index=["A", "B"], columns=["A", "B"])
    d2 = returns.distance_matrix(nan_corr)
    assert d2[0, 1] == pytest.approx(np.sqrt(2))


def _pair_frame(n: int = 120, seed: int = 3) -> pd.DataFrame:
    """Returns with every case ``pair_correlation`` must treat like ``DataFrame.corr``."""
    rng = np.random.default_rng(seed)
    a = rng.normal(0, 0.01, n)
    df = pd.DataFrame({
        "A": a,
        "B": 0.7 * a + rng.normal(0, 0.005, n),
        "C": rng.normal(0, 0.01, n),
        "SHORT": np.where(np.arange(n) < 30, rng.normal(0, 0.01, n), np.nan),
        "FLAT": np.zeros(n),
        "E": np.where(np.arange(n) < 80, 0.0, rng.normal(0, 0.01, n)),
        "F": np.where(np.arange(n) < 80, rng.normal(0, 0.01, n), np.nan),
    })
    df.loc[10:29, "B"] = np.nan        # NaN gaps, in different rows per column
    df.loc[50:69, "C"] = np.nan
    return df


_PAIRS = [
    ("A", "B"), ("B", "A"),                  # a pair and its reverse
    ("A", "C"), ("B", "C"), ("C", "B"),      # gaps in both columns
    ("A", "SHORT"),                          # overlap (30) below min_periods
    ("A", "FLAT"), ("FLAT", "A"),            # a zero-variance column
    ("E", "F"),                              # varies overall, constant where jointly finite
    ("A", "MISSING"), ("MISSING", "A"),      # a column the frame never had
    ("A", "A"),
]


def _matrix_lookup(ret, pairs, mp):
    corr = ret.corr(min_periods=mp)
    return np.array([corr.loc[a, b] if a in corr.columns and b in corr.columns else np.nan
                     for a, b in pairs], dtype=float)


@pytest.mark.parametrize("mp", [1, 20, 60, 100])
def test_pair_correlation_equals_the_matrix_lookup(mp):
    ret = _pair_frame()
    got = returns.pair_correlation(ret, _PAIRS, min_periods=mp)
    want = _matrix_lookup(ret, _PAIRS, mp)
    assert got.shape == (len(_PAIRS),)
    np.testing.assert_array_equal(np.isnan(got), np.isnan(want))   # same entries present
    np.testing.assert_allclose(got, want, rtol=0, atol=1e-12, equal_nan=True)


def test_pair_correlation_marks_the_unavailable_cases_nan():
    ret = _pair_frame()
    got = dict(zip(_PAIRS, returns.pair_correlation(ret, _PAIRS, min_periods=60)))
    assert np.isfinite(got[("A", "B")]) and np.isfinite(got[("A", "C")])
    for bad in [("A", "SHORT"), ("A", "FLAT"), ("FLAT", "A"), ("E", "F"),
                ("A", "MISSING"), ("MISSING", "A")]:
        assert np.isnan(got[bad]), bad
    assert got[("A", "B")] == pytest.approx(got[("B", "A")], abs=1e-15)   # symmetric


def test_pair_correlation_min_periods_is_inclusive():
    ret = _pair_frame()
    overlap = int((ret["B"].notna() & ret["C"].notna()).sum())
    pair = [("B", "C")]
    assert np.isfinite(returns.pair_correlation(ret, pair, min_periods=overlap)[0])
    assert np.isnan(returns.pair_correlation(ret, pair, min_periods=overlap + 1)[0])


def test_pair_correlation_defaults_to_the_correlation_min_periods(rawstore_tmp_db):
    ret = _pair_frame()
    corr = returns.correlation(ret)           # settings default min_periods
    want = np.array([corr.loc[a, b] if a in corr.columns and b in corr.columns else np.nan
                     for a, b in _PAIRS], dtype=float)
    got = returns.pair_correlation(ret, _PAIRS)
    np.testing.assert_array_equal(np.isnan(got), np.isnan(want))
    np.testing.assert_allclose(got, want, rtol=0, atol=1e-12, equal_nan=True)


def test_pair_correlation_is_chunk_independent(monkeypatch):
    ret = _pair_frame()
    whole = returns.pair_correlation(ret, _PAIRS, min_periods=60)
    monkeypatch.setattr(returns, "_PAIR_CHUNK", 2)
    np.testing.assert_array_equal(returns.pair_correlation(ret, _PAIRS, min_periods=60), whole)


def test_pair_correlation_empty_inputs():
    assert returns.pair_correlation(_pair_frame(), [], min_periods=5).shape == (0,)
    none = returns.pair_correlation(pd.DataFrame(), [("A", "B")], min_periods=5)
    assert none.shape == (1,) and np.isnan(none[0])
    assert np.isnan(returns.pair_correlation(None, [("A", "B")], min_periods=5)[0])


# ── robust inputs (S10) ─────────────────────────────────────────────────────
def _clean_group(noise=0.004, n=250, seed=4, members=("A1", "A2", "A3")):
    """Three names on one factor — tight, but not so tight that a spike vanishes."""
    rng = np.random.default_rng(seed)
    common = rng.normal(0, 0.01, n)
    data = {t: common + rng.normal(0, noise, n) for t in members}
    return pd.DataFrame(data, index=pd.date_range("2025-01-01", periods=n))


def _mean_corr_with(ret, ticker, mates):
    corr = ret.corr()
    return float(corr.loc[ticker, list(mates)].mean())


def test_winsorize_mad_clips_the_spike_and_keeps_the_body():
    rng = np.random.default_rng(0)
    x = pd.Series(rng.normal(0, 1, 200))
    x[7] = 50.0
    out = returns.winsorize_mad(pd.DataFrame({"X": x}), k=5.0)["X"]
    med = x.median()
    mad = (x - med).abs().median()
    assert out.iloc[7] == pytest.approx(med + 5 * 1.4826 * mad)
    assert out.iloc[0] == pytest.approx(x.iloc[0])       # the body is untouched
    assert out.iloc[7] < 10.0                            # the spike is gone


def test_winsorize_mad_leaves_a_flat_column_unchanged():
    # MAD is 0 for a flat or near-flat column: there is no spread to clip
    # against, and dividing by it would wipe the column out.
    ret = pd.DataFrame({"FLAT": [0.0] * 50, "MOVES": np.linspace(-1, 1, 50)})
    out = returns.winsorize_mad(ret)
    assert (out["FLAT"] == 0.0).all()
    assert out["MOVES"].tolist() == ret["MOVES"].tolist()


def test_winsorize_mad_keeps_nan_nan():
    ret = pd.DataFrame({"A": [np.nan, 1.0, 2.0, np.nan, 3.0]})
    out = returns.winsorize_mad(ret)
    assert out["A"].isna().tolist() == [True, False, False, True, False]


def test_winsorize_mad_rescues_a_correlation_broken_by_one_spike():
    # One +80% print is enough to make a name look like it never moved with its
    # own group (review §4b: Pearson on log returns is dominated by a few days).
    clean = _clean_group()
    spiked = clean.copy()
    spiked.iloc[100, 0] += 0.8
    before = _mean_corr_with(spiked, "A1", ["A2", "A3"])
    fixed = _mean_corr_with(returns.winsorize_mad(spiked), "A1", ["A2", "A3"])
    reference = _mean_corr_with(clean, "A1", ["A2", "A3"])
    assert abs(before - reference) > 0.2                 # the spike moves it a lot
    assert abs(fixed - reference) < 0.05                 # winsorising gives it back


def test_flat_print_names_flags_a_long_unchanged_stretch():
    rng = np.random.default_rng(0)
    n = 300
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    halt = np.concatenate([np.linspace(50, 80, n - 20), np.full(20, 80.0)])
    normal = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    blip = normal.copy()
    for i in range(40, n, 40):          # six 2-session halts: 2% of the window,
        blip[i] = blip[i - 1]           # but never a 5-session run
    prices = pd.DataFrame({"HALT": halt, "NORMAL": normal, "BLIP": blip}, index=idx)
    assert returns.flat_print_names(prices, run=5, max_share=0.02) == ["HALT"]


def test_flat_print_names_ignores_a_short_window():
    prices = pd.DataFrame({"A": [1.0, 1.0, 1.0]})
    assert returns.flat_print_names(prices, run=5, max_share=0.02) == []


# ── clustering ──────────────────────────────────────────────────────────────
def _two_group_returns(n=300, seed=0):
    rng = np.random.default_rng(seed)
    data = {}
    for group in (("A1", "A2", "A3"), ("B1", "B2", "B3")):
        base = rng.normal(0, 0.01, n)
        for t in group:
            data[t] = base + rng.normal(0, 0.0005, n)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(data, index=idx)


def _market_two_groups(n=300, seed=11):
    """Two tight groups that both load heavily on one common market factor."""
    rng = np.random.default_rng(seed)
    market = rng.normal(0, 0.012, n)
    data = {"SPY": market}
    for group in (("A1", "A2", "A3"), ("B1", "B2", "B3")):
        common = rng.normal(0, 0.004, n)
        for t in group:
            data[t] = market + common + rng.normal(0, 0.0003, n)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(data, index=idx)


def test_residualize_removes_market_and_separates_groups():
    ret = _market_two_groups()
    raw = ret.drop(columns=["SPY"])

    raw_corr = returns.correlation(raw)
    assert raw_corr.loc["A1", "B1"] > 0.8  # market dominates -> raw groups blur

    resid = returns.residualize(raw, ret["SPY"])
    res_corr = returns.correlation(resid)
    assert res_corr.loc["A1", "A2"] > 0.8
    assert res_corr.loc["B1", "B2"] > 0.8
    assert res_corr.loc["A1", "B1"] < 0.3

    _Z, _l, _c, raw_groups = cluster.clusters(raw, (1, 2))
    assert len(raw_groups[1]) == 1  # broad cut merges the market blob

    _Z2, _l2, _c2, res_groups = cluster.clusters(resid, (2,))
    sets = sorted(sorted(v) for v in res_groups[2].values())
    assert sets == [["A1", "A2", "A3"], ["B1", "B2", "B3"]]


def test_tight_cut_separates_groups_and_broad_cut_merges():
    ret = _two_group_returns()
    Z, labels, corr, groups = cluster.clusters(ret, (1, 2))
    tight = groups[2]
    assert len(tight) == 2
    sets = sorted(sorted(v) for v in tight.values())
    assert sets == [["A1", "A2", "A3"], ["B1", "B2", "B3"]]
    assert len(groups[1]) == 1  # the broad cut merges everything


def test_mean_intra_corr_high_within_group():
    ret = _two_group_returns()
    _Z, _l, corr, groups = cluster.clusters(ret, (2,))
    for members in groups[2].values():
        assert cluster.mean_intra_corr(corr, members) > 0.9


# ── labeling (legacy majority: label_cluster, kept for research scripts) ───
# #97 moved the board onto label_level. These tests still pin the old helper.
def test_label_when_majority_shares_an_industry():
    members = ["NVDA", "AMD", "AVGO", "INTC"]
    industries = {t: "Semiconductors" for t in members}
    assert label.label_cluster(members, industries) == "Semiconductors"


def test_label_takes_the_industry_text_by_ticker_as_a_keyword():
    """#266: a cluster is named after the industry (UW's two levels), not a sub-industry."""
    members = ["MSFT", "ADBE", "CRM", "ORCL"]
    industries = {"MSFT": "Software - Infrastructure", "ADBE": "Software - Application",
                  "CRM": "Software - Application", "ORCL": "Software - Application"}
    assert label.label_cluster(members, industry_by_ticker=industries) == "Software - Application"


def test_label_does_not_merge_industries_that_share_a_prefix():
    # "Software - Application" and "Software - Infrastructure" are two industries: 2 of 4 each is no majority.
    members = ["A", "B", "C", "D"]
    industries = {"A": "Software - Application", "B": "Software - Application",
                  "C": "Software - Infrastructure", "D": "Software - Infrastructure"}
    assert label.label_cluster(members, industries) is None


def test_label_is_none_when_mixed_or_too_small():
    mixed = ["A", "B", "C", "D"]
    industries = {"A": "Semiconductors", "B": "Semiconductors", "C": "Banks - Regional", "D": "Banks - Regional"}
    assert label.label_cluster(mixed, industries) is None
    assert label.label_cluster(["A", "B", "C"], {"A": "Banks", "B": "Banks", "C": "Banks"}) is None


def test_label_falls_back_to_theme():
    members = ["A", "B", "C", "D"]
    themes = {t: ["Cloud Computing"] for t in members}
    assert label.label_cluster(members, {}, themes) == "Cloud Computing"


def test_label_never_names_a_group_after_the_unknown_bucket():
    """R3 (#86): ``Unclassified`` (the master's unknown) and the legacy ``Other`` are not businesses."""
    members = ["A", "B", "C", "D"]
    for unknown in ("Unclassified", "Other", ""):
        assert label.label_cluster(members, {t: unknown for t in members}) is None, unknown
    # the unknown bucket is skipped, so the theme still names the group
    assert label.label_cluster(members, {t: "Unclassified" for t in members},
                               {t: ["Cloud Computing"] for t in members}) == "Cloud Computing"
    # ... and the theme catch-all is never a label either
    assert label.label_cluster(members, {}, {t: ["Other"] for t in members}) is None
    # a real industry still wins over a majority of unknowns being popped
    industries = {"A": "Semiconductors", "B": "Semiconductors", "C": "Unclassified", "D": "Unclassified"}
    assert label.label_cluster(members, industries) is None        # 2 / 4 is not a strict majority
    industries = {"A": "Semiconductors", "B": "Semiconductors", "C": "Semiconductors", "D": "Unclassified"}
    assert label.label_cluster(members, industries) == "Semiconductors"


# ── ohlc ────────────────────────────────────────────────────────────────────
def test_benchmark_tickers_constant():
    assert ohlc.BENCHMARK_TICKERS == ("SPY", "DIA", "QQQ")


def test_fetch_bulk_no_fetch_never_downloads(rawstore_tmp_db):
    res = ohlc.fetch_bulk(["AAA", "BBB"], no_fetch=True)
    assert res["no_fetch"] is True and res["fetched"] == 0
    assert res["pending"] == 2 + len(ohlc.BENCHMARK_TICKERS)          # an empty D01 holds none of them


# ── engine ──────────────────────────────────────────────────────────────────
def _seed_universe(tickers):
    rawstore.replace_all("equity_universe", [{
        "ticker": t, "market_cap": 1.0e10, "sector": "Technology",
        "price": 10.0, "name": t, "industry": "Semiconductors",
        "quote": {"symbol": t},
    } for t in tickers])


def _seed_ohlc(ret: pd.DataFrame):
    rows = []
    now = time.time()
    for t in ret.columns:
        price = 100 * np.exp(ret[t].cumsum())
        for ts, p in price.items():
            rows.append({"ticker": t, "ts": ts.isoformat(), "open": p, "high": p,
                         "low": p, "close": float(p), "volume": 1.0, "cached_at": now})
    d01_bars.write_bars(rows)


def test_engine_run_persists_and_clusters(rawstore_tmp_db):
    ret = _two_group_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)

    payload = engine.run(no_fetch=True, tickers=tickers, cuts=(2,), persist=True)
    assert payload["eligible"] == 6
    assert len(payload["clusters"]["2"]) == 2
    assert rawstore.count("comov_runs") == 1
    assert rawstore.count("comov_nodes") == 2
    assert rawstore.count("comov_members") == 6

    loaded = engine.load_payload()
    assert loaded["as_of"] == payload["as_of"]
    assert len(loaded["clusters"]["2"]) == 2


def test_engine_residual_separates_shared_market_groups(rawstore_tmp_db):
    ret = _market_two_groups()
    tickers = [c for c in ret.columns if c != "SPY"]
    _seed_universe(tickers + ["SPY"])
    _seed_ohlc(ret)

    off = engine.run(no_fetch=True, tickers=tickers, horizon=1, benchmark="off",
                     cuts=(1, 2))
    assert off["benchmark_applied"] is False
    assert len(off["clusters"]["1"]) == 1  # broad cut: the market blob merges all

    spy = engine.run(no_fetch=True, tickers=tickers, horizon=1, benchmark="spy",
                     cuts=(1, 2))
    assert spy["benchmark_applied"] is True
    sets = sorted(sorted(m["ticker"] for m in c["members"])
                  for c in spy["clusters"]["2"])
    assert sets == [["A1", "A2", "A3"], ["B1", "B2", "B3"]]


def test_engine_empty_when_too_few_tickers(rawstore_tmp_db):
    _seed_universe(["AAA"])
    payload = engine.run(no_fetch=True, tickers=["AAA"], cuts=(2,))
    assert payload["eligible"] < 3
    assert payload["clusters"] == {}


def test_refresh_run_computes_but_does_not_write_store(rawstore_tmp_db):
    ret = _two_group_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    payload = engine.run(no_fetch=True, tickers=tickers, cuts=(2,))
    assert len(payload["clusters"]["2"]) == 2          # still computed in memory
    assert rawstore.count("comov_runs") == 0            # docs/data_retrieval.md: Refresh = .cache only
    assert rawstore.count("comov_members") == 0


def test_config_hash_changes_with_cuts():
    assert engine.config_hash(252, "average", (2, 3)) != engine.config_hash(252, "average", (2, 4))


def test_config_hash_changes_with_horizon_and_benchmark():
    base = engine.config_hash(252, "average", (2, 3), horizon=5, benchmark="spy")
    assert base != engine.config_hash(252, "average", (2, 3), horizon=1, benchmark="spy")
    assert base != engine.config_hash(252, "average", (2, 3), horizon=5, benchmark="off")


def test_engine_run_persists_horizon_and_benchmark(rawstore_tmp_db):
    ret = _market_two_groups()
    tickers = [c for c in ret.columns if c != "SPY"]
    _seed_universe(tickers + ["SPY"])
    _seed_ohlc(ret)

    engine.run(no_fetch=True, tickers=tickers, horizon=1, benchmark="off", cuts=(2,),
               persist=True)
    payload = engine.run(no_fetch=True, tickers=tickers, horizon=5, benchmark="spy",
                         cuts=(2,), persist=True)

    row = rawstore.query(
        "SELECT horizon, benchmark FROM comov_runs WHERE as_of = ?",
        [payload["as_of"]])[0]
    assert row["horizon"] == 5
    assert row["benchmark"] == "spy"

    loaded = engine.load_payload(horizon=5, benchmark="spy")
    assert loaded["as_of"] == payload["as_of"]

    off = engine.load_payload(horizon=1, benchmark="off")
    assert off["as_of"] != payload["as_of"]


def test_payload_reports_prices_through_and_benchmark_status(rawstore_tmp_db):
    ret = _market_two_groups()
    tickers = [c for c in ret.columns if c != "SPY"]
    _seed_universe(tickers + ["SPY"])
    _seed_ohlc(ret)
    spy = engine.run(no_fetch=True, tickers=tickers, horizon=1, benchmark="spy", cuts=(2,))
    assert spy["benchmark_status"] == "applied"
    assert spy["prices_through"] == ret.index[-1].strftime("%Y-%m-%d")
    off = engine.run(no_fetch=True, tickers=tickers, horizon=1, benchmark="off", cuts=(2,))
    assert off["benchmark_status"] == "off"
    qqq = engine.run(no_fetch=True, tickers=tickers, horizon=1, benchmark="qqq", cuts=(2,))
    assert qqq["benchmark_status"] == "missing"          # QQQ never seeded
    assert qqq["benchmark_applied"] is False


def test_stale_benchmark_truncates_window_and_says_so(rawstore_tmp_db):
    ret = _market_two_groups()
    tickers = [c for c in ret.columns if c != "SPY"]
    _seed_universe(tickers + ["SPY"])
    _seed_ohlc(ret.drop(columns=["SPY"]))
    _seed_ohlc(ret[["SPY"]].iloc[:-12])                  # SPY 12 sessions behind
    p = engine.run(no_fetch=True, tickers=tickers, horizon=1, benchmark="spy", cuts=(2,))
    assert p["benchmark_status"] == "stale"
    assert p["benchmark_applied"] is True
    assert p["window_end"] == ret.index[-13].strftime("%Y-%m-%d")
    for c in p["clusters"]["2"]:
        assert c["last_residual_pct"] is not None          # tail no longer all-NaN


def test_stray_newer_bars_do_not_mark_benchmark_stale(rawstore_tmp_db):
    # One ticker with a few bars past everyone else (live: 2 names at 09-25
    # while 3,298 end 09-17) must not make a current SPY look stale.
    ret = _market_two_groups()
    tickers = [c for c in ret.columns if c != "SPY"] + ["ZZZ"]
    _seed_universe(tickers + ["SPY"])
    _seed_ohlc(ret)
    rng = np.random.default_rng(3)
    z_idx = pd.date_range(ret.index[0], periods=len(ret) + 5, freq="D")
    z = np.concatenate([ret["A1"].to_numpy(), rng.normal(0, 0.01, 5)])
    _seed_ohlc(pd.DataFrame({"ZZZ": z}, index=z_idx))
    p = engine.run(no_fetch=True, tickers=tickers, horizon=1, benchmark="spy", cuts=(2,))
    assert p["prices_through"] == ret.index[-1].strftime("%Y-%m-%d")
    assert p["benchmark_status"] == "applied"


def test_run_reports_names_without_prices(rawstore_tmp_db):
    ret = _two_group_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers + ["NOPX"])
    _seed_ohlc(ret)
    p = engine.run(no_fetch=True, tickers=tickers + ["NOPX"], cuts=(2,))
    assert p["no_prices"] == ["NOPX"]
    assert p["universe_total"] == 7 and p["eligible"] == 6


def test_load_payload_round_trips_status_and_does_not_invent_it(rawstore_tmp_db, monkeypatch):
    ret = _market_two_groups()
    tickers = [c for c in ret.columns if c != "SPY"]
    _seed_universe(tickers + ["SPY"])
    _seed_ohlc(ret)
    monkeypatch.setattr(ohlc, "fetch_bulk",
                        lambda tickers, no_fetch=False, **kw: {"no_fetch": no_fetch})
    engine.run(no_fetch=False, tickers=tickers, horizon=1, benchmark="qqq", cuts=(2,))  # QQQ missing
    loaded = engine.load_payload(horizon=1, benchmark="qqq")
    assert loaded["benchmark_status"] == "missing"
    assert loaded["benchmark_applied"] is False        # old code: True because bench != "off"
    assert loaded["prices_through"] == ret.index[-1].strftime("%Y-%m-%d")
    assert loaded["dropped"] is None                    # unknown after reload, not "none"


# ── noise gate (S8) ─────────────────────────────────────────────────────────
def _blocky_returns(k=8, m=40, n=400, seed=99):
    """``k`` names built from one factor plus ``m`` independent ones.

    A real block in a noise matrix — the block is 8 of 48 names, so a random
    group of the same size almost never contains two of them.
    """
    rng = np.random.default_rng(seed)
    base = rng.normal(0, 0.01, n)
    data = {}
    for i in range(k):
        data[f"T{i:02d}"] = base + rng.normal(0, 0.0002, n)
    for i in range(k, k + m):
        data[f"T{i:02d}"] = rng.normal(0, 0.01, n)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(data, index=idx)


def test_engine_marks_a_real_block_significant_and_the_noise_groups_not(rawstore_tmp_db):
    ret = _blocky_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    # persist=True is the Fetch path: it fits the shuffled-returns null.
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(4,), persist=True)
    block_names = {f"T{i:02d}" for i in range(8)}
    cards = p["clusters"]["4"]
    holders = [c for c in cards
               if block_names <= {m["ticker"] for m in c["members"]}]
    assert len(holders) == 1                    # exactly one card holds the block
    block = holders[0]
    assert block["mean_corr"] > block["baseline_r"]
    assert block["significant"] is True
    for other in cards:
        if other is block:
            continue
        assert other["baseline_r"] is not None
        assert other["mean_corr"] < other["baseline_r"]
        assert other["significant"] is False
    # 48 names, one real common move (the block); the rest is noise.
    assert p["n_factors"] == 1


def test_load_payload_derives_significance_from_the_persisted_baseline(rawstore_tmp_db):
    ret = _blocky_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(4,), persist=True)
    loaded = engine.load_payload()
    assert loaded["n_factors"] == p["n_factors"]
    assert len(loaded["clusters"]["4"]) == 4
    for c in loaded["clusters"]["4"]:
        assert c["baseline_r"] is not None
        assert c["significant"] is (c["mean_corr"] > c["baseline_r"])


def test_load_payload_significance_is_none_for_a_run_without_a_baseline(rawstore_tmp_db):
    # A run persisted before S8 has baseline_r NULL. It must read as "unknown",
    # never as "not significant" — the board must not hide names it cannot judge.
    ret = _two_group_returns()
    _seed_universe(list(ret.columns))
    as_of = "2026-01-01T00:00:00+00:00"
    rawstore.write_rows("comov_runs", [{
        "as_of": as_of, "generated_at": as_of, "window_days": 252, "n_tickers": 6,
        "linkage": "average", "config_hash": "old", "horizon": 1, "benchmark": "spy"}])
    rawstore.write_rows("comov_nodes", [{
        "as_of": as_of, "cut": 2, "cluster_id": "1", "label": "Cluster 1",
        "n": 3, "mean_corr": 0.5, "last_residual_pct": 1.0}])
    rawstore.write_rows("comov_members", [
        {"as_of": as_of, "cut": 2, "cluster_id": "1", "ticker": t} for t in ("A1", "A2", "A3")])
    loaded = engine.load_payload()
    assert loaded["n_factors"] is None
    card = loaded["clusters"]["2"][0]
    assert card["baseline_r"] is None
    assert card["significant"] is None


# ── catch-all group (S12) ───────────────────────────────────────────────────
def test_config_catchall_share():
    assert config.CATCHALL_SHARE == 0.40


def test_engine_flags_the_catch_all_group_and_load_payload_derives_it(rawstore_tmp_db):
    # _blocky_returns at cut 2: Ward splits the 48 names into the 8-name block
    # and the 40 noise names; the 40-name group holds 83% (> 40%) of the
    # clustered names, the block 17%.
    ret = _blocky_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(2,), persist=True)
    flags = {c["n"]: c["catchall"] for c in p["clusters"]["2"]}
    assert flags == {40: True, 8: False}
    # Derived again on reload from n / total members at the cut: no column.
    loaded = engine.load_payload()
    assert {c["n"]: c["catchall"] for c in loaded["clusters"]["2"]} == flags
    cols = {r["column_name"] for r in rawstore.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name IN ('comov_nodes', 'comov_members')")}
    assert "catchall" not in cols


def test_engine_persists_baseline_n_factors_and_the_null(rawstore_tmp_db):
    ret = _two_group_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(2,), persist=True)
    node = rawstore.query("SELECT baseline_r FROM comov_nodes WHERE as_of = ?",
                          [p["as_of"]])[0]
    run = rawstore.query("SELECT n_factors, null_json, linkage FROM comov_runs "
                         "WHERE as_of = ?", [p["as_of"]])[0]
    assert node["baseline_r"] is not None
    assert run["n_factors"] == p["n_factors"]
    assert run["linkage"] == "ward"
    assert json.loads(run["null_json"])            # grid point -> quantile


def test_refresh_reuses_the_persisted_null_and_skips_the_permutations(
        rawstore_tmp_db, monkeypatch):
    ret = _blocky_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    fetched = engine.run(no_fetch=True, tickers=tickers, cuts=(4,), persist=True)

    def _boom(*a, **k):
        raise AssertionError("a Refresh must not re-fit the null")
    monkeypatch.setattr(engine.noise, "selected_group_null", _boom)
    refreshed = engine.run(no_fetch=True, tickers=tickers, cuts=(4,), persist=False)
    base = {c["n"]: c["baseline_r"] for c in fetched["clusters"]["4"]}
    assert {c["n"]: c["baseline_r"] for c in refreshed["clusters"]["4"]} == base
    assert any(c["significant"] for c in refreshed["clusters"]["4"])


def test_refresh_without_a_persisted_null_leaves_groups_unjudged(rawstore_tmp_db):
    ret = _blocky_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(4,), persist=False)
    for c in p["clusters"]["4"]:
        assert c["baseline_r"] is None
        assert c["significant"] is None              # shown, never folded away


def test_persisted_null_ignores_runs_of_another_config(rawstore_tmp_db):
    rawstore.write_rows("comov_runs", [{
        "as_of": "2026-01-01T00:00:00+00:00", "generated_at": "x",
        "window_days": 252, "n_tickers": 6, "linkage": "average",
        "config_hash": "avg-hash", "horizon": 1, "benchmark": "spy",
        "null_json": json.dumps({"2": 0.3})}])
    assert engine._persisted_null("avg-hash") == {2: 0.3}
    assert engine._persisted_null("ward-hash") == {}


# ── core vs joined-this-quarter (persistence.core_members) ─────────────────
def _persisted_run_with_core():
    """One dated run holding a single 5-name group with mixed ``core``."""
    as_of = "2026-01-01T00:00:00+00:00"
    rawstore.write_rows("comov_runs", [{
        "as_of": as_of, "generated_at": as_of, "window_days": 252, "n_tickers": 5,
        "linkage": "ward", "config_hash": "core", "horizon": 1, "benchmark": "spy",
        "benchmark_status": "applied", "prices_through": "2025-12-31",
    }])
    rawstore.write_rows("comov_nodes", [{
        "as_of": as_of, "cut": 2, "cluster_id": "1", "label": "Cluster 1",
        "n": 5, "mean_corr": 0.5, "baseline_r": 0.05, "last_residual_pct": 1.0,
    }])
    rawstore.write_rows("comov_members", [
        {"as_of": as_of, "cut": 2, "cluster_id": "1", "ticker": t, "core": c}
        for t, c in {"AAA": False, "BBB": True, "CCC": None, "DDD": True,
                     "EEE": False}.items()])
    _seed_universe(["AAA", "BBB", "CCC", "DDD", "EEE"])
    return as_of


def test_load_payload_orders_core_first_then_ticker(rawstore_tmp_db):
    # C1: a reload used to sort members alphabetically while a fresh run kept
    # cluster order, so the same group read differently after a Refresh.
    _persisted_run_with_core()
    card = engine.load_payload()["clusters"]["2"][0]
    assert [(m["ticker"], m["core"]) for m in card["members"]] == [
        ("BBB", True), ("DDD", True),                  # with the group a quarter ago
        ("AAA", False), ("CCC", None), ("EEE", False)]  # joined since / unknown
    assert "stability" not in card


def _lag_two_of_the_a_group(monkeypatch):
    # A quarter ago A1 and A2 were together with a name that has since gone:
    # J({A1,A2,A3}, {A1,A2,GONE}) = 2/4 = 0.5 → the same group; B* is new.
    lag = {2: [{"lag_id": "1", "members": ["A1", "A2", "GONE"], "oos_r": 0.3,
                "oos_base": 0.1, "held": True}]}
    monkeypatch.setattr(engine.persistence, "lagged_test", lambda *a, **k: lag)


def test_engine_marks_core_from_the_quarter_ago_match(rawstore_tmp_db, monkeypatch):
    ret = _two_group_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    _lag_two_of_the_a_group(monkeypatch)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(2,), persist=True)
    groups = {frozenset(m["ticker"] for m in c["members"]): c for c in p["clusters"]["2"]}
    a = groups[frozenset({"A1", "A2", "A3"})]
    assert [(m["ticker"], m["core"]) for m in a["members"]] == [
        ("A1", True), ("A2", True), ("A3", False)]
    assert a["persist"]["new"] is False and a["persist"]["held"] is True
    b = groups[frozenset({"B1", "B2", "B3"})]
    assert b["persist"]["new"] is True
    assert all(m["core"] is None for m in b["members"])   # new: nothing is "fringe"
    assert "stability" not in a


def test_run_and_reload_agree_on_member_order(rawstore_tmp_db, monkeypatch):
    ret = _two_group_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    _lag_two_of_the_a_group(monkeypatch)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(2,), persist=True)
    loaded = engine.load_payload()
    fresh = {c["label"]: [(m["ticker"], m["core"]) for m in c["members"]]
             for c in p["clusters"]["2"]}
    again = {c["label"]: [(m["ticker"], m["core"]) for m in c["members"]]
             for c in loaded["clusters"]["2"]}
    assert fresh == again


def test_fetch_no_longer_runs_the_bootstrap():
    # Removed 2026-09-30: resampling dates inside one window measured sampling
    # noise, not persistence, and cost ~20 clusterings per Fetch.
    assert not hasattr(noise, "coassignment_stability")
    assert not hasattr(config, "STABILITY_BOOT")


# ── robust inputs in the engine (S10) ───────────────────────────────────────
def _with_flat_printer(ret: pd.DataFrame, name="FLAT", flat=20) -> pd.DataFrame:
    """``ret`` plus a name whose close has not moved for the last ``flat`` sessions."""
    ret = ret.copy()
    rng = np.random.default_rng(7)
    ret[name] = rng.normal(0, 0.01, len(ret))
    ret.loc[ret.index[-flat:], name] = 0.0
    return ret


def test_engine_excludes_flat_printers_and_lists_them(rawstore_tmp_db):
    ret = _with_flat_printer(_two_group_returns())
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(2,))
    assert p["flat_prints"] == ["FLAT"]
    # The exclusion is its own bucket: a flat name has prices and history.
    assert "FLAT" not in (p["no_prices"] or [])
    assert "FLAT" not in (p["dropped"] or [])
    assert p["eligible"] == 6
    assert all(m["ticker"] != "FLAT"
               for c in p["clusters"]["2"] for m in c["members"])


def test_engine_winsorizes_before_clustering(rawstore_tmp_db):
    # Without winsorising, one +80% print pulls the name out of its own group.
    ret = _clean_group(members=("A1", "A2", "A3"))
    ret["B1"] = np.random.default_rng(3).normal(0, 0.01, len(ret))
    spiked = ret.copy()
    spiked.iloc[100, 0] += 0.8
    tickers = list(spiked.columns)
    _seed_universe(tickers)
    _seed_ohlc(spiked)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(2,))
    holder = [c for c in p["clusters"]["2"]
              if {"A1", "A2", "A3"} <= {m["ticker"] for m in c["members"]}]
    assert len(holder) == 1
    assert holder[0]["mean_corr"] > 0.8


def test_engine_last_move_uses_the_raw_returns(rawstore_tmp_db):
    # The direction line is a real move: winsorising must not clip it away.
    ret = _two_group_returns()
    ret.loc[ret.index[-1], ["A1", "A2", "A3"]] += 0.8
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(2,), benchmark="off")
    card = [c for c in p["clusters"]["2"]
            if "A1" in {m["ticker"] for m in c["members"]}][0]
    assert card["last_residual_pct"] > 20


# ── report (readability layer) ──────────────────────────────────────────────
def _report_payload(**over):
    payload = {
        "as_of": "2026-09-17T00:00:00+00:00", "eligible": 2, "universe_total": 2,
        "cuts": [20], "fetch": {}, "horizon": 5, "benchmark": "spy",
        "benchmark_applied": True, "themes": [],
        "prices_through": "2026-09-17", "benchmark_status": "applied",
        "clusters": {"20": [{
            "id": "1", "label": "Cluster 1", "n": 2, "mean_corr": 0.72,
            "last_residual_pct": -3.4,
            "members": [
                {"ticker": "AAA", "name": "A", "sector": "Financial Services",
                 "market_cap": 1e9, "themes": []},
                {"ticker": "BBB", "name": "B", "sector": "Financial Services",
                 "market_cap": 1e9, "themes": []},
            ],
        }]},
    }
    payload.update(over)
    return payload


def test_report_has_readability_layer():
    from RevenueChaser.module_comovement import report

    html = report.render_comovement_dashboard(_report_payload())
    assert "About this data" in html              # the board-level explanation
    assert "AAA" in html and "BBB" in html
    # Remove market + Horizon knobs, and the benchmark options.
    assert "Remove market" in html
    assert "Horizon" in html
    assert "1d" in html and "5d" in html
    assert "S&P 500" in html and "SPY" in html
    assert "after removing" in html
    # Filters must be visible on the board, not buried in a closed <details>:
    # the shared bar mounts into an always-visible section.
    assert '<details class="filters">' not in html
    assert '<section class="rcf-bar" id="rc-filters"></section>' in html
    assert "RCFilters.mount(" in html
    # Cards are ordered by residual tightness in JS, not by size.
    assert "by[b].mean" in html and "by[a].mean" in html
    assert "by[b].items.length - by[a].items.length" not in html
    # S8/D5: the fixed LEFTOVER_R hiding is replaced by the visible card.
    assert "LEFTOVER" not in html
    assert "No shared move" in html


def test_report_defaults_spy_and_1d():
    from RevenueChaser.module_comovement import report

    # S7 (decision D4, prior E1): the default horizon is 1d, not 5d. Passing no
    # horizon is what a stored payload without one looks like.
    html = report.render_comovement_dashboard(
        _report_payload(horizon=None, benchmark="spy"))
    assert re.search(r'data-v="spy" aria-checked="true"', html)
    assert re.search(r'data-v="1" aria-checked="true"', html)
    assert not re.search(r'data-v="5" aria-checked="true"', html)


def test_config_default_horizon_is_one_day():
    assert config.DEFAULT_HORIZON == 1
    # The knob keeps all four horizons (decision D4).
    assert config.HORIZONS == (1, 3, 5, 10)


def test_relatedness_cluster_levels_reads_the_comovement_default_run(monkeypatch):
    # S7: Relatedness's cluster chips used a hardcoded horizon = 5, so they went
    # stale the moment the board's default changed. It must read the co-movement
    # default (horizon, benchmark) instead.
    from types import SimpleNamespace

    from RevenueChaser.module_comovement import config as comov_config
    from RevenueChaser.module_comovement import settings as comov_settings
    from RevenueChaser.module_relatedness import engine as rel_engine

    seen = []

    def _q(sql, params=None, **kw):
        seen.append((sql, list(params or [])))
        return []

    monkeypatch.setattr(rel_engine.rawstore, "query", _q)
    monkeypatch.setattr(comov_settings, "effective",
                        lambda window=None: SimpleNamespace(default_window=189))
    assert rel_engine._cluster_levels() == {}
    sql, params = seen[0]
    assert "horizon = ?" in sql and "benchmark = ?" in sql
    # #95: a precomputed window must not displace the default-window run
    assert "window_days = ?" in sql
    assert params == [comov_config.DEFAULT_HORIZON, comov_config.DEFAULT_BENCHMARK, 189]


def test_report_empty_state():
    from RevenueChaser.module_comovement import report
    html = report.render_comovement_dashboard({"as_of": "2026-09-17T00:00:00+00:00", "clusters": {}, "cuts": []})
    assert "No clusters yet" in html


def test_report_header_uses_prices_through_not_compute_time():
    from RevenueChaser.module_comovement import report
    html = report.render_comovement_dashboard(_report_payload(
        as_of="2026-09-25T23:21:28+00:00", prices_through="2026-09-17",
        benchmark_status="applied"))
    assert "Prices through <b>Thursday, September 17, 2026 EOD</b>" in html
    assert "September 25" not in html


def test_report_missing_benchmark_note_is_truthful():
    from RevenueChaser.module_comovement import report
    html = report.render_comovement_dashboard(_report_payload(
        benchmark_applied=False, benchmark_status="missing"))
    assert "is not in the store" not in html
    assert "no usable prices" in html


def test_report_raw_direction_is_not_called_vs_market():
    from RevenueChaser.module_comovement import report
    html = report.render_comovement_dashboard(_report_payload(
        benchmark="off", benchmark_applied=False, benchmark_status="off"))
    assert "'market'" not in html          # old: "% vs ' + (res ? b.etf : 'market')"
    assert "raw, market included" in html


def test_report_stale_benchmark_note_names_window_end():
    from RevenueChaser.module_comovement import report
    html = report.render_comovement_dashboard(_report_payload(
        benchmark_status="stale", window_end="2026-08-31"))
    assert "ends 2026-08-31" in html
    assert "after removing" in html                 # applied sub-title is kept
    assert 'class="alert"' in html                  # a warning is state: always visible


def test_report_shows_drop_accounting():
    from RevenueChaser.module_comovement import report
    html = report.render_comovement_dashboard(_report_payload(
        universe_total=3751, eligible=3489, no_prices=["AAC"] * 145, dropped=["X"] * 118))
    # The funnel: ≥ floor slice → drops → clustered, even for an old payload
    # that has only the drop lists.
    assert "clustered <b class=\"num\">3,489</b>" in html
    assert "No cached prices</span><span class=\"num\">−145</span>" in html
    assert "Short history" in html and "−118" in html


def test_report_shows_flat_print_accounting():
    from RevenueChaser.module_comovement import report
    html = report.render_comovement_dashboard(_report_payload(
        universe_total=3751, eligible=3392, no_prices=["AAC"] * 145,
        dropped=["X"] * 118, flat_prints=["ZZZ"] * 97))
    assert "clustered <b class=\"num\">3,392</b>" in html
    assert "−145" in html and "−118" in html
    assert "Flat / halted prints</span><span class=\"num\">−97</span>" in html


# ── card and limits UX (S11) ────────────────────────────────────────────────
def test_report_states_its_limits_under_the_sub_title():
    from RevenueChaser.module_comovement import report
    html = report.render_comovement_dashboard(_report_payload(n_factors=21))
    assert ("Groups, not pairs · 21 common moves above noise · membership moves "
            "week to week") in html
    assert "independent bets" not in html
    # No n_factors (a run persisted before 2026-10-01): keep the claim, drop the number.
    none = report.render_comovement_dashboard(_report_payload(n_factors=None))
    assert "Groups, not pairs · membership moves week to week" in none
    assert "common moves" not in none


def test_report_uses_the_shared_help_box_once():
    from RevenueChaser.module_comovement import report
    html = report.render_comovement_dashboard(_report_payload())
    # One board-level "About this data" panel replaces the help box.
    assert html.count('<details class="about"') == 1
    assert "About this data" in html
    assert "Coarse" in html and "Fine" in html          # the old body survived
    # S11: what r, the persistence badges and a dashed chip actually mean.
    assert ("r is compared to the groups the same method finds in shuffled, "
            "pure-noise returns. “Held out-of-sample” means") in html
    assert "Dashed names joined the group since then." in html
    assert "“stable”" not in html
    # Source, window and horizon are stated, not left to guess (plan S5).
    assert "price_bars_daily" in html and "Daily Bars" in html and "swing_ohlc" not in html
    assert "What a group is" in html
    # #97: a label is the enrichment rule, not a bare majority.
    assert ("A group is named only when one industry (else a theme) is over-represented "
            "against all grouped names at that level: at least 4 names and 25% of the group, "
            "at least 3× its share at that level, and significant after correcting for the "
            "many groups tested (BH q < 0.01). The label shows that share and the ×lift; "
            "otherwise the group stays a neutral “Cluster N”.") in html
    assert "GICS sub-industry" not in html


def test_one_name_groups_cannot_comove_and_merge_away():
    from RevenueChaser.module_comovement import report

    payload = _report_payload(clusters={"20": [
        _report_cluster("1", mean=0.72, tickers=("AAA", "BBB"),
                        significant=True, baseline_r=0.06, stability=0.9),
        _report_cluster("2", mean=0.90, tickers=("CCC",),
                        significant=True, baseline_r=0.02, stability=1.0),
    ]})
    rows = report._member_rows(payload, applied=True)
    assert {r["ticker"]: r["no_shared"] for r in rows} == {
        "AAA": False, "BBB": False, "CCC": True}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_merges_a_one_name_group_into_no_shared_move(tmp_path):
    from RevenueChaser.module_comovement import report

    payload = _report_payload(clusters={"20": [
        _report_cluster("1", mean=0.72, tickers=("AAA", "BBB"),
                        significant=True, baseline_r=0.06),
        _report_cluster("2", mean=0.90, tickers=("CCC",),
                        significant=True, baseline_r=0.02),
    ]})
    got = _run_board_js(report.render_comovement_dashboard(payload), tmp_path)
    assert "No shared move beyond SPY · 1 name" in got["grid"]
    assert "Cluster 2" not in got["grid"]
    assert got["count"] == "3 names in 2 groups"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_shows_group_composition_top_two_plus_other(tmp_path):
    from RevenueChaser.module_comovement import report

    industries = (["Semiconductors"] * 7 + ["Semiconductor Equipment & Materials"] * 3
                  + ["Banks - Regional"] * 2)
    tickers = tuple(f"T{i:02d}" for i in range(len(industries)))
    payload = _report_payload(clusters={"20": [_report_cluster(
        "1", mean=0.72, tickers=tickers, significant=True, baseline_r=0.06,
        member_meta={t: {"industry": s} for t, s in zip(tickers, industries)})]})
    grid = _run_board_js(report.render_comovement_dashboard(payload), tmp_path)["grid"]
    # Top two industries, then "other" with how many industries it holds;
    # each segment is a button that opens its names.
    assert '<span class="n num">7</span> Semiconductors' in grid
    assert '<span class="n num">3</span> Semiconductor Equipment &amp; Materials' in grid
    assert '<span class="n num">2</span> other <span class="meta">(1 industry)</span>' in grid
    assert grid.count('class="seg-btn') == 3 and 'aria-expanded="false"' in grid
    # The composition, its panels and the filter shortcut all talk about the industry (#266).
    assert 'aria-label="Composition by industry"' in grid
    assert grid.count(">Filter to this industry</button>") == 3
    assert "sub-industr" not in grid and "GICS" not in grid


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_counts_the_industries_behind_other(tmp_path):
    from RevenueChaser.module_comovement import report

    industries = (["Biotechnology"] * 4 + ["Drug Manufacturers - General"] * 3
                  + ["Medical Devices"] * 2 + ["Diagnostics & Research"] * 1)
    tickers = tuple(f"T{i:02d}" for i in range(len(industries)))
    payload = _report_payload(clusters={"20": [_report_cluster(
        "1", mean=0.72, tickers=tickers, significant=True, baseline_r=0.06,
        member_meta={t: {"industry": s} for t, s in zip(tickers, industries)})]})
    grid = _run_board_js(report.render_comovement_dashboard(payload), tmp_path)["grid"]
    assert '<span class="n num">3</span> other <span class="meta">(2 industries)</span>' in grid


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_composition_uses_one_level_and_adds_up_to_n(tmp_path):
    from RevenueChaser.module_comovement import report

    meta = {
        # no industry text: the sector is NOT a segment of its own
        "AAA": {"industry": "", "sector": "Financial Services"},
        # "Other" is not a business -> no industry
        "BBB": {"industry": "Other", "sector": "Energy"},
        "CCC": {"industry": "", "sector": "Energy"},
        # nothing usable at any level -> no industry
        "DDD": {"industry": "Unclassified", "sector": "Unclassified"},
        # one real industry, with characters the board must escape
        "EEE": {"industry": "Oil & Gas E&P", "sector": "Energy"},
    }
    payload = _report_payload(clusters={"20": [_report_cluster(
        "1", mean=0.72, tickers=tuple(meta), significant=True, baseline_r=0.06,
        member_meta=meta)]})
    grid = _run_board_js(report.render_comovement_dashboard(payload), tmp_path)["grid"]
    # Segments are ONE level (the industry), never a mix of industry and sector; names
    # without one are an explicit segment, so the parts sum to n.
    assert '<span class="n num">1</span> Oil &amp; Gas E&amp;P' in grid
    assert '<span class="n num">4</span> unclassified <span class="meta">(no industry)</span>' in grid
    assert "Energy" not in grid.split('class="chips"')[0]
    assert "Other" not in grid
    assert "No industry" in grid                                      # the panel of the unclassified names
    assert "No GICS" not in grid


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_composition_treats_the_master_unknown_like_the_old_other(tmp_path):
    """R3 (#86): the identity master writes ``Unclassified``; the board treats it as "no industry" exactly
    as it treated ``Other`` (the legacy value still on rows with no master entry)."""
    from RevenueChaser.module_comovement import report

    meta = {
        "AAA": {"industry": "Unclassified", "sector": "Unclassified",
                "gics": ["Unclassified", "Unclassified"]},
        "BBB": {"industry": "Other", "sector": "Other", "gics": ["Other", "Other"]},
        "CCC": {"industry": "Semiconductors", "sector": "Technology",
                "gics": ["Technology", "Semiconductors"]},
    }
    payload = _report_payload(clusters={"20": [_report_cluster(
        "1", mean=0.72, tickers=tuple(meta), significant=True, baseline_r=0.06,
        member_meta=meta)]})
    grid = _run_board_js(report.render_comovement_dashboard(payload), tmp_path)["grid"]
    assert '<span class="n num">1</span> Semiconductors' in grid
    assert '<span class="n num">2</span> unclassified' in grid       # the two unknowns, one segment
    assert "Unclassified" not in grid and "Other" not in grid        # never an "industry" of its own


def test_member_rows_default_the_sector_to_unclassified():
    from RevenueChaser.module_comovement import report

    payload = _report_payload(clusters={"20": [_report_cluster(
        "1", tickers=("AAA",), member_meta={"AAA": {"sector": "", "industry": ""}})]})
    row = report._member_rows(payload, applied=True)[0]
    assert row["sector"] == "Unclassified"
    assert row["industry"] == ""
    assert row["gics"] == ["Unclassified", ""]                      # the tree stops at the sector
    assert "sub_industry" not in row and "industry_group" not in row


def test_member_rows_carry_the_industry_and_a_two_level_gics_path():
    from RevenueChaser.module_comovement import report

    meta = {
        "AAA": {"sector": "Technology", "industry": "Software - Application"},
        # a legacy "Other" industry reads Unclassified on the filter path, like core.universe does
        "BBB": {"sector": "Energy", "industry": "Other"},
        # a path the engine already built (core.universe.filter_fields) is kept as it is
        "CCC": {"sector": "Healthcare", "industry": "Biotechnology",
                "gics": ["Healthcare", "Biotechnology (as the engine built it)"]},
    }
    payload = _report_payload(clusters={"20": [_report_cluster(
        "1", tickers=tuple(meta), member_meta=meta)]})
    rows = {r["ticker"]: r for r in report._member_rows(payload, applied=True)}
    assert rows["AAA"]["industry"] == "Software - Application"
    assert rows["AAA"]["gics"] == ["Technology", "Software - Application"]
    assert rows["BBB"]["gics"] == ["Energy", "Unclassified"]
    assert rows["CCC"]["gics"] == ["Healthcare", "Biotechnology (as the engine built it)"]
    for r in rows.values():
        assert "sub_industry" not in r and "industry_group" not in r


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_renders_no_composition_line_when_industry_is_wiped(tmp_path):
    from RevenueChaser.module_comovement import report

    # A store with no usable industry: every industry is '' / 'Other' / 'Unclassified' and the sector
    # is the unknown default, so the card must not claim "Other x 3".
    meta = {"AAA": {"industry": "Other", "sector": "Other"},
            "BBB": {"industry": "Unclassified", "sector": "Unclassified"},
            "CCC": {"industry": "", "sector": ""}}
    payload = _report_payload(clusters={"20": [_report_cluster(
        "1", mean=0.72, tickers=tuple(meta), significant=True, baseline_r=0.06,
        member_meta=meta)]})
    grid = _run_board_js(report.render_comovement_dashboard(payload), tmp_path)["grid"]
    assert "Other" not in grid
    assert '<span class="n num">3</span> unclassified' in grid


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_explains_a_name_by_the_shared_industry(tmp_path):
    from RevenueChaser.module_comovement import report

    named = _report_cluster(
        "1", label="Semiconductors", tickers=("AAA", "BBB", "CCC", "DDD"),
        significant=True, baseline_r=0.06,
        member_meta={"AAA": {"industry": "Semiconductors"}, "BBB": {"industry": "Semiconductors"},
                     "CCC": {"industry": "Semiconductors"}, "DDD": {"industry": "Banks - Regional"}})
    neutral = _report_cluster(
        "2", mean=0.6, tickers=("EEE", "FFF", "GGG", "HHH"), significant=True, baseline_r=0.06,
        member_meta={"EEE": {"industry": "Semiconductors"}, "FFF": {"industry": "Semiconductors"},
                     "GGG": {"industry": "Banks - Regional"}, "HHH": {"industry": "Banks - Regional"}})
    # most members "Unclassified" is not a business: the label was popped, so the card says neutral too
    unknown = _report_cluster(
        "3", mean=0.5, tickers=("III", "JJJ", "KKK", "LLL"), significant=True, baseline_r=0.06,
        member_meta={"III": {"industry": "Unclassified"}, "JJJ": {"industry": "Unclassified"},
                     "KKK": {"industry": "Unclassified"}, "LLL": {"industry": "Banks - Regional"}})
    grid = _run_board_js(report.render_comovement_dashboard(
        _report_payload(clusters={"20": [named, neutral, unknown]})), tmp_path)["grid"]
    assert grid.count("No industry or theme is over-represented enough here") == 2
    assert "Named <b>Cluster" not in grid and "Named <b>Unclassified" not in grid
    assert ("Named <b>Semiconductors</b>: the share and ×lift in the name are against "
            "all grouped names at this level (BH q < 0.01).") in grid
    assert ("No industry or theme is over-represented enough here, so the group keeps "
            "a neutral number instead of a made-up label.") in grid
    assert "sub-industry" not in grid and "GICS" not in grid


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_escapes_the_industry_text_a_group_is_named_after(tmp_path):
    """The industry is a provider string and now also the group's name: every place the board
    prints it (title, bar, legend, panel, the "why" text) must go through ``rcEsc``."""
    from RevenueChaser.module_comovement import report

    hostile = "<img src=x onerror=alert(1)>"
    tickers = ("AAA", "BBB", "CCC", "DDD")
    payload = _report_payload(clusters={"20": [_report_cluster(
        "1", label=hostile, mean=0.72, tickers=tickers, significant=True, baseline_r=0.06,
        member_meta={t: {"industry": hostile} for t in tickers})]})
    grid = _run_board_js(report.render_comovement_dashboard(payload), tmp_path)["grid"]
    assert "<img" not in grid
    assert grid.count("&lt;img src=x onerror=alert(1)&gt;") >= 6   # label, why title + text, bar, legend, panel
    assert "the share and ×lift in the name are against all grouped names at this level" in grid


# Capture the board's listeners so a test can fire a hover or a click on the real script.
_PROBE_HOOKS = r"""
var _docL = {}, _gridL = {};
document.addEventListener = function(ev, fn){ (_docL[ev] = _docL[ev] || []).push(fn); };
_els["grid"] = _el("grid");
_els["grid"].addEventListener = function(ev, fn){ (_gridL[ev] = _gridL[ev] || []).push(fn); };
_els["tip"] = _el("tip");
_els["tip"].getBoundingClientRect = function(){ return {width: 100, height: 40}; };
"""
_PROBE_DRIVER = r"""
globalThis.window = {innerWidth: 1200, innerHeight: 800};
function _target(sel, el){ return {closest: function(s){ return s === sel ? el : null; }}; }
function _attrs(map){ return {getAttribute: function(k){ return map[k]; },
                              getBoundingClientRect: function(){ return {left: 10, bottom: 20, top: 5}; }}; }
function _tip(t){
  var chip = _attrs({"data-t": t, "data-g": "20:1"});
  _docL["mouseover"].forEach(function(f){ f({target: _target(".chip", chip)}); });
  return _els["tip"].innerHTML;
}
function _filterTo(t){
  var btn = _attrs({"data-filter-t": t, "data-filter-g": "20:1"});
  _gridL["click"].forEach(function(f){ f({target: _target("[data-filter-t]", btn)}); });
  return RCF.state().gics;
}
console.log(JSON.stringify(%s));
"""


def _run_board_js_probe(html, tmp_path, expr):
    """Run the board's script, then evaluate the JS expression ``expr`` and return it as JSON."""
    script = "\n".join(part.split("</script>")[0] for part in html.split("<script>")[1:])
    path = tmp_path / "board_probe.js"
    path.write_text(_NODE_STUB + _PROBE_HOOKS + script + (_PROBE_DRIVER % expr), encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def _tooltip_payload():
    meta = {"AAA": {"sector": "Technology", "industry": "Software - Application"},
            "BBB": {"sector": "Energy", "industry": "Unclassified"},
            "CCC": {"sector": "Unclassified", "industry": ""}}
    return _report_payload(clusters={"20": [_report_cluster(
        "1", mean=0.72, tickers=tuple(meta), significant=True, baseline_r=0.06, member_meta=meta)]})


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_chip_tooltip_names_the_industry_then_the_sector_then_says_none(tmp_path):
    from RevenueChaser.module_comovement import report

    html = report.render_comovement_dashboard(_tooltip_payload())
    tips = _run_board_js_probe(html, tmp_path, '{a: _tip("AAA"), b: _tip("BBB"), c: _tip("CCC")}')
    assert "Software - Application" in tips["a"] and "Technology" not in tips["a"]
    assert "Energy" in tips["b"] and "Unclassified" not in tips["b"]     # no usable industry: the sector
    assert "No industry" in tips["c"] and "GICS" not in tips["c"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_filter_to_this_industry_selects_the_industry_level(tmp_path):
    from RevenueChaser.module_comovement import report

    html = report.render_comovement_dashboard(_tooltip_payload())
    gics = _run_board_js_probe(html, tmp_path, '_filterTo("AAA")')
    assert gics == {"key": "Technology›Software - Application",
                    "label": "Software - Application", "level": 2}


# ── the "No shared move" card (S8 / decision D5) ─────────────────────────────
def _report_cluster(cid="1", label=None, mean=0.72, last=-3.4,
                    tickers=("AAA", "BBB"), significant=None, baseline_r=None,
                    stability=None, member_meta=None, persist=None):
    return {
        "id": cid, "label": label or f"Cluster {cid}", "n": len(tickers),
        "mean_corr": mean, "last_residual_pct": last,
        "significant": significant, "baseline_r": baseline_r,
        "stability": stability, "persist": persist,
        "members": [{"ticker": t, "name": t, "sector": "Financial Services",
                     "market_cap": 1e9, "themes": [],
                     **(member_meta or {}).get(t, {})} for t in tickers],
    }


def _mixed_significance_payload(**over):
    """One real group, one group at chance, one noise blob."""
    payload = _report_payload(clusters={"20": [
        _report_cluster("1", mean=0.72, tickers=("AAA", "BBB"),
                        significant=True, baseline_r=0.06),
        _report_cluster("2", mean=0.04, tickers=("CCC",),
                        significant=False, baseline_r=0.08),
        _report_cluster("3", mean=0.03, tickers=("DDD", "EEE"),
                        significant=False, baseline_r=0.07),
    ]})
    payload.update(over)
    return payload


def test_member_rows_flag_only_explicitly_non_significant_groups():
    from RevenueChaser.module_comovement import report

    rows = report._member_rows(_mixed_significance_payload(), applied=True)
    assert {r["ticker"]: r["no_shared"] for r in rows} == {
        "AAA": False, "BBB": False, "CCC": True, "DDD": True, "EEE": True}
    assert rows[0]["baseline_r"] == 0.06 and rows[0]["significant"] is True
    # A run persisted before S8 has no baseline: unknown, so its card stays.
    old = _report_payload(clusters={"20": [_report_cluster("1", significant=None)]})
    assert [r["no_shared"] for r in report._member_rows(old, applied=True)] == [False, False]
    # The merge is a residual-board feature: a raw board keeps its groups.
    raw = _mixed_significance_payload(benchmark="off", benchmark_applied=False)
    assert not any(r["no_shared"] for r in report._member_rows(raw, applied=False))


def test_report_has_the_merge_but_not_the_old_leftover_constant():
    from RevenueChaser.module_comovement import report

    html = report.render_comovement_dashboard(_mixed_significance_payload())
    assert "No shared move" in html
    assert ("These names did not move together more than the groups the same "
            "method finds in shuffled, pure-noise returns.") in html
    assert "LEFTOVER_R" not in html and "hiddenLeftover" not in html


_NODE_STUB = r"""
var _els = {};
function _el(id){
  var attrs = {};
  return {id: id || "", innerHTML: "", textContent: "", value: "", className: "",
    title: "", style: {}, dataset: {}, options: {length: 0}, hidden: false, checked: false,
    classList: {add: function(){}, remove: function(){}, toggle: function(){},
                contains: function(){ return false; }},
    setAttribute: function(k, v){ attrs[k] = String(v); },
    getAttribute: function(k){ return k in attrs ? attrs[k] : null; },
    removeAttribute: function(k){ delete attrs[k]; },
    appendChild: function(){}, addEventListener: function(){},
    querySelector: function(sel){ return _q(sel); },
    querySelectorAll: function(){ return []; }, focus: function(){},
    closest: function(){ return null; }, contains: function(){ return false; }};
}
function _q(sel){
  // "#id" selectors resolve to the shared element (the board reads them back).
  var m = /^#([\w-]+)$/.exec(sel || "");
  return m ? document.getElementById(m[1]) : (_els["q:" + sel] = _els["q:" + sel] || _el(""));
}
globalThis.document = {
  getElementById: function(id){ if(!_els[id]) _els[id] = _el(id); return _els[id]; },
  createElement: function(){ return _el(""); },
  querySelector: _q,
  querySelectorAll: function(){ return []; },
  addEventListener: function(){},
  body: {classList: {add: function(){}, remove: function(){}}}
};
globalThis.location = {hash: "", search: "", pathname: "/"};
globalThis.history = {replaceState: function(){}};
"""


def _run_board_js(html, tmp_path, filters=None):
    """Run the board's own script against a DOM stub and read the grid back.

    ``filters`` is a shared-filter state (rc_filters.js), passed the way a deep
    link does: in the URL hash.
    """
    # The shared filter component, then the board's own script.
    script = "\n".join(part.split("</script>")[0] for part in html.split("<script>")[1:])
    driver = ("\nconsole.log(JSON.stringify({grid: _els['grid'].innerHTML,"
              " count: _els['count'].textContent,"
              " oos: _els['oos'].hidden ? '' : _els['oos-txt'].textContent}));\n")
    path = tmp_path / "board.js"
    pre = ""
    if filters is not None:
        from urllib.parse import quote
        pre = f"globalThis.location.hash = {json.dumps('#f=' + quote(json.dumps(filters)))};\n"
    path.write_text(_NODE_STUB + pre + script + driver, encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_merges_non_significant_groups_into_one_card(tmp_path):
    # The merge lives in JS, so run the real script: a string assertion on the
    # HTML would pass with the bug still in place.
    from RevenueChaser.module_comovement import report

    got = _run_board_js(report.render_comovement_dashboard(_mixed_significance_payload()),
                        tmp_path)
    grid, count = got["grid"], got["count"]
    assert grid.count("No shared move beyond SPY") == 1
    assert "No shared move beyond SPY · 3 names" in grid
    assert ("These names did not move together more than the groups the same "
            "method finds in shuffled, pure-noise returns.") in grid
    assert "Cluster 1" in grid
    assert "Cluster 2" not in grid and "Cluster 3" not in grid   # merged away
    assert 'vs noise <span class="num">0.06</span>' in grid
    assert "vs noise" not in grid.split("No shared move")[1]     # no baseline there
    assert grid.count('class="card') == 2                        # real card + merged
    assert "CCC" in grid and "DDD" in grid and "EEE" in grid      # names stay visible
    assert count == "5 names in 2 groups"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_keeps_every_card_on_a_raw_board(tmp_path):
    from RevenueChaser.module_comovement import report

    payload = _mixed_significance_payload(benchmark="off", benchmark_applied=False)
    got = _run_board_js(report.render_comovement_dashboard(payload), tmp_path)
    assert "No shared move" not in got["grid"]
    assert got["grid"].count('class="card') == 3
    assert got["count"] == "5 names in 3 groups"


# ── stability badge + fringe chips (S9) ─────────────────────────────────────
def test_member_rows_carry_persistence_and_member_core():
    from RevenueChaser.module_comovement import report

    held = {"jaccard": 0.7, "new": False, "held": True, "oos_r": 0.2, "oos_base": 0.05}
    rows = report._member_rows(_report_payload(clusters={"20": [_report_cluster(
        "1", persist=held,
        member_meta={"AAA": {"core": True}, "BBB": {"core": False}})]}), applied=True)
    assert [r["persist"] for r in rows] == [held, held]
    assert [r["core"] for r in rows] == [True, False]
    assert all("stability" not in r for r in rows)
    # No lagged match: unknown, not "joined this quarter".
    old = report._member_rows(_report_payload(), applied=True)
    assert [r["core"] for r in old] == [None, None]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_dims_names_that_joined_this_quarter(tmp_path):
    from RevenueChaser.module_comovement import report

    payload = _report_payload(clusters={"20": [_report_cluster(
        "1", mean=0.72, tickers=("AAA", "BBB", "CCC"), significant=True,
        baseline_r=0.06,
        persist={"jaccard": 0.67, "new": False, "held": True,
                 "oos_r": 0.2, "oos_base": 0.05},
        member_meta={"AAA": {"core": True}, "BBB": {"core": True},
                     "CCC": {"core": False}})]})
    html = report.render_comovement_dashboard(payload)
    assert "opacity:.55" in html                     # the dimmed style ships with the board
    assert ".chip.fringe" in html

    grid = _run_board_js(html, tmp_path)["grid"]
    assert "stable " not in grid                     # the bootstrap badge is gone
    assert "Dashed = joined since a quarter ago" in grid
    assert grid.index("AAA") < grid.index("BBB") < grid.index("CCC")
    assert grid.count('class="chip fringe') == 1     # only the newcomer is dashed


def test_member_rows_carry_the_catchall_flag():
    from RevenueChaser.module_comovement import report

    big = _report_cluster("1", tickers=("AAA", "BBB"), stability=0.9)
    big["catchall"] = True
    small = _report_cluster("2", tickers=("CCC",))
    rows = report._member_rows(_report_payload(clusters={"20": [big, small]}),
                               applied=True)
    assert {r["ticker"]: r["catchall"] for r in rows} == {
        "AAA": True, "BBB": True, "CCC": False}
    # A payload without the key (older cache) reads "not a catch-all".
    old = report._member_rows(_report_payload(), applied=True)
    assert [r["catchall"] for r in old] == [False, False]


def _catchall_payload(**over):
    """A catch-all residual group (5 of 7 names) beside a real tight group."""
    big = _report_cluster(
        "1", mean=0.20, tickers=("AAA", "BBB", "CCC", "DDD", "EEE"),
        significant=True, baseline_r=0.05, stability=0.91,
        member_meta={"AAA": {"core": True, "stability": 0.95},
                     "BBB": {"core": True, "stability": 0.90},
                     "CCC": {"core": False, "stability": 0.30},
                     "DDD": {"core": True, "stability": 0.80},
                     "EEE": {"core": False, "stability": 0.10}})
    big["catchall"] = True
    real = _report_cluster(
        "2", mean=0.72, tickers=("FFF", "GGG"), significant=True,
        baseline_r=0.06, stability=0.82,
        member_meta={"FFF": {"core": True, "stability": 0.9},
                     "GGG": {"core": False, "stability": 0.4}})
    payload = _report_payload(clusters={"20": [big, real]})
    payload.update(over)
    return payload


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_catch_all_card_says_so_and_does_not_split_core_fringe(tmp_path):
    from RevenueChaser.module_comovement import report

    grid = _run_board_js(report.render_comovement_dashboard(_catchall_payload()),
                         tmp_path)["grid"]
    cards = grid.split('<div class="card')[1:]
    assert len(cards) == 2                                     # nothing hidden
    # Still sorted by mean: the real (0.72) card first, the catch-all second.
    real, blob = cards
    assert "Cluster 2" in real and "Cluster 1" in blob
    assert "catch-all: 71% of names · weak shared move, not a theme" in blob
    assert "fringe" not in blob                                # all chips plain
    for t in ("AAA", "BBB", "CCC", "DDD", "EEE"):
        assert t in blob
    # The ordinary card keeps its core / joined-this-quarter split.
    assert 'class="chip fringe' in real
    assert "catch-all" not in real


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_catch_all_line_is_residual_only(tmp_path):
    from RevenueChaser.module_comovement import report

    payload = _catchall_payload(benchmark="off", benchmark_applied=False)
    grid = _run_board_js(report.render_comovement_dashboard(payload), tmp_path)["grid"]
    assert "catch-all" not in grid                             # raw board: beta warning instead


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_omits_the_badge_when_stability_is_unknown(tmp_path):
    from RevenueChaser.module_comovement import report

    payload = _report_payload(clusters={"20": [_report_cluster(
        "1", mean=0.72, tickers=("AAA", "BBB"), significant=True, baseline_r=0.06)]})
    grid = _run_board_js(report.render_comovement_dashboard(payload), tmp_path)["grid"]
    assert "stable " not in grid
    assert "fringe" not in grid


# ── out-of-sample persistence (review 2026-10-01 §1) ────────────────────────
def test_fetch_persists_the_lagged_test_and_load_payload_returns_it(rawstore_tmp_db):
    ret = _blocky_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(4,), persist=True)
    block = {f"T{i:02d}" for i in range(8)}
    card = next(c for c in p["clusters"]["4"]
                if block <= {m["ticker"] for m in c["members"]})
    # The block is one factor over the whole history: it was a group a quarter
    # ago too, and it held on the quarter the lagged clustering never saw.
    assert card["persist"]["new"] is False
    assert card["persist"]["held"] is True
    assert card["persist"]["jaccard"] >= config.PERSIST_MIN_JACCARD
    assert p["oos"]["4"]["total"] >= 1
    assert rawstore.count("comov_lag") == len(tickers)
    loaded = engine.load_payload()
    lc = next(c for c in loaded["clusters"]["4"]
              if block <= {m["ticker"] for m in c["members"]})
    assert lc["persist"] == pytest.approx(card["persist"])
    assert loaded["oos"] == p["oos"]


def test_refresh_rematches_against_the_persisted_lag_without_reclustering(
        rawstore_tmp_db, monkeypatch):
    ret = _blocky_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    fetched = engine.run(no_fetch=True, tickers=tickers, cuts=(4,), persist=True)

    def _boom(*a, **k):
        raise AssertionError("a Refresh must not re-run the lagged clustering")
    monkeypatch.setattr(engine.persistence, "lagged_test", _boom)
    refreshed = engine.run(no_fetch=True, tickers=tickers, cuts=(4,), persist=False)
    assert refreshed["oos"] == fetched["oos"]
    key = lambda c: (c["n"], c["mean_corr"])  # noqa: E731
    assert ({key(c): c["persist"] for c in refreshed["clusters"]["4"]}
            == {key(c): c["persist"] for c in fetched["clusters"]["4"]})


def test_refresh_without_a_persisted_lag_has_no_persistence_verdict(rawstore_tmp_db):
    ret = _blocky_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(4,), persist=False)
    assert p["oos"] == {}
    assert all(c["persist"] is None for c in p["clusters"]["4"])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_shows_the_persistence_badges_and_the_oos_line(tmp_path):
    from RevenueChaser.module_comovement import report

    held = {"jaccard": 0.72, "new": False, "held": True, "oos_r": 0.21, "oos_base": 0.05}
    broke = {"jaccard": 0.6, "new": False, "held": False, "oos_r": 0.01, "oos_base": 0.05}
    new = {"jaccard": 0.2, "new": True, "held": None, "oos_r": None, "oos_base": None}
    payload = _report_payload(oos={"20": {"held": 79, "total": 80}}, clusters={"20": [
        _report_cluster("1", mean=0.8, tickers=("AAA", "BBB"), significant=True,
                        baseline_r=0.06, persist=held),
        _report_cluster("2", mean=0.7, tickers=("CCC", "DDD"), significant=True,
                        baseline_r=0.06, persist=broke),
        _report_cluster("3", mean=0.6, tickers=("EEE", "FFF"), significant=True,
                        baseline_r=0.06, persist=new),
        _report_cluster("4", mean=0.5, tickers=("GGG", "HHH"), significant=True,
                        baseline_r=0.06),
    ]})
    got = _run_board_js(report.render_comovement_dashboard(payload), tmp_path)
    cards = got["grid"].split('<div class="card')[1:]
    assert "✓ held out-of-sample" in cards[0]
    assert "72% same names as a quarter ago · last quarter r 0.21 vs random names 0.05" in cards[0]
    assert "✗ did not hold out-of-sample" in cards[1]
    assert "60% same names as a quarter ago" in cards[1]
    assert "new this quarter" in cards[2]
    assert "out-of-sample" not in cards[3] and "new this quarter" not in cards[3]
    assert got["oos"] == ("Out-of-sample: 79 of 80 groups formed a quarter ago still "
                          "moved together over the last quarter (vs random names).")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_js_hides_the_oos_line_without_a_lagged_test(tmp_path):
    from RevenueChaser.module_comovement import report

    got = _run_board_js(report.render_comovement_dashboard(_report_payload()), tmp_path)
    assert got["oos"] == ""


# ── payload contract for the shared filters + "About this data" (plan S1/S5) ─
def test_members_carry_the_spine_contract_and_the_run_states_its_funnel(rawstore_tmp_db):
    ret = _two_group_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(2,), persist=True)
    m = p["clusters"]["2"][0]["members"][0]
    assert m["gics"] == ["Technology", "Semiconductors"]            # sector, industry: two levels (#266)
    assert m["sector"] == "Technology" and m["industry"] == "Semiconductors"
    assert "industry_group" not in m and "sub_industry" not in m
    assert m["theme_ids"] == [] and "index" in m and m["name"]
    f = p["funnel"]
    assert f["scanned_total"] == len(tickers) and f["clustered"] == p["eligible"]
    assert f["clustered"] == (f["classification_total"] - f["no_prices"]
                              - f["short_history"] - f["flat"])
    assert p["window_start"] and p["window_start"] < p["window_end"]
    assert p["source"]["table"] == "prices.duckdb → price_bars_daily + price_actions"
    assert p["verdicts_from"] is None
    loaded = engine.load_payload()
    assert loaded["funnel"]["clustered"] == f["clustered"]         # survives a reload
    assert loaded["window_start"] == p["window_start"]
    assert loaded["clusters"]["2"][0]["members"][0]["gics"][0] == "Technology"


def test_refresh_on_newer_prices_says_whose_verdicts_it_shows(rawstore_tmp_db):
    ret = _blocky_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret.iloc[:-5])
    fetched = engine.run(no_fetch=True, tickers=tickers, cuts=(4,), persist=True)
    _seed_ohlc(ret)                          # a screener scan adds 5 new sessions
    refreshed = engine.run(no_fetch=True, tickers=tickers, cuts=(4,), persist=False)
    assert refreshed["prices_through"] > fetched["prices_through"]
    assert refreshed["verdicts_from"] == fetched["prices_through"]


# ── shared filters on a group board (decision Q1: select groups, dim members) ─
def _two_sector_payload():
    return _report_payload(clusters={"20": [
        _report_cluster("1", mean=0.72, tickers=("FIN1", "TEC1", "TEC2"),
                        significant=True, baseline_r=0.06,
                        member_meta={"FIN1": {"sector": "Financial Services"},
                                     "TEC1": {"sector": "Technology"},
                                     "TEC2": {"sector": "Technology"}}),
        _report_cluster("2", mean=0.60, tickers=("TEC3", "TEC4"),
                        significant=True, baseline_r=0.06,
                        member_meta={"TEC3": {"sector": "Technology"},
                                     "TEC4": {"sector": "Technology"}}),
    ]})


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_filters_select_groups_and_dim_members_but_keep_whole_group_numbers(tmp_path):
    from RevenueChaser.module_comovement import report

    f = {"v": 1, "index": "all", "mc": {"b": [], "min": None, "max": None},
         "gics": {"key": "Financial Services", "label": "Financial Services", "level": 1},
         "theme": None, "earn": "any"}
    got = _run_board_js(report.render_comovement_dashboard(_two_sector_payload()),
                        tmp_path, filters=f)
    grid = got["grid"]
    assert "Cluster 1" in grid and "Cluster 2" not in grid     # group selected / dropped
    assert "1 of 3 match filters" in grid
    assert "3 names" in grid                                   # n is still the whole group
    assert grid.count('class="chip dim') == 2                  # TEC1, TEC2 dimmed, not removed
    assert "Numbers are for the whole group" in grid
    assert got["count"] == "1 name in 1 group"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_no_filters_shows_every_group_undimmed(tmp_path):
    from RevenueChaser.module_comovement import report

    got = _run_board_js(report.render_comovement_dashboard(_two_sector_payload()), tmp_path)
    assert "match filters" not in got["grid"] and "chip dim" not in got["grid"]
    assert got["count"] == "5 names in 2 groups"


# ── shared funnel steps (additive next to the legacy ``funnel`` dict) ────────
def _assert_steps_match_funnel(p):
    from RevenueChaser.core import funnel as funnel_mod

    steps = p["funnel_steps"]
    assert funnel_mod.validate(steps) == []
    by = {s["key"]: s for s in steps}
    f = p["funnel"]
    # R4 (#87): the type filter's three rows follow ``no_market_cap``; the seeded
    # names have no identity-master row, so the "unknown type" note is there too.
    assert [s["key"] for s in steps] == [
        "scanned", "below_floor", "no_market_cap", "funds_etfs", "spacs",
        "other_securities", "unknown_type", "no_prices", "listed_recently",
        "missing_bars", "flat", "clustered"]
    assert by["scanned"]["count"] == f["scanned_total"]
    assert by["below_floor"]["count"] == f["below_floor"]
    assert by["no_market_cap"]["count"] == f["no_market_cap"]
    assert by["no_prices"]["count"] == f["no_prices"]
    # the legacy ``short_history`` count is the sum of the two split steps
    assert by["listed_recently"]["count"] + by["missing_bars"]["count"] == f["short_history"]
    assert by["flat"]["count"] == f["flat"]
    assert by["clustered"]["count"] == f["clustered"] == p["eligible"]
    assert by["below_floor"]["by_design"] is True and by["flat"]["by_design"] is True
    assert by["listed_recently"]["by_design"] is True and by["listed_recently"]["fix"] is None
    for k in ("no_market_cap", "no_prices", "missing_bars"):
        assert by[k]["by_design"] is False
    assert by["no_market_cap"]["fix"]["route"] == "/run/universe-stats"
    # A Fetch downloads no prices since #379, so these drops carry no Fetch button (review B RB-8)
    assert by["no_prices"]["fix"] is None and by["missing_bars"]["fix"] is None
    assert str(config.MIN_OBS) in by["missing_bars"]["label"]
    assert str(config.WINDOW) in by["missing_bars"]["label"]


def test_fetch_path_emits_valid_funnel_steps(rawstore_tmp_db):
    ret = _two_group_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(2,), persist=True)
    _assert_steps_match_funnel(p)


def test_refresh_path_and_reload_emit_valid_funnel_steps(rawstore_tmp_db):
    ret = _two_group_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    engine.run(no_fetch=True, tickers=tickers, cuts=(2,), persist=True)
    refreshed = engine.run(no_fetch=True, tickers=tickers, cuts=(2,), persist=False)
    _assert_steps_match_funnel(refreshed)
    loaded = engine.load_payload()
    _assert_steps_match_funnel(loaded)


def test_empty_payload_has_no_funnel_steps(rawstore_tmp_db):
    assert engine.load_payload()["funnel_steps"] == []


def test_legacy_funnel_without_drop_keys_gives_no_steps():
    assert engine.funnel_steps({"scanned_total": 5, "clustered": 3}) == []


def test_floor_step_label_states_the_floor_the_run_used():
    # The floor becomes editable (#40); the label must follow the run's floor.
    f = {"scanned_total": 10, "below_floor": 4, "no_market_cap": 1, "no_prices": 0,
         "short_history": 1, "flat": 0, "clustered": 4, "floor": 1_000_000_000.0}
    labels = {s["key"]: s["label"] for s in engine.funnel_steps(f)}
    assert labels["below_floor"] == "Below the $1,000M classification floor"


def test_funnel_steps_only_change_the_about_funnel_rows():
    """The steps drive the "Universe → clustered names" rows (#40: the split
    short-history steps read as on the Settings card); nothing else on the board."""
    from RevenueChaser.module_comovement import report

    base = _report_payload()
    with_steps = {**base, "funnel_steps": [{"key": "scanned", "label": "x",
                                            "count": 1, "kind": "start"}]}
    strip = lambda h: re.sub(r'<div id="about-funnel">.*?</div></div>', "", h, flags=re.S)
    a, b = (report.render_comovement_dashboard(x) for x in (base, with_steps))
    assert a != b and '<span>x</span><span class="num">1</span>' in b
    assert strip(a) == strip(b)


# ── window control, short-window lock, custom badge, effective settings (#40 B1) ──
def _settings_block(window=252, custom=False, **over):
    short = window < config.SHORT_WINDOW
    s = {"window": window, "default_window": 252,
         "window_options": list(config.WINDOW_OPTIONS),
         "min_obs": 126, "min_periods": 60, "oos_holdout": 63, "oos_min_obs": 40,
         "winsor_k": 5.0, "bench_stale_sessions": 2, "floor": 3.0e8,
         "short_window": short, "horizon_locked": short, "custom": custom,
         "overridden": ["MIN_OBS"] if custom else []}
    s.update(over)
    return s


def _win_payload(window=252, custom=False, settings_over=None, **over):
    short = window < config.SHORT_WINDOW
    p = _report_payload(
        window=window, horizon=1 if short else 5,
        settings=_settings_block(window, custom, **(settings_over or {})),
        horizon_locked=short,
        short_window_note=(f"Short window ({window} sessions): noisy, exploratory. "
                           "Horizon is fixed at 1 day.") if short else None,
        oos_note=(f"Not evaluated: a {window}-session window is too short for the "
                  "out-of-sample persistence test.") if short else None)
    p.update(over)
    return p


def _render(**kw):
    from RevenueChaser.module_comovement import report
    return report.render_comovement_dashboard(_win_payload(**kw))


def test_window_control_lists_the_six_options_and_marks_the_current_one():
    html = _render(window=126)
    group = re.search(r'<div class="rcf-seg" role="radiogroup"[^>]*data-group="window">'
                      r'(.*?)</div>', html, re.S)
    assert group, "no Window radiogroup"
    labels = re.findall(r'data-v="(\d+)"[^>]*>([^<]+)', group.group(1))
    assert labels == [("21", "1m"), ("42", "2m"), ("126", "6m"), ("189", "9m"),
                      ("252", "1y"), ("378", "1.5y")]
    assert re.search(r'data-v="126" aria-checked="true"', group.group(1))
    assert group.group(1).count('aria-checked="true"') == 1
    assert 'title="126 trading sessions"' in group.group(1)   # day counts in the tooltip
    assert 'title="378 trading sessions"' in group.group(1)


def test_window_marker_follows_the_payload_not_the_default():
    for w, label in ((42, "2m"), (378, "1.5y")):
        html = _render(window=w)
        assert re.search(rf'data-v="{w}" aria-checked="true"[^>]*>{re.escape(label)}', html)


def test_a_short_window_locks_the_horizon_and_says_why():
    html = _render(window=42)
    hz = re.search(r'data-group="horizon">(.*?)</div>', html, re.S).group(1)
    for n in (3, 5, 10):
        assert re.search(rf'data-v="{n}"[^>]*aria-disabled="true"', hz), n
    assert not re.search(r'data-v="1"[^>]*aria-disabled="true"', hz)
    assert re.search(r'data-v="1" aria-checked="true"', hz)
    assert ("Short windows use 1-day returns only (too few independent observations)"
            in html)
    assert 'title="Short windows use 1-day returns only' in hz


def test_a_normal_window_leaves_the_horizon_open():
    html = _render(window=252)
    hz = re.search(r'data-group="horizon">(.*?)</div>', html, re.S).group(1)
    assert "aria-disabled" not in hz
    assert "Short windows use 1-day returns only" not in html


def test_the_short_window_note_and_the_oos_note_render_escaped():
    html = _render(window=21, short_window_note="Short <b>x</b> & noisy",
                   oos_note="OOS <i>off</i>")
    assert "Short &lt;b&gt;x&lt;/b&gt; &amp; noisy" in html
    assert "OOS &lt;i&gt;off&lt;/i&gt;" in html
    assert "<b>x</b> & noisy" not in html
    plain = _render(window=21)
    assert "noisy, exploratory" in plain
    assert "too short for the out-of-sample persistence test" in plain
    assert 'id="oos-note"' in plain
    normal = _render(window=252)
    assert "noisy, exploratory" not in normal and 'id="oos-note"' not in normal


def test_a_non_default_window_leaves_no_stale_config_text():
    """126 sessions, MIN_OBS 100, holdout 84: none of the 252 / 126 / 63 defaults may
    be printed as if they were the effective values."""
    html = _render(window=126, custom=True, settings_over={
        "min_obs": 100, "oos_holdout": 84, "oos_min_obs": 53},
        oos_start="2026-06-01", funnel={
            "scanned_total": 100, "classification_total": 80, "below_floor": 20,
            "no_prices": 0, "short_history": 5, "flat": 0, "clustered": 75})
    assert "(126 trading sessions)" in html and "(252 trading sessions)" not in html
    assert "last 126 sessions" in html and "last 252 sessions" not in html
    assert "126 ÷ n" in html and "252 ÷ n" not in html
    assert "<b>84 sessions</b>" in html and "<b>63 sessions</b>" not in html
    assert "(&lt; 100 of 126 sessions)" in html
    assert "(&lt; 126 of 252 sessions)" not in html
    assert "over 126 sessions" in html
    assert "84 sessions they never saw" in html


def test_the_badge_and_reset_appear_only_when_settings_are_custom():
    custom = _render(window=252, custom=True)
    plain = _render(window=252, custom=False)
    for needle in ('id="custom-badge"', "Custom settings", 'id="reset-settings"',
                   "Reset to defaults"):
        assert needle in custom, needle
        assert needle not in plain, needle
    # AGENTS.md section 6: the top bar is title + subtitle + Last sync only.
    top = re.search(r'<header class="topbar">.*?</header>', custom, re.S).group(0)
    assert "Custom settings" not in top and "Reset to defaults" not in top
    row = re.search(r'<section class="rcf-boardrow".*?</section>', custom, re.S).group(0)
    assert 'id="custom-badge"' in row and 'id="reset-settings"' in row


def test_a_legacy_payload_without_settings_still_renders_the_defaults():
    from RevenueChaser.module_comovement import report
    html = report.render_comovement_dashboard(_report_payload())
    assert f"({config.WINDOW} trading sessions)" in html
    assert re.search(rf'data-v="{config.WINDOW}" aria-checked="true"', html)
    assert "Custom settings" not in html


def test_the_funnel_shows_the_split_steps():
    steps = [
        {"key": "scanned", "label": "Scanned universe", "count": 100, "kind": "start",
         "fix": None},
        {"key": "below_floor", "label": "Below the $300M classification floor",
         "count": 20, "kind": "drop", "by_design": True, "fix": None},
        {"key": "listed_recently",
         "label": "Listed recently (first price inside the 252-session window)",
         "count": 6, "kind": "drop", "by_design": True, "fix": None},
        {"key": "missing_bars", "label": "Missing price bars (< 126 of 252 sessions)",
         "count": 4, "kind": "drop", "by_design": False,
         "fix": {"route": "/run/module_comovement", "label": "Fetch prices"}},
        {"key": "clustered", "label": "Clustered", "count": 70, "kind": "result",
         "fix": None},
    ]
    html = _render(window=252, funnel_steps=steps, funnel={"clustered": 70})
    body = re.search(r'<div id="about-funnel">.*?</div></div>', html, re.S).group(0)
    assert "Listed recently" in body and "by design" in body
    assert "Missing price bars" in body and "Fetch prices" in body
    assert "−6" in body and "−4" in body
    assert "Short history" not in body


# JS: the board URL builder and the reset handler run under node.
_FETCH_STUB = r"""
var FETCHES = [], RESP = [];
globalThis.fetch = function(url, opts){
  FETCHES.push({url: url, body: opts && opts.body ? JSON.parse(opts.body) : null});
  var r = RESP.shift() || {status: 200, body: {ok: true, job: {status: "running"}}};
  return Promise.resolve({status: r.status, ok: r.status < 400,
                          json: function(){ return Promise.resolve(r.body); }});
};
"""


def _run_board_snippet(html, tmp_path, driver):
    script = "\n".join(part.split("</script>")[0] for part in html.split("<script>")[1:])
    path = tmp_path / "snippet.js"
    path.write_text(_NODE_STUB + _FETCH_STUB + script + "\n" + driver, encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_horizon_market_and_window_changes_carry_the_other_two(tmp_path):
    html = _render(window=126)
    got = _run_board_snippet(html, tmp_path, r"""
var out = {};
setHorizon(10); out.horizon = location.search;
setMarket("qqq"); out.market = location.search;
setWindow(378);  out.window = location.search;
console.log(JSON.stringify(out));
""")
    assert got["horizon"] == "?refresh=1&window=126&horizon=10&benchmark=spy", (
        f"a Horizon change must keep the window: {got['horizon']}")
    assert got["market"] == "?refresh=1&window=126&horizon=5&benchmark=qqq", got["market"]
    assert got["window"] == "?refresh=1&window=378&horizon=5&benchmark=spy", got["window"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_clicking_the_current_window_does_not_reload(tmp_path):
    got = _run_board_snippet(_render(window=126), tmp_path, r"""
setWindow(126);
console.log(JSON.stringify({search: location.search}));
""")
    assert got["search"] == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_reset_posts_reset_true_then_polls_and_reloads_on_done(tmp_path):
    html = _render(window=252, custom=True)
    assert "function settingsJobVerdict(" in html          # the shared verdict, not a fork
    got = _run_board_snippet(html, tmp_path, r"""
RESP.push({status: 202, body: {ok: true, job_id: "R1", values: {}}});
RESP.push({status: 200, body: {ok: true, job: {status: "done", message: "ok"}}});
var btn = document.getElementById("reset-settings");
resetSettings(btn);
setTimeout(function(){
  console.log(JSON.stringify({f: FETCHES, search: location.search}));
  process.exit(0);
}, 60);
""")
    assert got["f"][0]["url"] == "/api/settings/board/module_comovement"
    assert got["f"][0]["body"] == {"reset": True}
    assert got["f"][1]["url"] == "/api/job/R1"
    assert got["search"] == "?refresh=1"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_a_failed_reset_does_not_reload(tmp_path):
    got = _run_board_snippet(_render(window=252, custom=True), tmp_path, r"""
RESP.push({status: 400, body: {ok: false, error: "nope", messages: ["nope"]}});
resetSettings(document.getElementById("reset-settings"));
setTimeout(function(){
  console.log(JSON.stringify({search: location.search,
    msg: document.getElementById("reset-msg").textContent}));
  process.exit(0);
}, 40);
""")
    assert got["search"] == "" and "nope" in got["msg"]


# ── security-type filter rows and the unknown-type note (#87, Identity R4) ───
_NOTE_LINK = {"tab": "module_settings", "anchor": "identity", "label": "Settings → Identity"}


def _typed_funnel(**over):
    """20 scanned → 15 at the floor → 7 operating → 4 clustered, every step counted."""
    f = {"scanned_total": 20, "below_floor": 4, "no_market_cap": 1, "floor_total": 15,
         "funds_etfs": 5, "spacs": 2, "other_securities": 1, "classification_total": 7,
         "unknown_type": 0, "no_prices": 1, "short_history": 2, "flat": 0,
         "clustered": 4, "floor": 3e8}
    f.update(over)
    return f


def test_funnel_steps_state_the_type_filter_right_after_no_market_cap():
    from RevenueChaser.core import funnel as funnel_mod
    from RevenueChaser.core import universe as universe_mod

    steps = engine.funnel_steps(_typed_funnel())
    assert [s["key"] for s in steps] == [
        "scanned", "below_floor", "no_market_cap", "funds_etfs", "spacs",
        "other_securities", "no_prices", "short_history", "flat", "clustered"]
    assert funnel_mod.validate(steps) == []
    by = {s["key"]: s for s in steps}
    for key, label, _types in universe_mod.TYPE_GROUPS:
        assert by[key]["label"] == label and by[key]["kind"] == "drop"
        assert by[key]["by_design"] is True and by[key]["fix"] is None
    assert (by["funds_etfs"]["count"], by["spacs"]["count"],
            by["other_securities"]["count"]) == (5, 2, 1)


def test_an_unknown_type_note_follows_the_type_rows_and_is_not_counted():
    from RevenueChaser.core import funnel as funnel_mod

    steps = engine.funnel_steps(_typed_funnel(unknown_type=7))
    assert [s["key"] for s in steps][3:8] == [
        "funds_etfs", "spacs", "other_securities", "unknown_type", "no_prices"]
    note = next(s for s in steps if s["key"] == "unknown_type")
    assert note["kind"] == "note" and note["count"] == 7
    assert note["label"] == "Unknown security type — kept"
    assert note["link"] == _NOTE_LINK and note["fix"] is None
    assert funnel_mod.validate(steps) == []          # 20 - drops == 4 with the 7 ignored


def test_no_note_when_every_kept_name_is_typed_or_the_count_is_absent():
    for f in (_typed_funnel(unknown_type=0), {k: v for k, v in _typed_funnel().items()
                                              if k != "unknown_type"}):
        assert "unknown_type" not in [s["key"] for s in engine.funnel_steps(f)]


def test_a_funnel_persisted_before_the_type_filter_has_no_type_rows_and_no_note():
    from RevenueChaser.core import funnel as funnel_mod

    old = {"scanned_total": 10, "below_floor": 1, "no_market_cap": 0, "no_prices": 0,
           "short_history": 2, "flat": 0, "clustered": 7, "floor": 3e8,
           "classification_total": 9}
    steps = engine.funnel_steps(old)
    assert [s["key"] for s in steps] == ["scanned", "below_floor", "no_market_cap",
                                          "no_prices", "short_history", "flat", "clustered"]
    assert funnel_mod.validate(steps) == []
    # all three group keys are needed: a partial funnel is treated as an old one
    partial = engine.funnel_steps({**old, "funds_etfs": 3, "spacs": None,
                                   "other_securities": 1, "unknown_type": 4})
    assert [s["key"] for s in partial] == [s["key"] for s in steps]


def test_the_required_step_keys_do_not_include_the_new_type_keys():
    assert engine._STEP_KEYS == ("scanned_total", "below_floor", "no_market_cap",
                                 "no_prices", "short_history", "flat", "clustered")


def _seed_master_types(types):
    rawstore.ensure_table("security_master")
    rawstore.write_rows("security_master", [
        {"ticker": t, "security_type": ty} for t, ty in types.items()])


def _typed_run(rawstore_tmp_db, master):
    """6 clustered-able names + FUNDX in the spine; ``master`` is the typed rows."""
    ret = _two_group_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers + ["FUNDX"])
    _seed_ohlc(ret)
    _seed_master_types(master)
    return tickers


def test_run_drops_a_fund_from_the_clusters_and_the_funnel_says_so(rawstore_tmp_db):
    from RevenueChaser.core import funnel as funnel_mod

    tickers = _typed_run(rawstore_tmp_db, {"FUNDX": "cef"})        # the six are untyped
    p = engine.run(no_fetch=True, cuts=(2,), persist=True)         # universe from the spine
    members = {m["ticker"] for c in p["clusters"]["2"] for m in c["members"]}
    assert members == set(tickers) and "FUNDX" not in members
    f = p["funnel"]
    assert f["scanned_total"] == 7 and f["floor_total"] == 7
    assert (f["funds_etfs"], f["spacs"], f["other_securities"]) == (1, 0, 0)
    assert f["classification_total"] == 6 == f["unknown_type"]
    assert funnel_mod.validate(p["funnel_steps"]) == []
    by = {s["key"]: s for s in p["funnel_steps"]}
    assert by["funds_etfs"]["count"] == 1 and by["unknown_type"]["count"] == 6
    assert by["unknown_type"]["kind"] == "note"
    # the persisted run carries the new counts, so a reload rebuilds the same steps
    stored = engine._loads_or_empty(rawstore.query(
        "SELECT funnel_json FROM comov_runs")[0]["funnel_json"])
    assert stored["funds_etfs"] == 1 and stored["unknown_type"] == 6
    assert stored["floor_total"] == 7
    loaded = engine.load_payload()
    assert funnel_mod.validate(loaded["funnel_steps"]) == []
    assert loaded["funnel_steps"] == p["funnel_steps"]


def test_a_fully_typed_universe_has_no_unknown_type_note(rawstore_tmp_db):
    master = {t: "common" for t in ("A1", "A2", "A3", "B1", "B2", "B3")}
    _typed_run(rawstore_tmp_db, {**master, "FUNDX": "etp"})
    p = engine.run(no_fetch=True, cuts=(2,), persist=False)
    keys = [s["key"] for s in p["funnel_steps"]]
    assert "funds_etfs" in keys and "unknown_type" not in keys
    assert p["funnel"]["unknown_type"] == 0
    assert p["eligible"] == 6


# ── the funnel note and the header line on the board (#87) ───────────────────
def _note_steps(label="Unknown security type — kept"):
    return [
        {"key": "scanned", "label": "Scanned universe", "count": 100, "kind": "start",
         "fix": None},
        {"key": "funds_etfs", "label": "Funds / ETFs", "count": 10, "kind": "drop",
         "by_design": True, "fix": None},
        {"key": "unknown_type", "label": label, "count": 7, "kind": "note", "fix": None,
         "link": _NOTE_LINK},
        {"key": "clustered", "label": "Clustered", "count": 90, "kind": "result",
         "fix": None},
    ]


def _about_funnel(html):
    return re.search(r'<div id="about-funnel">.*?</div></div>', html, re.S).group(0)


def test_the_funnel_renders_a_note_row_with_a_settings_link_and_a_positive_count():
    body = _about_funnel(_render(funnel_steps=_note_steps(), funnel={"clustered": 90}))
    row = re.search(r'<div class="row note">.*?</div>', body, re.S).group(0)
    assert "⚠" in row and "Unknown security type — kept" in row
    assert ('<span class="meta">— see <a href="/#module_settings" target="_top" '
            "onclick=\"return rcOpenTab('module_settings', 'identity')\">"
            'Settings → Identity</a></span>') in row
    assert row.endswith('<span class="num">7</span></div>')     # positive, not "−7"
    assert "−7" not in body
    assert "−10" in body and "Funds / ETFs" in body             # the type drop is a normal row
    assert 'class="row total"' in body                          # the result row is unchanged


def test_the_note_label_is_escaped():
    body = _about_funnel(_render(funnel_steps=_note_steps("<b>x</b> & more"),
                                 funnel={"clustered": 90}))
    assert "&lt;b&gt;x&lt;/b&gt; &amp; more" in body and "<b>x</b>" not in body


def test_a_note_without_a_link_still_renders():
    steps = _note_steps()
    del steps[2]["link"]
    body = _about_funnel(_render(funnel_steps=steps, funnel={"clustered": 90}))
    assert '<div class="row note">' in body and "see <a" not in body


def test_the_board_has_no_identity_refresh_button():
    """Long store-writing jobs start only from Settings, never from a board."""
    for html in (_render(funnel_steps=_note_steps(), funnel={"clustered": 90}), _render()):
        assert "/run/identity" not in html
        assert "Refresh identity" not in html


def test_the_note_row_has_its_own_style():
    assert ".funnel .row.note" in _render()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_the_settings_link_opens_the_tab_in_the_parent_dashboard(tmp_path):
    html = _render(funnel_steps=_note_steps(), funnel={"clustered": 90})
    assert "function rcOpenTab(" in html
    got = _run_board_snippet(html, tmp_path, r"""
var out = {}, shown = [], scrolled = [];
function frame(attrs, doc){
  return {attrs: attrs, contentDocument: doc,
          getAttribute: function(k){ return this.attrs[k] || null; },
          setAttribute: function(k, v){ this.attrs[k] = v; }};
}
function dashboard(f){
  return {showTab: function(k){ shown.push(k);
                                if(f && !f.attrs.src) f.attrs.src = f.attrs["data-src"]; },
          document: {querySelector: function(sel){
            out.selector = sel; return f; }}};
}
globalThis.window = {};
// the Settings frame was never opened: it loads straight onto the anchor
var cold = frame({"data-src": "/view/module_settings"}, null);
globalThis.parent = dashboard(cold);
out.framed = rcOpenTab("module_settings", "identity");
out.cold_src = cold.attrs.src;
// the Settings frame is loaded: scroll to the card
var card = {scrollIntoView: function(){ scrolled.push("identity"); }};
var warm = frame({"data-src": "/view/module_settings", src: "/view/module_settings"},
                 {getElementById: function(id){ return id === "identity" ? card : null; }});
globalThis.parent = dashboard(warm);
out.warm = rcOpenTab("module_settings", "identity");
out.warm_src = warm.attrs.src;
globalThis.parent = globalThis.window;
out.top = rcOpenTab("module_settings", "identity");     // opened alone: follow the href
globalThis.parent = {};
out.nofn = rcOpenTab("module_settings", "identity");    // a parent without showTab
globalThis.parent = {showTab: function(){ throw new Error("boom"); }, document: null};
out.throws = rcOpenTab("module_settings", "");          // never breaks the click
out.shown = shown; out.scrolled = scrolled;
console.log(JSON.stringify(out));
""")
    assert got == {"framed": False, "cold_src": "/view/module_settings#identity",
                   "selector": '.frame[data-tab="module_settings"]',
                   "warm": False, "warm_src": "/view/module_settings",
                   "top": True, "nofn": True, "throws": True,
                   "shown": ["module_settings", "module_settings"],
                   "scrolled": ["identity"]}


def test_a_note_link_to_another_tab_or_with_an_unsafe_id():
    steps = _note_steps()
    steps[2]["link"] = {"tab": "module_value_chain", "anchor": "x", "label": "VC"}
    body = _about_funnel(_render(funnel_steps=steps, funnel={"clustered": 90}))
    assert ('<a href="/#module_value_chain" target="_top" '
            "onclick=\"return rcOpenTab('module_value_chain', 'x')\">VC</a>") in body
    steps[2]["link"] = {"tab": "x');alert(1);//", "label": "bad"}
    body = _about_funnel(_render(funnel_steps=steps, funnel={"clustered": 90}))
    assert "alert(1)" not in body and "see <a" not in body
    steps[2]["link"] = {"tab": "module_settings", "anchor": "a b'", "label": "S"}
    body = _about_funnel(_render(funnel_steps=steps, funnel={"clustered": 90}))
    assert "rcOpenTab('module_settings', '')" in body


def _universe_line(html):
    return re.search(r'<div class="universe" aria-label="Universe">.*?</div>', html, re.S).group(0)


def test_the_header_line_adds_the_operating_companies_step():
    line = _universe_line(_render(funnel={
        "scanned_total": 100, "floor_total": 80, "classification_total": 70,
        "funds_etfs": 8, "spacs": 1, "other_securities": 1, "clustered": 65}))
    parts = ['Scanned universe <b class="num">100</b> · all market caps',
             '≥ $300M <b class="num">80</b>',
             'operating companies <b class="num">70</b>',
             'clustered <b class="num">65</b>']
    at = [line.index(p) for p in parts]
    assert at == sorted(at), line
    assert line.count('<span class="arrow"') == 3          # four steps, three arrows


def test_an_old_payload_keeps_todays_header_line():
    html = _render(funnel={"scanned_total": 100, "classification_total": 80, "clustered": 75})
    line = _universe_line(html)
    assert '≥ $300M <b class="num">80</b>' in line
    assert "operating companies" not in line
    assert 'clustered <b class="num">75</b>' in line


# --- #117 (C15): the multi-day move is the last h-session return, not a mean of overlaps ---

def _price_path(n=40):
    idx = pd.bdate_range("2026-01-02", periods=n)
    base = 100 * np.exp(np.cumsum(np.linspace(0.001, 0.02, n)))
    return pd.DataFrame({"X": base, "Y": base * 2}, index=idx)


@pytest.mark.parametrize("h", [1, 3, 5, 10])
def test_last_move_is_the_horizon_session_return(h):
    prices = _price_path()
    ret, _ = returns.log_returns(prices, window=30, min_obs=5, horizon=h)
    got = engine._last_move_pct(ret, ["X", "Y"], h)
    want = float(np.log(prices["X"].iloc[-1] / prices["X"].iloc[-1 - h]) * 100.0)
    assert got == pytest.approx(want)


def test_last_move_h1_unchanged_and_ignores_older_rows():
    ret = pd.DataFrame({"X": [0.5, 0.5, 0.01], "Y": [0.5, 0.5, 0.03]})
    assert engine._last_move_pct(ret, ["X", "Y"], 1) == pytest.approx(2.0)
    assert engine._last_move_pct(ret, ["X", "Y"], 5) == pytest.approx(2.0)


def test_last_move_skips_members_without_a_latest_value():
    ret = pd.DataFrame({"X": [0.1, 0.02], "Y": [0.1, np.nan]})
    assert engine._last_move_pct(ret, ["X", "Y"], 3) == pytest.approx(2.0)
