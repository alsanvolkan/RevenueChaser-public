"""Co-movement editable settings (#40): one resolver, one floor, window as a run
argument, the short-window rules, the short-history split, the recompute job."""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from RevenueChaser.core import board_settings as bs
from RevenueChaser.core import funnel as funnel_mod
from RevenueChaser.core import rawstore
from RevenueChaser.module_comovement.tests import prices_bars
from RevenueChaser.core.tests.panel_client import panel_test_client
from RevenueChaser.core import universe as universe_mod
from RevenueChaser.module_comovement import config, engine, ohlc
from RevenueChaser.module_comovement import settings as cs
from RevenueChaser.module_comovement.tests.test_comovement import (
    _market_two_groups, _seed_ohlc, _seed_universe, _two_group_returns)

B = "module_comovement"
_ROOT = Path(__file__).resolve().parents[2]


# ── the one floor ───────────────────────────────────────────────────────────
def _floor_definitions() -> list[str]:
    """Every non-test assignment of a numeric literal to a ``*MC_MIN*`` name."""
    pat = re.compile(r"^\s*\w*MC_MIN\w*\s*(?::[^=\n]+)?=\s*[\d_.eE+]+\s*(#.*)?$", re.M)
    hits = []
    for path in _ROOT.rglob("*.py"):
        rel = path.relative_to(_ROOT).as_posix()
        if any(p in rel for p in (".venv/", ".cache/", "/tests/", ".hermes/", ".claude/", "node_modules/")):
            continue
        for m in pat.finditer(path.read_text(encoding="utf-8", errors="ignore")):
            hits.append(f"{rel}: {m.group(0).strip()}")
    return hits


def test_exactly_one_numeric_floor_definition():
    hits = _floor_definitions()
    assert len(hits) == 1 and hits[0].startswith("core/universe.py"), hits


def test_board_configs_have_no_floor_of_their_own():
    from RevenueChaser.module_relatedness import config as rel_config
    assert not hasattr(config, "MC_MIN") and not hasattr(rel_config, "MC_MIN")


def test_comovement_universe_is_the_classification_universe_at_the_floor(rawstore_tmp_db):
    caps = {"BIG1": 5e10, "BIG2": 1e9, "EDGE": universe_mod.DEFAULT_MC_MIN,
            "LOW": universe_mod.DEFAULT_MC_MIN - 1, "NOCAP": None}
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "market_cap": c, "sector": "x", "price": 1.0, "name": t,
         "quote": {"symbol": t}} for t, c in caps.items()])
    got = ohlc.universe_tickers()
    assert got == universe_mod.classification_universe(universe_mod.DEFAULT_MC_MIN)
    assert got == ["BIG1", "BIG2", "EDGE"]


# ── effective(): the single resolver ────────────────────────────────────────
def test_default_effective_equals_todays_constants(rawstore_tmp_db):
    e = cs.effective()
    assert (e.window, e.min_obs, e.min_periods) == (252, 126, 60)
    assert (e.oos_holdout, e.oos_min_obs) == (63, 40)
    assert (e.winsor_k, e.bench_stale_sessions) == (5.0, 2)
    assert e.floor == universe_mod.DEFAULT_MC_MIN == 300_000_000.0
    assert not e.short_window and not e.horizon_locked and e.oos_enabled
    assert e.custom is False and e.overridden == ()


def test_oos_min_obs_is_derived_from_the_holdout(rawstore_tmp_db):
    assert cs.oos_min_obs_for(63) == 40 and cs.oos_min_obs_for(42) == 26
    bs.save(B, {"OOS_HOLDOUT": 100})
    e = cs.effective()
    assert e.oos_holdout == 100 and e.oos_min_obs == 63
    assert rawstore.count("board_settings") == 1         # derived, never stored


def test_effective_reads_the_store_and_the_window_argument(rawstore_tmp_db):
    bs.save(B, {"WINDOW": 189, "MIN_OBS": 100, "WINSOR_K": 6.0, "MIN_PERIODS": 30,
                "BENCH_STALE_SESSIONS": 4})
    e = cs.effective()
    assert (e.window, e.default_window, e.min_obs, e.min_periods) == (189, 189, 100, 30)
    assert (e.winsor_k, e.bench_stale_sessions, e.custom) == (6.0, 4, True)
    e2 = cs.effective(252)                                # a run argument wins
    assert e2.window == 252 and e2.default_window == 189 and e2.min_obs == 100


