"""Refresh (no_fetch) is pure: no store writes, no N x N, bounded payload; Fetch is locked (#115)."""
from __future__ import annotations

import pytest

from RevenueChaser.core import job_lock, rawstore
from RevenueChaser.module_relatedness import engine, store
from RevenueChaser.module_relatedness.tests.test_relatedness import (
    _prof, _seed_filings, _seed_universe)

TICKERS = [f"T{i:02d}" for i in range(12)]


@pytest.fixture
def seeded(rawstore_tmp_db, monkeypatch, tmp_path):
    _seed_universe(TICKERS)
    _seed_filings(TICKERS)
    monkeypatch.setattr(engine.config, "RELATEDNESS_JSON", tmp_path / "r.json")
    for t in TICKERS:
        store.save_profile(t, "cik-" + t, _prof(products=["gpu"], end_markets=["dc"]),
                           "k", "m")
    monkeypatch.setattr(engine.vc_engine, "fetch_corpus",
                        lambda *a, **k: {"no_fetch": True})
    return TICKERS


def _forbid_writes(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("Refresh must not write the store")
    for name in ("write_rows", "replace_all", "execute", "atomic", "delete"):
        monkeypatch.setattr(rawstore, name, boom)


def test_refresh_writes_nothing(seeded, monkeypatch):
    _forbid_writes(monkeypatch)
    payload = engine.run(no_fetch=True, tickers=seeded)
    assert payload["n"] == len(seeded)


def test_refresh_leaves_scores_and_registry_untouched(seeded):
    before = (rawstore.count("relatedness_scores"), rawstore.count("run_registry"))
    engine.run(no_fetch=True, tickers=seeded)
    assert (rawstore.count("relatedness_scores"), rawstore.count("run_registry")) == before


def test_fetch_still_persists_scores_and_registry(seeded, monkeypatch):
    monkeypatch.setattr(engine, "build_profiles",
                        lambda tickers, **k: store.load_profiles(tickers))
    engine.run(no_fetch=False, tickers=seeded)
    assert rawstore.count("relatedness_scores") > 0
    assert rawstore.get("run_registry", "relatedness_run")


def test_market_stats_reach_every_focal(seeded, monkeypatch):
    seen = {}
    monkeypatch.setattr(engine, "_attach_peer_market",
                        lambda peers: seen.setdefault("focals", set(peers)))
    payload = engine.run(no_fetch=True, tickers=seeded)
    assert len(seeded) > 1
    assert seen["focals"] == set(payload["peers"]) == set(seeded)


def _market_prices(n: int = 260, seed: int = 5):
    """Closes with a shared market factor, gaps, and a pair with too little overlap."""
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-02", periods=n, freq="B")
    market = rng.normal(0.0003, 0.010, n)
    common = rng.normal(0.0002, 0.008, n)

    def px(r):
        return 100.0 * np.exp(np.cumsum(r))

    df = pd.DataFrame({
        "NVDA": px(market + common + rng.normal(0, 0.002, n)),
        "AMD": px(market + common + rng.normal(0, 0.002, n)),
        "TSM": px(market + rng.normal(0, 0.006, n)),
        "ODD": px(rng.normal(0, 0.012, n)),
        "SPY": px(market + rng.normal(0, 0.001, n)),
    }, index=idx)
    df.loc[df.index[20:35], "AMD"] = np.nan           # a gap inside the window
    df.loc[df.index[:120], "NVDA"] = np.nan           # NVDA lists late ...
    df["SHORTY"] = px(rng.normal(0, 0.012, n))
    df.loc[df.index[151:], "SHORTY"] = np.nan         # ... SHORTY ends early: ~30 shared rows
    return df


def _market_peers():
    def e(peer):
        return {"peer": peer, "score": 1.0, "percentile": 100, "shared": []}
    return {
        "NVDA": [e("AMD"), e("TSM"), e("SHORTY"), e("GHOST"), e(None)],
        "AMD": [e("NVDA"), e("ODD")],
        "TSM": [e("NVDA"), e("AMD")],
    }


def test_peer_market_never_builds_a_correlation_matrix(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_comovement import ohlc as comov_ohlc
    from RevenueChaser.module_comovement import returns as comov_returns

    def boom(*a, **k):
        raise AssertionError("_attach_peer_market must stay pair-only, never N x N")

    monkeypatch.setattr(comov_returns, "correlation", boom)
    monkeypatch.setattr(comov_ohlc, "price_frame", lambda tickers, start=None: _market_prices())
    peers = _market_peers()
    engine._attach_peer_market(peers)
    amd = next(p for p in peers["NVDA"] if p["peer"] == "AMD")
    assert set(amd["move"]) == {1, 3, 5, 10} and "beyond_spy" in amd


def test_peer_market_computes_log_returns_once_per_horizon(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_comovement import config as comov_config
    from RevenueChaser.module_comovement import ohlc as comov_ohlc
    from RevenueChaser.module_comovement import returns as comov_returns

    assert comov_config.DEFAULT_HORIZON in comov_config.HORIZONS
    seen = []
    real = comov_returns.log_returns

    def counting(prices, *a, horizon=1, **k):
        seen.append(horizon)
        return real(prices, *a, horizon=horizon, **k)

    monkeypatch.setattr(comov_returns, "log_returns", counting)
    monkeypatch.setattr(comov_ohlc, "price_frame", lambda tickers, start=None: _market_prices())
    engine._attach_peer_market(_market_peers())
    assert sorted(seen) == sorted(comov_config.HORIZONS)


def test_peer_market_equals_the_full_matrix_values(rawstore_tmp_db, monkeypatch):
    import math
    from RevenueChaser.module_comovement import config as comov_config
    from RevenueChaser.module_comovement import ohlc as comov_ohlc
    from RevenueChaser.module_comovement import returns as comov_returns

    prices = _market_prices()
    monkeypatch.setattr(comov_ohlc, "price_frame", lambda tickers, start=None: prices)
    peers = _market_peers()
    engine._attach_peer_market(peers)

    # Reference: the full pandas matrices, looked up per pair.
    corr_by_h = {}
    for n in comov_config.HORIZONS:
        ret, _ = comov_returns.log_returns(prices, horizon=n)
        corr_by_h[n] = comov_returns.correlation(ret)
    ret1, _ = comov_returns.log_returns(prices, horizon=comov_config.DEFAULT_HORIZON)
    cols = [c for c in ret1.columns if c != "SPY"]
    resid = comov_returns.correlation(comov_returns.residualize(ret1[cols], ret1["SPY"]))

    def look(m, a, b):
        if a not in m.columns or b not in m.columns:
            return None
        v = float(m.loc[a, b])
        return None if math.isnan(v) else v

    n_move = n_spy = n_none = 0
    for focal, entries in peers.items():
        for p in entries:
            want_move = {n: look(corr_by_h[n], focal, p["peer"])
                         for n in comov_config.HORIZONS if n in corr_by_h}
            want_move = {n: v for n, v in want_move.items() if v is not None}
            got_move = p.get("move") or {}
            assert set(got_move) == set(want_move), (focal, p["peer"])
            for n, v in want_move.items():
                assert got_move[n] == pytest.approx(v, abs=1e-4)
            want_spy = look(resid, focal, p["peer"]) if focal in resid.columns else None
            if want_spy is None:
                assert "beyond_spy" not in p, (focal, p["peer"])
            else:
                assert p["beyond_spy"] == pytest.approx(want_spy, abs=1e-4)
            n_move += bool(want_move)
            n_spy += want_spy is not None
            n_none += not want_move and want_spy is None
    # The fixture really exercises present values and unavailable ones.
    assert n_move >= 4 and n_spy >= 4 and n_none >= 2
    shorty = next(p for p in peers["NVDA"] if p["peer"] == "SHORTY")
    assert "move" not in shorty and "beyond_spy" not in shorty
    none_peer = next(p for p in peers["NVDA"] if p["peer"] is None)
    assert "move" not in none_peer and "beyond_spy" not in none_peer


def test_payload_does_not_ship_all_pair_scores(seeded):
    payload = engine.run(no_fetch=True, tickers=seeded)
    assert "scores" not in payload
    assert payload["n_pairs"] > 0


def test_pairs_stat_reads_n_pairs():
    from RevenueChaser.module_relatedness import report
    assert ">77<" in report._summary_html({"n": 1, "n_pairs": 77})


def test_fetch_refuses_to_overlap(seeded):
    with job_lock.acquire("relatedness", wait=0) as got:
        assert got
        with pytest.raises(engine.AlreadyRunning):
            engine.run(no_fetch=False, tickers=seeded)


def test_refresh_ignores_the_lock(seeded):
    with job_lock.acquire("relatedness", wait=0):
        assert engine.run(no_fetch=True, tickers=seeded)["n"] == len(seeded)


def test_dashboard_runner_reports_a_running_job(seeded):
    from RevenueChaser.app import jobs, runners
    with job_lock.acquire("relatedness", wait=0):
        with pytest.raises(jobs.JobMessage):
            runners.run_relatedness(None, no_fetch=False)
