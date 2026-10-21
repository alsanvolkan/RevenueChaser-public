"""#42 cut-tiers research script: synthetic-data tests, read-only and offline guarantees.

Synthetic data only (built here); nothing touches a real store or the network. The
file name starts with a digit, so the module is loaded by path (importlib).
"""
from __future__ import annotations

import hashlib
import importlib.util
import socket
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "research" / "42_cut_tiers.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("cut_tiers_42", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── the response: planted groups beat a single blob ─────────────────────────
def _planted_prices(seed=7, groups=3, per_group=10, noise=10, sessions=400):
    rng = np.random.default_rng(seed)
    factor = rng.normal(0.0, 0.02, (sessions, groups))
    members = factor[:, np.repeat(np.arange(groups), per_group)]
    ret = members + rng.normal(0.0, 0.01, members.shape)
    ret = np.hstack([ret, rng.normal(0.0, 0.02, (sessions, noise))])
    names = ([f"G{g}_{i}" for g in range(groups) for i in range(per_group)]
             + [f"N{i}" for i in range(noise)])
    idx = pd.bdate_range(end="2026-09-30", periods=sessions)
    return pd.DataFrame(100.0 * np.exp(np.cumsum(ret, axis=0)), index=idx, columns=names)


def _recovered(planted: dict, groups: dict, threshold: float = 0.7) -> bool:
    clusters = [set(v) for v in groups.values()]
    for members in planted.values():
        ms = set(members)
        best = max((len(ms & c) / len(ms) for c in clusters), default=0.0)
        if best < threshold:
            return False
    return True


def test_oos_r2_recovers_planted_groups(mod):
    prices = _planted_prices()
    planted = {f"G{g}": [f"G{g}_{i}" for i in range(10)] for g in range(3)}

    form = mod.form_matrix(prices, 0, mod.WINDOW)
    test = mod.test_matrix(prices, mod.WINDOW, mod.WINDOW + mod.HOLDOUT, mod.OOS_MIN_OBS)
    g1 = mod.cluster_groups(form, 1)
    g3 = mod.cluster_groups(form, 3)

    r1 = mod.oos_r2(g1, test)
    r3 = mod.oos_r2(g3, test)
    assert r3 is not None and r1 is not None
    assert r3 > r1 + 0.2, f"k=3 R2 {r3:.3f} not clearly above k=1 R2 {r1:.3f}"
    assert _recovered(planted, g3), f"planted groups not recovered: {g3}"


def test_oos_r2_is_one_when_a_group_is_a_perfect_twin(mod):
    idx = pd.bdate_range("2026-01-01", periods=40)
    a = np.tile(np.linspace(-1.0, 1.0, 40), (4, 1)).T
    test = pd.DataFrame(a, index=idx, columns=list("WXYZ"))
    groups = {0: ["W", "X", "Y", "Z"]}
    assert mod.oos_r2(groups, test) == pytest.approx(1.0, abs=1e-9)


def test_oos_r2_pure_noise_with_one_pair_is_not_positive(mod):
    """F42-1: a name with no peer must be scored against 0, not skipped.

    Before the fix the pair's two names (2 of 100) were the only cells in
    ``SS_res`` while ``SS_tot`` summed all 100, so pure noise scored ~ +0.97.
    """
    rng = np.random.default_rng(3)
    names = [f"n{i}" for i in range(100)]
    test = pd.DataFrame(rng.normal(0.0, 0.01, (80, 100)), columns=names)
    r2 = mod.oos_r2({0: ["n0", "n1"]}, test, names)
    assert r2 is not None and r2 <= 0.0, f"pure noise R2 {r2}"


def test_oos_r2_planted_groups_stay_clearly_positive(mod):
    """F42-1: scoring the unpredicted names against 0 must not kill a real signal."""
    prices = _planted_prices()
    form = mod.form_matrix(prices, 0, mod.WINDOW)
    test = mod.test_matrix(prices, mod.WINDOW, mod.WINDOW + mod.HOLDOUT, mod.OOS_MIN_OBS)
    names = [t for t in form.columns if t in set(test.columns)]
    r3 = mod.oos_r2(mod.cluster_groups(form, 3), test, names)
    assert r3 is not None and r3 > 0.3, f"planted R2 {r3}"