def test_min_obs_never_exceeds_the_run_window(rawstore_tmp_db):
    bs.save(B, {"WINDOW": 378, "MIN_OBS": 300})
    e = cs.effective(126)
    assert e.min_obs == 119 and e.min_periods <= e.min_obs       # 95 % of 126 (Co-movement review S2)


@pytest.mark.parametrize("window,expect", [(21, 15), (42, 21), (63, 31), (125, 62)])
def test_short_windows_scale_min_obs_and_min_periods(rawstore_tmp_db, window, expect):
    bs.save(B, {"MIN_OBS": 200, "MIN_PERIODS": 100})     # stored values are overridden
    e = cs.effective(window)
    assert e.min_obs == e.min_periods == expect
    assert e.short_window and e.horizon_locked and not e.oos_enabled


def test_window_126_is_not_short(rawstore_tmp_db):
    e = cs.effective(126)
    assert not e.short_window and e.min_obs == 119 and e.min_periods == 60


@pytest.mark.parametrize("window,expect", [(126, 119), (189, 126), (252, 126), (378, 126)])
def test_a_window_never_needs_every_session(rawstore_tmp_db, window, expect):
    """Fable review of Co-movement (28 Oct 2026), S2: ``min(MIN_OBS, window)`` asked the 6-month window for 126 of
    126 returns, so one missing daily bar dropped the name. Daily Bars never goes back to a session before a name's
    last stored one (``core.prices._classify``), so that gap is permanent. A window now needs at most 95 % of its
    sessions; the longer windows keep MIN_OBS."""
    assert cs.effective(window).min_obs == expect


def test_one_missing_bar_no_longer_drops_a_name_from_the_6_month_window(rawstore_tmp_db):
    ret = _two_group_returns()
    ret.loc[ret.index[-10], "A1"] = np.nan                     # one session with no stored bar
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_prices(ret)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(2,), persist=False, window=126)
    assert "A1" not in p["dropped"] and p["funnel"]["missing_bars"] == 0
    assert any(m["ticker"] == "A1" for c in p["clusters"]["2"] for m in c["members"])


def test_payload_block_for_the_ui(rawstore_tmp_db):
    p = cs.effective(42).payload()
    assert p["window"] == 42 and p["window_options"] == [21, 42, 126, 189, 252, 378]
    assert p["custom"] is False and p["horizon_locked"] is True
    assert p["min_obs"] == 21 and p["oos_min_obs"] == 40


# ── config_hash ─────────────────────────────────────────────────────────────
_BASE = dict(window=252, linkage="ward", cuts=(20, 80, 250), horizon=1,
             benchmark="spy", min_obs=126, oos_holdout=63, winsor_k=5.0,
             min_periods=60, bench_stale_sessions=2, floor=300_000_000.0)


def _hash(**over) -> str:
    kw = {**_BASE, **over}
    return engine.config_hash(kw.pop("window"), kw.pop("linkage"), kw.pop("cuts"),
                              kw.pop("horizon"), kw.pop("benchmark"), **kw)


@pytest.mark.parametrize("name,value", [
    ("window", 126), ("linkage", "average"), ("cuts", (10, 30, 80)), ("horizon", 5),
    ("benchmark", "qqq"), ("min_obs", 100), ("oos_holdout", 84), ("winsor_k", 6.0),
    ("min_periods", 40), ("bench_stale_sessions", 3), ("floor", 500_000_000.0),
])
def test_config_hash_covers_every_input(rawstore_tmp_db, name, value):
    assert _hash(**{name: value}) != _hash()


def test_config_hash_is_stable_for_equal_inputs(rawstore_tmp_db):
    assert _hash() == _hash()
    assert engine.config_hash(252, "ward", (20, 80, 250)) == _hash()    # resolved defaults


def _market_run(**kw):
    ret = _two_group_returns()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_ohlc(ret)
    return tickers, dict(no_fetch=True, tickers=tickers, cuts=(2,), **kw)


def test_a_saved_setting_changes_the_run_hash(rawstore_tmp_db):
    _t, kw = _market_run(persist=False)
    base = engine.run(**kw)["config_hash"]
    bs.save(B, {"WINSOR_K": 6.0})
    assert engine.run(**kw)["config_hash"] != base


