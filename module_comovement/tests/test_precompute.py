"""#95: a window switch serves a stored run; Fetch precomputes the popular windows.

A store-only run (or a precompute) serves the latest persisted run with the
same ``config_hash``, the same ``prices_through`` and the same clustered names
instead of re-clustering. Anything else — a settings change, newer prices,
another ticker set — recomputes as before.
"""
from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd
import pytest

from RevenueChaser.core import board_settings as bs
from RevenueChaser.core import rawstore
from RevenueChaser.module_comovement import cluster as cluster_mod
from RevenueChaser.module_comovement import config, engine
from RevenueChaser.module_comovement.tests.test_comovement import (
    _market_two_groups, _seed_ohlc, _seed_universe)
from RevenueChaser.module_comovement.tests.test_settings import _diff, _norm

B = "module_comovement"


@pytest.fixture
def market(rawstore_tmp_db):
    """Two residual groups on a SPY factor, 400 sessions; tickers exclude SPY."""
    ret = _market_two_groups(n=400)
    tickers = [t for t in ret.columns if t != "SPY"]
    _seed_universe(tickers)
    _seed_ohlc(ret)
    return ret, tickers


@pytest.fixture
def clusterings(monkeypatch):
    """Counts every clustering the engine runs."""
    calls = []
    real = cluster_mod.clusters

    def spy(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(cluster_mod, "clusters", spy)
    return calls


def _add_session(ret: pd.DataFrame) -> None:
    """One more stored session for every name (the D01 seed replaces per ticker,
    so the whole history is written again, stamped now)."""
    nxt = ret.index[-1] + pd.Timedelta(days=1)
    rng = np.random.default_rng(5)
    more = pd.DataFrame(rng.normal(0, 0.01, (1, ret.shape[1])), index=[nxt],
                        columns=ret.columns)
    _seed_ohlc(pd.concat([ret, more]))


# ── serving a stored run ────────────────────────────────────────────────────
def test_switch_to_a_precomputed_window_does_no_clustering(market, clusterings):
    stored = engine.run(no_fetch=True, persist=True, window=126)
    clusterings.clear()
    served = engine.run(no_fetch=True, window=126)
    assert clusterings == []
    assert served["as_of"] == stored["as_of"]
    assert served["config_hash"] == stored["config_hash"]


def test_a_served_run_matches_the_computed_one_key_for_key(market):
    stored = engine.run(no_fetch=True, persist=True, window=126)
    served = engine.run(no_fetch=True, window=126)
    assert set(served) == set(stored)                      # no new payload key
    a, b = (json.loads(json.dumps(_norm(p), default=str)) for p in (served, stored))
    assert not _diff(a, b), _diff(a, b)[:10]
    for k in ("fetch", "dropped", "no_prices", "flat_prints", "universe_total",
              "window_end", "funnel", "funnel_steps", "oos_note", "window", "settings",
              "horizon", "benchmark", "benchmark_status", "benchmark_applied",
              "prices_through", "n_factors", "window_start", "oos_start", "oos", "cuts"):
        assert served[k] == stored[k], k
    assert served["verdicts_from"] is None


def test_a_fetch_run_is_never_served(market, clusterings):
    engine.run(no_fetch=True, persist=True, window=126)
    clusterings.clear()
    engine.run(no_fetch=True, persist=True, window=126)    # persisting, no reuse
    assert clusterings != []
    assert rawstore.count("comov_runs") == 2


def test_a_settings_change_invalidates(market, clusterings):
    engine.run(no_fetch=True, persist=True, window=126)
    bs.save(B, {"WINSOR_K": 6.0})
    clusterings.clear()
    engine.run(no_fetch=True, window=126)
    assert clusterings != []
    bs.reset(B)
    clusterings.clear()
    engine.run(no_fetch=True, window=126)
    assert clusterings == []


def test_newer_prices_recompute(market, clusterings):
    ret, _t = market
    stored = engine.run(no_fetch=True, persist=True, window=126)
    _add_session(ret)
    clusterings.clear()
    p = engine.run(no_fetch=True, window=126)
    assert clusterings != [] and p["prices_through"] > stored["prices_through"]


def test_revised_prices_on_the_same_dates_recompute(market, clusterings):
    """A re-fetch can revise adjusted history without moving the last date."""
    ret, _t = market
    stored = engine.run(no_fetch=True, persist=True, window=126)
    _seed_ohlc(ret * 1.5)
    clusterings.clear()
    p = engine.run(no_fetch=True, window=126)
    assert clusterings != [] and p["prices_through"] == stored["prices_through"]


def test_another_ticker_set_recomputes(market, clusterings):
    _ret, tickers = market
    engine.run(no_fetch=True, persist=True, window=126)
    clusterings.clear()
    p = engine.run(no_fetch=True, window=126, tickers=tickers[:-1])
    assert clusterings != [] and p["eligible"] == len(tickers) - 1


def test_load_payload_sorts_cards_by_size_then_label(market):
    engine.run(no_fetch=True, persist=True)
    with rawstore.connect() as conn:                      # a tie, labelled out of order
        conn.execute("UPDATE comov_nodes SET n = 3, label = CASE cluster_id "
                     "WHEN '1' THEN 'Zeta' ELSE 'Alpha' END")
    for items in engine.load_payload()["clusters"].values():
        assert [(c["n"], c["label"]) for c in items] == sorted(
            [(c["n"], c["label"]) for c in items], key=lambda x: (-x[0], x[1]))


def test_a_run_older_than_the_engine_epoch_is_not_served(market, clusterings, monkeypatch):
    """A stored run older than ``ENGINE_EPOCH`` is never served (#95 follow-up):
    a store-only run re-clusters instead, exactly like a settings change."""
    engine.run(no_fetch=True, persist=True, window=126)
    monkeypatch.setattr(config, "ENGINE_EPOCH", "2099-01-01T00:00:00+00:00")
    clusterings.clear()
    engine.run(no_fetch=True, window=126)
    assert clusterings != []


def _tight_returns(groups, n=300, seed=17):
    rng = np.random.default_rng(seed)
    data = {}
    for group in groups:
        base = rng.normal(0, 0.01, n)
        for t in group:
            data[t] = base + rng.normal(0, 0.0005, n)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(data, index=idx)


def _four_group_returns(n=300, seed=17):
    """Two tight groups of four names each (big enough for ``LABEL_MIN_N``)."""
    return _tight_returns((("A1", "A2", "A3", "A4"), ("B1", "B2", "B3", "B4")), n, seed)


def _seed_universe_industry(industry_by_ticker: dict):
    """Like ``test_comovement._seed_universe`` but each ticker keeps its own
    industry (UW's text, #266), so a cluster's label depends on which group it lands in."""
    rawstore.replace_all("equity_universe", [{
        "ticker": t, "market_cap": 1.0e10, "sector": "Technology",
        "price": 10.0, "name": t, "industry": industry, "quote": {"symbol": t},
    } for t, industry in industry_by_ticker.items()])


def test_served_run_relabels_from_the_current_spine(rawstore_tmp_db):
    """A served run's labels come from the *live* spine, not storage time
    (#95 follow-up): an industry reclassification after the run was stored
    must show up on it right away, with cards re-sorted on the new labels.

    #97 names a group only when its industry is enriched versus the cut
    (lift >= 3). Two equal industries in a background of eight have lift 2, so
    this fixture is four pure groups of four: each is 100% of a name that is
    4/16 of the background (lift 4), which the enrichment rule does label.
    """
    groups = (("A1", "A2", "A3", "A4"), ("B1", "B2", "B3", "B4"),
              ("C1", "C2", "C3", "C4"), ("D1", "D2", "D3", "D4"))
    industries = {"A": "Zeta Industries", "B": "Alpha Systems",
                  "C": "Middle Widgets", "D": "Quartz Mining"}
    ret = _tight_returns(groups)
    _seed_universe_industry({t: industries[t[0]] for t in ret.columns})
    _seed_ohlc(ret)

    stored = engine.run(no_fetch=True, persist=True, cuts=(4,), window=126)
    stored_labels = [c["label"] for c in stored["clusters"]["4"]]
    assert stored_labels == [
        "Alpha Systems 100%, ×4.0", "Middle Widgets 100%, ×4.0",
        "Quartz Mining 100%, ×4.0", "Zeta Industries 100%, ×4.0"]

    # The spine reclassifies the Zeta group after the run was stored.
    industries["A"] = "Aardvark Corp"
    _seed_universe_industry({t: industries[t[0]] for t in ret.columns})

    served = engine.run(no_fetch=True, cuts=(4,), window=126)
    assert served["as_of"] == stored["as_of"]                      # same run served
    served_labels = [c["label"] for c in served["clusters"]["4"]]
    assert served_labels == [
        "Aardvark Corp 100%, ×4.0", "Alpha Systems 100%, ×4.0",
        "Middle Widgets 100%, ×4.0", "Quartz Mining 100%, ×4.0"]


def test_a_cluster_with_no_shared_industry_stays_neutral_when_served(rawstore_tmp_db):
    """A group that shares nothing (or only ``Unclassified``, or a 2/2 industry
    split that fails the enrichment hits gate) keeps its neutral number, on the
    run and on the served run alike."""
    ret = _four_group_returns()
    tickers = list(ret.columns)
    _seed_universe_industry({**{t: "Unclassified" for t in tickers[:4]},
                             **{t: ("Banks - Regional" if i < 2 else "Banks - Diversified")
                                for i, t in enumerate(tickers[4:])}})
    _seed_ohlc(ret)

    stored = engine.run(no_fetch=True, persist=True, cuts=(2,), window=126)
    served = engine.run(no_fetch=True, cuts=(2,), window=126)
    assert served["as_of"] == stored["as_of"]
    for p in (stored, served):
        labels = [c["label"] for c in p["clusters"]["2"]]
        assert len(labels) == 2 and all(label.startswith("Cluster ") for label in labels), labels


# ── precompute ──────────────────────────────────────────────────────────────
def test_precompute_windows_default():
    assert config.PRECOMPUTE_WINDOWS == (126, 252)


def test_precompute_computes_then_skips(market, clusterings):
    assert engine.precompute() == {126: "computed", 252: "computed"}
    assert rawstore.count("comov_runs") == 2
    clusterings.clear()
    assert engine.precompute() == {126: "reused", 252: "reused"}
    assert clusterings == [] and rawstore.count("comov_runs") == 2
    w = sorted(r["w"] for r in rawstore.query("SELECT window_days w FROM comov_runs"))
    assert w == [126, 252]


def test_precompute_after_a_fetch_reuses_the_fetch_window(market):
    engine.run(no_fetch=True, persist=True)                # the Fetch's own run (252)
    assert engine.precompute() == {126: "computed", 252: "reused"}


def test_precompute_records_a_full_run(market):
    engine.precompute(windows=(126,))
    row = rawstore.query("SELECT * FROM comov_runs")[0]
    assert row["window_days"] == 126 and row["null_json"] and row["funnel_json"]


# ── the Fetch runner ────────────────────────────────────────────────────────
@pytest.fixture
def runner(market, monkeypatch):
    from RevenueChaser.app import runners
    from RevenueChaser.module_comovement import ohlc

    monkeypatch.setattr(ohlc, "fetch_bulk", lambda tickers=None, no_fetch=False, **kw:
                        {"requested": 0, "fetched": 0, "no_fetch": no_fetch})
    return runners


def test_fetch_precomputes_the_popular_windows(runner, monkeypatch):
    seen = []
    real = engine.precompute
    monkeypatch.setattr(engine, "precompute", lambda **kw: seen.append(kw) or real(**kw))
    runner.run_comovement(None)
    assert len(seen) == 1
    assert sorted(r["w"] for r in rawstore.query("SELECT window_days w FROM comov_runs")) \
        == [126, 252]


def test_a_store_only_run_does_not_precompute(runner, monkeypatch):
    seen = []
    monkeypatch.setattr(engine, "precompute", lambda **kw: seen.append(kw))
    runner.run_comovement(None, no_fetch=True)
    runner.run_comovement(None, no_fetch=True, window=126)
    assert seen == []


def test_a_precompute_failure_does_not_fail_the_fetch(runner, monkeypatch, caplog):
    def boom(**kw):
        raise RuntimeError("precompute exploded")

    monkeypatch.setattr(engine, "precompute", boom)
    with caplog.at_level(logging.WARNING):
        path = runner.run_comovement(None)
    assert path == runner.COMOVEMENT_LATEST and path.exists()
    board = json.loads(runner.COMOVEMENT_JSON.read_text(encoding="utf-8"))
    assert board["config_hash"] and board["window"] == 252
    assert rawstore.count("comov_runs") == 1
    assert "precompute exploded" in caplog.text


# ── Relatedness keeps reading the Fetch run ─────────────────────────────────
def _relatedness_run(monkeypatch) -> str | None:
    """The ``as_of`` Relatedness's cluster chips read."""
    from RevenueChaser.module_relatedness import engine as rel_engine

    seen = []
    real = rawstore.query

    def q(sql, params=None, **kw):
        out = real(sql, params, **kw)
        if "FROM comov_runs" in sql:
            seen.append(out[0]["as_of"] if out else None)
        return out

    monkeypatch.setattr(rel_engine.rawstore, "query", q)
    assert rel_engine._cluster_levels()
    return seen[0]


def test_relatedness_reads_the_default_window_run_not_a_newer_precompute(
        market, monkeypatch):
    fetch = engine.run(no_fetch=True, persist=True)                       # 252
    engine.precompute(windows=(126,))                                     # newer as_of
    assert _relatedness_run(monkeypatch) == fetch["as_of"]


def test_relatedness_prefers_newer_prices_over_the_default_window(market, monkeypatch):
    ret, _t = market
    engine.run(no_fetch=True, persist=True)                               # 252, old prices
    _add_session(ret)
    newer = engine.run(no_fetch=True, persist=True, window=126)           # newer prices
    assert _relatedness_run(monkeypatch) == newer["as_of"]