def test_oos_r2_baseline_and_k_share_the_same_ss_tot(mod):
    """F42-1: a k grouping and a baseline on the same names share SS_tot."""
    prices = _planted_prices()
    form = mod.form_matrix(prices, 0, mod.WINDOW)
    test = mod.test_matrix(prices, mod.WINDOW, mod.WINDOW + mod.HOLDOUT, mod.OOS_MIN_OBS)
    names = [t for t in form.columns if t in set(test.columns)]
    baseline = {f"b{g}": [f"G{g}_{i}" for i in range(10)] for g in range(3)}
    _, ss_tot_k, _ = mod.oos_r2_parts(mod.cluster_groups(form, 3), test, names)
    _, ss_tot_b, _ = mod.oos_r2_parts(baseline, test, names)
    assert ss_tot_k == pytest.approx(ss_tot_b)
    assert ss_tot_k == pytest.approx(mod.ss_total(test, names))
    # the shared denominator ignores test columns outside ``names``
    with_extra = test.copy()
    with_extra["out_of_scope"] = 5.0
    assert mod.ss_total(with_extra, names) == pytest.approx(ss_tot_k)


# ── the pre-registered rule, one test per branch ────────────────────────────
def _m(mean, rng=0.01, hold=0.9, small=0.0):
    return {"mean_oos_r2": mean, "range": rng, "hold_share": hold, "small_share": small}


def test_rule_fine_picks_the_largest_namable_holding_k(mod):
    per_k = {20: _m(0.20), 80: _m(0.30), 100: _m(0.40, hold=0.85, small=0.02),
             150: _m(0.42, hold=0.50, small=0.02), 250: _m(0.30, rng=0.05, hold=0.50)}
    proposal = mod.propose_levels(per_k, sector_r2=0.10)
    assert proposal["fine"]["candidate"] == 100
    assert proposal["fine"]["proposed"] == 100
    assert proposal["fine"]["verdict"] == "replace"


def test_rule_medium_picks_the_first_plateau(mod):
    means = {10: 0.30, 15: 0.32, 20: 0.335, 30: 0.340, 40: 0.350, 60: 0.355,
             80: 0.356, 100: 0.356, 125: 0.356, 150: 0.356, 200: 0.356,
             250: 0.356, 300: 0.356}
    per_k = {k: _m(means[k]) for k in mod.K_GRID}
    proposal = mod.propose_levels(per_k, sector_r2=0.10)
    assert proposal["medium"]["candidate"] == 40


def test_rule_medium_keeps_when_every_doubling_still_gains(mod):
    """F42-2: with no k whose double is inside the grid, Medium must keep.

    Before the fix the end of the grid was read as a plateau: k = 250 was
    compared with 300 (1.2x, gain 0.008 < 0.01), so the candidate became 250.
    """
    means = {k: 0.03 * float(np.log2(k)) for k in mod.K_GRID}
    per_k = {k: _m(means[k]) for k in mod.K_GRID}
    proposal = mod.propose_levels(per_k, sector_r2=0.0)
    assert proposal["medium"]["verdict"] == "keep"
    assert proposal["medium"]["candidate"] == proposal["medium"]["current"]


def test_rule_coarse_keeps_20_above_the_sector_baseline(mod):
    per_k = {20: _m(0.25), 40: _m(0.30), 80: _m(0.30), 250: _m(0.30)}
    proposal = mod.propose_levels(per_k, sector_r2=0.20)
    assert proposal["coarse"]["proposed"] == 20
    assert proposal["coarse"]["verdict"] == "keep"