def test_refresh_after_a_settings_change_does_not_reuse_the_old_null(rawstore_tmp_db):
    """Runs persisted under other settings no longer match: significance and
    persistence read 'not evaluated' (None) until the next Fetch."""
    _t, kw = _market_run(persist=True)
    fetched = engine.run(**kw)
    assert any(c["significant"] is not None for v in fetched["clusters"].values() for c in v)
    bs.save(B, {"MIN_OBS": 100})
    kw["persist"] = False
    after = engine.run(**kw)
    cards = [c for v in after["clusters"].values() for c in v]
    assert cards and all(c["significant"] is None and c["persist"] is None for c in cards)
    assert after["oos"] == {}
    bs.reset(B)
    back = engine.run(**kw)
    assert any(c["significant"] is not None for v in back["clusters"].values() for c in v)


# ── the window is a run argument; short windows ────────────────────────────
def test_payload_carries_the_effective_settings(rawstore_tmp_db):
    _t, kw = _market_run(persist=False)
    p = engine.run(**kw)
    assert p["window"] == 252 and p["horizon_locked"] is False
    assert p["short_window_note"] is None
    s = p["settings"]
    assert s["window"] == 252 and s["custom"] is False
    assert s["window_options"] == [21, 42, 126, 189, 252, 378]
    assert p["source"]["window"] == 252


def test_window_argument_changes_the_window_used(rawstore_tmp_db):
    _t, kw = _market_run(persist=False)
    full = engine.run(**kw)
    w126 = engine.run(window=126, **kw)
    assert w126["window"] == 126 and w126["config_hash"] != full["config_hash"]
    assert w126["window_start"] > full["window_start"]


def test_short_window_locks_horizon_skips_oos_and_says_so(rawstore_tmp_db):
    _t, kw = _market_run(persist=True)
    p = engine.run(window=42, horizon=5, **kw)
    assert p["horizon"] == 1 and p["horizon_locked"] is True
    assert p["oos"] is None and "too short" in p["oos_note"]
    assert p["oos_start"] is None
    assert "noisy" in p["short_window_note"].lower()
    assert p["settings"]["short_window"] is True and p["window"] == 42
    assert p["funnel"]["min_obs"] == 21 and p["funnel"]["window"] == 42
    assert p["eligible"] == 6
    assert rawstore.count("comov_lag") == 0
    assert rawstore.query("SELECT window_days w FROM comov_runs")[0]["w"] == 42


def test_short_window_scaling_reaches_the_return_matrix(rawstore_tmp_db, monkeypatch):
    """A name with 30 returns is kept at window 42 (min_obs 21) but dropped at 252."""
    ret = _two_group_returns()
    ret["NEW"] = ret["A1"] + np.random.default_rng(3).normal(0, 0.0005, len(ret))
    ret.iloc[:-31, ret.columns.get_loc("NEW")] = np.nan
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_prices(ret)
    kw = dict(no_fetch=True, tickers=tickers, cuts=(2,), persist=False)
    assert engine.run(**kw)["funnel"]["short_history"] == 1
    short = engine.run(window=42, **kw)
    assert short["funnel"]["short_history"] == 0 and short["eligible"] == 7


# ── the short-history split ─────────────────────────────────────────────────
def _seed_prices(ret: pd.DataFrame):
    """Like ``_seed_ohlc`` but a NaN return means *no stored bar that day*."""
    now = time.time()
    rows = []
    for t in ret.columns:
        price = 100 * np.exp(ret[t].fillna(0.0).cumsum())
        for ts, p in price.items():
            if np.isnan(ret[t].loc[ts]):
                continue
            rows.append({"ticker": t, "ts": ts.isoformat(), "open": p, "high": p,
                         "low": p, "close": float(p), "volume": 1.0, "cached_at": now})
    prices_bars.write_bars(rows)


def _split_fixture():
    ret = _two_group_returns()                      # 300 sessions, 6 names
    n = len(ret)
    rng = np.random.default_rng(9)
    late = pd.Series(rng.normal(0, 0.01, n), index=ret.index)
    late.iloc[: n - 30] = np.nan                    # first bar 30 sessions ago
    gappy = pd.Series(rng.normal(0, 0.01, n), index=ret.index)
    keep = set(rng.choice(np.arange(1, n), size=60, replace=False)) | {0}
    gappy[[i not in keep for i in range(n)]] = np.nan   # old name, sparse bars
    ret["LATE"], ret["GAPPY"] = late, gappy
    return ret


def test_short_history_is_split_into_listed_recently_and_missing_bars(rawstore_tmp_db):
    ret = _split_fixture()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_prices(ret)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(2,), persist=False)
    f = p["funnel"]
    assert f["listed_recently"] == 1 and f["missing_bars"] == 1
    assert f["short_history"] == 2 == f["listed_recently"] + f["missing_bars"]
    assert sorted(p["dropped"]) == ["GAPPY", "LATE"]
    steps = p["funnel_steps"]
    assert funnel_mod.validate(steps) == []
    by = {s["key"]: s for s in steps}
    assert by["listed_recently"]["count"] == 1 and by["missing_bars"]["count"] == 1
    assert by["listed_recently"]["by_design"] is True and by["listed_recently"]["fix"] is None
    assert by["missing_bars"]["by_design"] is False
    assert by["missing_bars"]["fix"] is None              # a Fetch downloads no prices (#379, review RB-8)
    assert "short_history" not in by and by["clustered"]["count"] == 6


def test_split_survives_persist_and_reload(rawstore_tmp_db):
    ret = _split_fixture()
    tickers = list(ret.columns)
    _seed_universe(tickers)
    _seed_prices(ret)
    engine.run(no_fetch=True, tickers=tickers, cuts=(2,), persist=True)
    loaded = engine.load_payload()
    assert funnel_mod.validate(loaded["funnel_steps"]) == []
    by = {s["key"]: s for s in loaded["funnel_steps"]}
    assert by["listed_recently"]["count"] == 1 and by["missing_bars"]["count"] == 1
    assert loaded["window"] == 252 and loaded["settings"]["window"] == 252


def test_a_funnel_persisted_before_the_split_keeps_one_short_history_step():
    f = {"scanned_total": 10, "below_floor": 1, "no_market_cap": 0, "no_prices": 0,
         "short_history": 2, "flat": 0, "clustered": 7, "floor": 3e8}
    steps = engine.funnel_steps(f)
    assert [s["key"] for s in steps] == ["scanned", "below_floor", "no_market_cap",
                                          "no_prices", "short_history", "flat", "clustered"]
    assert funnel_mod.validate(steps) == []


def test_split_short_history_uses_the_window_start():
    idx = pd.date_range("2026-01-01", periods=20)
    prices = pd.DataFrame({"OLD": np.nan, "NEW": np.nan, "ALL": 1.0}, index=idx)
    prices.loc[idx[0]:idx[1], "OLD"] = 1.0          # first bar before the window start
    prices.loc[idx[15]:, "NEW"] = 1.0               # first bar inside the window
    recent, missing = engine.split_short_history(prices, ["OLD", "NEW"], window=10, horizon=1)
    assert recent == ["NEW"] and missing == ["OLD"]
    assert engine.split_short_history(prices, [], 10, 1) == ([], [])


# ── default parity: today's results at default settings ────────────────────
_GOLDEN = Path(__file__).with_name("golden_default_parity.json")


def _norm(p):
    cl = {cut: [{"id": c["id"], "label": c["label"], "n": c["n"],
                 "mean_corr": c["mean_corr"], "baseline_r": c["baseline_r"],
                 "significant": c["significant"], "catchall": c["catchall"],
                 "persist": c["persist"], "members": [m["ticker"] for m in c["members"]]}
                for c in items] for cut, items in p["clusters"].items()}
    return {"clusters": cl, "funnel": p["funnel"], "eligible": p["eligible"],
            "oos": p["oos"], "n_factors": p["n_factors"], "dropped": p["dropped"],
            "horizon": p["horizon"], "benchmark_status": p["benchmark_status"],
            "window_start": p["window_start"], "oos_start": p["oos_start"],
            "flat_prints": p["flat_prints"], "cuts": p["cuts"]}


def _diff(a, b, path="") -> list[str]:
    """Paths where ``a`` and ``b`` differ; floats compare to 1e-9 relative.

    The golden file was captured on Windows; Linux BLAS gives the same clusters,
    labels and counts but can differ in the last bits of a correlation."""
    if isinstance(a, float) and isinstance(b, float):
        return [] if math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12) else [f"{path}: {a!r} != {b!r}"]
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return [f"{path}: keys {sorted(a.keys() ^ b.keys())}"]
        return [d for k in a for d in _diff(a[k], b[k], f"{path}/{k}")]
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} != {len(b)}"]
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in _diff(x, y, f"{path}[{i}]")]
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