def test_rule_coarse_proposes_above_the_baseline_when_below_it(mod):
    per_k = {20: _m(0.25, rng=0.05), 40: _m(0.35), 80: _m(0.36), 250: _m(0.36)}
    proposal = mod.propose_levels(per_k, sector_r2=0.30)
    assert proposal["coarse"]["candidate"] == 40
    assert proposal["coarse"]["proposed"] == 40
    assert proposal["coarse"]["verdict"] == "replace"


def test_rule_spread_guard_keeps_when_the_gain_is_within_the_spread(mod):
    per_k = {20: _m(0.20), 80: _m(0.30), 100: _m(0.40, hold=0.85, small=0.02),
             250: _m(0.30, rng=0.20, hold=0.50)}
    proposal = mod.propose_levels(per_k, sector_r2=0.10)
    # gain 0.10 over k=250 is not larger than the 0.20 spread across origins
    assert proposal["fine"]["candidate"] == 100
    assert proposal["fine"]["proposed"] == 250
    assert proposal["fine"]["verdict"] == "keep"


# ── the store files are opened read-only ────────────────────────────────────
def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_raw(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE equity_universe (ticker TEXT, market_cap DOUBLE, "
                "sector TEXT, industry TEXT)")
    con.execute("CREATE TABLE security_master (ticker TEXT, security_type TEXT)")
    con.execute("CREATE TABLE swing_ohlc (ticker TEXT, ts TEXT, close DOUBLE)")
    con.execute("INSERT INTO equity_universe VALUES ('AAA', 1e9, 'Technology', 'Software'), "
                "('BBB', 2e9, 'Energy', 'Oil & Gas E&P')")
    con.execute("INSERT INTO security_master VALUES ('AAA', 'common'), ('BBB', 'common')")
    for ticker in ("AAA", "BBB", "SPY"):
        for day in pd.bdate_range("2026-01-01", periods=10):
            con.execute("INSERT INTO swing_ohlc VALUES (?, ?, ?)",
                        [ticker, day.strftime("%Y-%m-%d"), 100.0])
    con.close()


def _make_prices(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE price_bars_daily (ticker TEXT, session_date DATE, close DOUBLE)")
    con.execute("INSERT INTO price_bars_daily VALUES ('AAA', DATE '2026-01-02', 100.0)")
    con.close()


def _make_raw_without_swing(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE equity_universe (ticker TEXT, market_cap DOUBLE, "
                "sector TEXT, industry TEXT)")
    con.execute("CREATE TABLE security_master (ticker TEXT, security_type TEXT)")
    con.execute("INSERT INTO equity_universe VALUES ('AAA', 1e9, 'Technology', 'Software')")
    con.execute("INSERT INTO security_master VALUES ('AAA', 'common')")
    con.close()


def test_load_inputs_is_read_only(mod, tmp_path):
    raw = tmp_path / "raw.duckdb"
    prices = tmp_path / "prices.duckdb"
    _make_raw(raw)
    _make_prices(prices)
    before = (_sha(raw), _sha(prices))

    tickers, frame, classification = mod.load_inputs(raw, prices)

    assert (_sha(raw), _sha(prices)) == before
    assert {"AAA", "BBB"} <= set(tickers)
    assert "SPY" in frame.columns
    assert classification["AAA"]["sector"]
    assert classification["AAA"]["industry"] == "Software"


def test_load_inputs_opens_both_files_read_only(mod, tmp_path, monkeypatch):
    """Byte equality alone misses a writable open that happens not to write: check the mode itself."""
    raw = tmp_path / "raw.duckdb"
    prices = tmp_path / "prices.duckdb"
    _make_raw(raw)
    _make_prices(prices)
    modes: list[bool] = []
    real = duckdb.connect

    def spy(path, *a, **k):
        modes.append(bool(k.get("read_only")))
        return real(path, *a, **k)

    monkeypatch.setattr(mod.duckdb, "connect", spy)
    mod.load_inputs(raw, prices)
    assert modes == [True, True]


def test_load_inputs_returns_empty_frame_without_swing_ohlc(mod, tmp_path):
    """F42-4: no ``price_bars_daily`` fallback; the board reads swing_ohlc only."""
    raw = tmp_path / "raw.duckdb"
    prices = tmp_path / "prices.duckdb"
    _make_raw_without_swing(raw)
    _make_prices(prices)
    tickers, frame, _classification = mod.load_inputs(raw, prices)
    assert "AAA" in tickers
    assert frame.empty


# ── flat printers are dropped before returns, as the board does ─────────────
def test_drop_flat_printers_excludes_a_zero_return_name(mod):
    idx = pd.bdate_range("2026-01-01", periods=30)
    rng = np.random.default_rng(0)
    prices = pd.DataFrame(
        {"AAA": 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, 30))),
         "FLAT": np.full(30, 50.0),
         "BBB": 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, 30)))},
        index=idx)
    kept, names = mod.drop_flat_printers(prices, ["AAA", "FLAT", "BBB"])
    assert "FLAT" not in names
    assert "FLAT" not in kept.columns
    assert "AAA" in names and "BBB" in names


def test_run_drops_flat_printers_before_returns(mod, monkeypatch, tmp_path):
    """F42-3 wiring: run() must call the drop (the unit test alone misses removal)."""
    raw = tmp_path / "raw.duckdb"
    prices = tmp_path / "prices.duckdb"
    _make_raw(raw)      # every swing_ohlc close is a constant 100.0
    _make_prices(prices)
    real = mod.drop_flat_printers
    kept: dict[str, list[str]] = {}

    def spy(frame, names):
        _frame, names = real(frame, names)
        kept["names"] = names
        raise RuntimeError("stop-after-flat-printers")

    monkeypatch.setattr(mod, "drop_flat_printers", spy)
    with pytest.raises(RuntimeError, match="stop-after-flat-printers"):
        mod.main(["--raw", str(raw), "--prices", str(prices),
                  "--out", str(tmp_path / "out")])
    assert kept["names"] == []


# ── the network is blocked inside main() ────────────────────────────────────
def test_network_block_is_active_inside_main(mod, monkeypatch):
    """F42-5: assert the script's own block message, without ever opening a socket.

    The socket is faked, so a removed block fails on the message (not on an
    offline machine's unrelated OS error) and never dials 8.8.8.8.
    """
    block_message = "network access is blocked in the #42 research script"

    class FakeSocket:
        def connect(self, *_a, **_k):
            raise OSError("a real connection was attempted")

    seen: dict[str, str] = {}

    def fake_run(_args):
        try:
            socket.socket().connect(("8.8.8.8", 80))
        except OSError as exc:
            seen["message"] = str(exc)
        return 0

    monkeypatch.setattr(socket, "socket", FakeSocket)
    monkeypatch.setattr(mod, "run", fake_run)
    rc = mod.main(["--raw", "unused.duckdb", "--prices", "unused.duckdb"])
    assert rc == 0
    assert seen.get("message") == block_message


# ── the report names the origins actually used ─────────────────────────────
def test_markdown_lists_the_origins_actually_used(mod):
    verdict = {"current": 20, "candidate": 20, "proposed": 20,
               "verdict": "keep", "gain": 0.0, "spread": None}
    report = {
        "inputs": {"window": 252, "holdout": 63, "horizon": 1, "winsor_k": 5.0,
                   "linkage": "ward", "benchmark": "SPY"},
        "universe": {"total": 0, "eligible": 0, "dropped_short": 0,
                     "listed_recently": 0, "missing_bars": 0},
        "origins": [{"label": "q1"}, {"label": "q2"}, {"label": "q3"}],
        "per_k": {},
        "baselines": {},
        "sector_r2": None,
        "current": [20, 80, 250],
        "proposal": {lvl: dict(verdict) for lvl in ("coarse", "medium", "fine")},
    }
    md = mod.render_markdown(report)
    assert "Origins: q1, q2, q3" in md