def test_default_settings_reproduce_the_pre_change_payload(rawstore_tmp_db):
    """``golden_default_parity.json`` was captured from the code before #40.

    Only the hash string and the new keys (funnel split, window, settings) may
    differ: clusters, labels, significance, persistence, funnel counts, window
    dates and the lagged-test summary are identical.

    One case was rewritten for the Fable review of 28 Oct 2026, N2: ``mkt_nop`` (a store-only run with SPY applied)
    used to reuse the null and lagged test that ``two_p`` (a Fetch on other data with no SPY, so raw returns) had
    stored under the same hash. It now has its own hash and nothing stored to reuse: ``baseline_r``,
    ``significant`` and ``persist`` are None and ``oos`` is empty. Nothing else in the file changed."""
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    got = {}
    for name, ret in (("two", _two_group_returns(n=700)), ("mkt", _market_two_groups(n=700))):
        tickers = [t for t in ret.columns if t != "SPY"]
        _seed_universe(tickers)
        _seed_ohlc(ret)
        got[name + "_nop"] = _norm(engine.run(no_fetch=True, tickers=tickers, cuts=(2,),
                                              persist=False))
        got[name + "_p"] = _norm(engine.run(no_fetch=True, tickers=tickers, cuts=(2,),
                                            persist=True))
    # R4 (#87) adds the security-type counts to the funnel dict. Nothing is typed
    # as a fund here, so ``classification_total`` and every cluster stay identical.
    new_funnel_keys = {"listed_recently", "missing_bars", "window", "min_obs",
                       "floor_total", "funds_etfs", "spacs", "other_securities",
                       "unknown_type"}
    for k, v in got.items():
        v["funnel"] = {a: b for a, b in v["funnel"].items() if a not in new_funnel_keys}
    diffs = _diff(json.loads(json.dumps(got, default=str)), golden)
    assert not diffs, diffs[:20]


# ── the recompute job + the settings route ─────────────────────────────────
def test_the_recompute_job_is_registered_short_lane_no_uw():
    """Read the registration itself: app tests clear the ``jobs`` registries."""
    from RevenueChaser.app import dashboard
    src = Path(dashboard.__file__).read_text(encoding="utf-8")
    m = re.search(r'jobs\.register\("comovement_recompute",(.*?)\)\s*$', src, re.S | re.M)
    assert m, "comovement_recompute is not registered"
    call = m.group(1)
    assert "runners.run_comovement_recompute" in call
    assert re.findall(r'"(\w+)"', re.search(r"\[(.*?)\]", call, re.S).group(1)) == [
        "module_comovement", "module_settings"]
    assert "requires_uw=False" in call and "lane=" not in call      # short lane (default)


def test_recompute_runner_rerenders_both_boards_and_reports(rawstore_tmp_db):
    from RevenueChaser.app import runners
    from RevenueChaser.app.jobs import JobMessage

    _market_run(persist=True)
    bs.save(B, {"WINSOR_K": 6.0})
    with pytest.raises(JobMessage) as exc:
        runners.run_comovement_recompute(None)
    msg = exc.value.message
    assert "6 clustered" in msg and "252-session" in msg and "custom settings" in msg
    assert runners.COMOVEMENT_LATEST.exists() and runners.SETTINGS_LATEST.exists()
    payload = json.loads(runners.COMOVEMENT_JSON.read_text(encoding="utf-8"))
    assert payload["settings"]["winsor_k"] == 6.0 and payload["settings"]["custom"] is True


@pytest.fixture
def client(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.app import dashboard
    submitted = []
    monkeypatch.setattr(dashboard.jobs, "submit",
                        lambda module, params=None: submitted.append((module, params)) or "job123")
    dashboard.app.config["TESTING"] = True
    c = panel_test_client(dashboard.app)
    c.submitted = submitted
    return c


def test_route_unknown_board_is_404(client):
    r = client.post("/api/settings/board/module_nope", json={"changes": {"X": 1}})
    assert r.status_code == 404 and r.get_json()["ok"] is False
    assert client.submitted == []


def test_route_invalid_change_is_400_with_messages_and_no_job(client):
    r = client.post("/api/settings/board/module_comovement",
                    json={"changes": {"WINSOR_K": 9, "MIN_OBS": 10}})
    assert r.status_code == 400
    body = r.get_json()
    assert body["ok"] is False
    assert any("WINSOR_K must be between 4 and 8" in m for m in body["messages"])
    assert any("MIN_OBS" in m for m in body["messages"])
    assert client.submitted == [] and rawstore.count("board_settings") == 0


@pytest.mark.parametrize("body", [None, {}, {"changes": "x"}, {"changes": {}},
                                  {"reset": []}, {"reset": ["NOPE"]}, {"reset": "all"}])
def test_route_malformed_bodies_are_400(client, body):
    r = client.post("/api/settings/board/module_comovement", json=body)
    assert r.status_code == 400 and client.submitted == []


def test_route_save_stores_and_queues_the_recompute(client):
    r = client.post("/api/settings/board/module_comovement",
                    json={"changes": {"WINSOR_K": 6.5, "MIN_OBS": 100}})
    assert r.status_code == 202
    body = r.get_json()
    assert body["ok"] is True and body["job_id"] == "job123"
    assert body["values"]["values"]["WINSOR_K"] == 6.5
    assert body["values"]["overridden"] == ["MIN_OBS", "WINSOR_K"]
    assert client.submitted == [("comovement_recompute", {})]
    assert bs.get(B, "MIN_OBS") == 100


def test_route_reset_deletes_rows_and_queues_the_recompute(client):
    bs.save(B, {"WINSOR_K": 6.5, "MIN_OBS": 100})
    r = client.post("/api/settings/board/module_comovement", json={"reset": True})
    assert r.status_code == 202 and r.get_json()["values"]["overridden"] == []
    assert rawstore.count("board_settings") == 0
    assert client.submitted == [("comovement_recompute", {})]


def test_route_reset_some_keys(client):
    bs.save(B, {"WINSOR_K": 6.5, "MIN_OBS": 100})
    r = client.post("/api/settings/board/module_comovement", json={"reset": ["WINSOR_K"]})
    assert r.status_code == 202 and r.get_json()["values"]["overridden"] == ["MIN_OBS"]


def test_config_hash_carries_the_engine_method(rawstore_tmp_db, monkeypatch):
    """A stored run, null or lagged test of an older method never matches (Co-movement review, 28 Oct 2026)."""
    before = _hash()
    monkeypatch.setattr(engine.config, "METHOD", engine.config.METHOD + 1)
    assert _hash() != before


def test_a_raw_return_fallback_has_its_own_hash(rawstore_tmp_db):
    """Fable review of Co-movement (28 Oct 2026), N2: with SPY missing, "remove market = SPY" falls back to raw
    returns, but the run hashed as a residual one. A Refresh with SPY back then reused that Fetch's raw-return null
    and lagged test, and the other way round. The hash now says when the benchmark was asked for and not applied."""
    _t, kw = _market_run(persist=False)                  # no SPY bars: the board falls back to raw returns
    p = engine.run(**kw)
    assert p["benchmark"] == "spy" and p["benchmark_applied"] is False
    assert p["config_hash"] != engine.config_hash(252, config.LINKAGE, (2,), 1, "spy")
    assert p["config_hash"] == engine.config_hash(252, config.LINKAGE, (2,), 1, "spy", benchmark_applied=False)
    assert engine.config_hash(252, config.LINKAGE, (2,), 1, "spy", benchmark_applied=True) \
        == engine.config_hash(252, config.LINKAGE, (2,), 1, "spy")
    assert engine.config_hash(252, config.LINKAGE, (2,), 1, "off", benchmark_applied=False) \
        == engine.config_hash(252, config.LINKAGE, (2,), 1, "off")   # off is raw by choice, not a fallback


@pytest.mark.parametrize("persist", [False, True])
def test_a_name_listed_after_a_stale_benchmarks_last_bar_is_listed_recently(rawstore_tmp_db, persist):
    """Fable review of Co-movement (28 Oct 2026), N3: when SPY is stale the window is cut at its last bar. A name
    whose first bar is after that cut has no price inside the cut window; both the store-only and the full read
    then filed it as "missing bars" (a gap), though it simply listed after the window started."""
    ret = _market_two_groups(n=300)
    ret.loc[ret.index[-6:], "SPY"] = np.nan                     # SPY 6 sessions behind: stale, the window is cut
    new = pd.Series(np.nan, index=ret.index)
    new.iloc[-3:] = 0.01                                        # first bar 3 sessions ago, after SPY's last
    ret["NEW"] = new
    tickers = [t for t in ret.columns if t != "SPY"]
    _seed_universe(tickers)
    _seed_prices(ret)
    p = engine.run(no_fetch=True, tickers=tickers, cuts=(2,), persist=persist)
    assert p["benchmark_status"] == "stale" and "NEW" in p["dropped"]
    assert p["funnel"]["listed_recently"] == 1 and p["funnel"]["missing_bars"] == 0
