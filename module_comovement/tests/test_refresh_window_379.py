"""#379 COM-3: a store-only run (Refresh, window switch, Settings recompute) reads only its window of D01.

The engine used to load every stored bar of every name (two years, ~1.96M rows live) before it could even decide to
serve a stored run. A store-only run now reads the bars from the first session its numbers use
(``ohlc.price_frame(start=...)``) and takes the three facts that need the whole history from SQL instead: each name's
first and last session, the sessions of the union index, and the halted / flat names (only the few candidates SQL
finds are read whole and judged by the same ``returns.flat_print_names``). A Fetch and a precompute still read the
whole history (the lagged test needs it).

Since the D01 switch the bars are D01's (``core.prices.daily_bars(basis="adjusted")``), over the span
``ohlc.history_start`` names; the "whole history" is that span.

The rule is "no number moves": every payload here is compared with the full read (``engine.WINDOWED_REFRESH =
False``) key for key, and ``test_recompute_parity`` keeps its golden. Offline, temp store.
"""
from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd
import pytest

from RevenueChaser.core import prices, rawstore
from RevenueChaser.module_comovement import engine, ohlc
from RevenueChaser.module_comovement.tests import d01_bars
from RevenueChaser.module_comovement.tests import test_recompute_parity as parity


def _rows(t: str, closes: pd.Series, fmt: str = "{}") -> list[dict]:
    now = time.time()
    return [{"ticker": t, "ts": fmt.format(ts.isoformat()), "open": p, "high": p, "low": p,
             "close": float(p), "volume": 1.0, "cached_at": now}
            for ts, p in closes.items() if not np.isnan(p)]


def _edge_store(stale_spy: bool = False) -> list[str]:
    """The parity market plus the names that need the whole history to be judged right."""
    ret = parity._market()
    tickers = parity._seed(ret)
    idx = ret.index
    rng = np.random.default_rng(379)
    extra: dict[str, pd.Series] = {}

    def walk() -> pd.Series:
        return pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(idx)))), index=idx)

    # Flat for 12 sessions long before any window: flagged only on the whole history.
    s = walk(); s.iloc[40:52] = s.iloc[40]; extra["FLATOLD"] = s
    # Flat for 12 sessions inside every window.
    s = walk(); s.iloc[-16:-4] = s.iloc[-16]; extra["FLATNEW"] = s
    # One unbroken run of exactly FLAT_RUN unchanged closes: a candidate SQL must find, under the share.
    s = walk(); s.iloc[100:106] = s.iloc[100]; extra["THIN"] = s
    # Bars only in the first 100 sessions: has prices, none in the window (missing bars, not no prices).
    s = walk(); s.iloc[100:] = np.nan; extra["OLDONLY"] = s
    # Listed early, then a gap that spans the window start: missing bars, not listed recently.
    s = walk(); s.iloc[60:390] = np.nan; extra["GAPPY"] = s
    rows = []
    for t, s in extra.items():
        rows += _rows(t, s)
    d01_bars.write_bars(rows)
    if stale_spy:
        d01_bars.delete_bars("SPY", idx[-5])
    names = [*tickers, *extra]
    rawstore.replace_all("equity_universe", [{
        "ticker": t, "market_cap": 1.0e10, "sector": "Technology", "price": 10.0, "name": t,
        "industry": (parity._INDUSTRIES[int(t[1])] if t[0] == "B" and t[1].isdigit() else "Unclassified"),
        "quote": {"symbol": t}} for t in names])
    return names


def _strip(p: dict) -> str:
    return json.dumps({k: v for k, v in p.items() if k not in ("as_of", "generated_at")},
                      default=str, sort_keys=True)


def _both(monkeypatch, **kw) -> tuple[str, str]:
    monkeypatch.setattr(engine, "WINDOWED_REFRESH", False)
    full = engine.run(no_fetch=True, **kw)
    monkeypatch.setattr(engine, "WINDOWED_REFRESH", True)
    windowed = engine.run(no_fetch=True, **kw)
    return _strip(full), _strip(windowed)


CASES = [(w, h, b) for w in (21, 126, 252, 378) for h in (1, 5) for b in ("spy", "off") if not (w < 126 and h > 1)]


@pytest.mark.parametrize("window,horizon,benchmark", CASES)
def test_a_store_only_run_matches_the_full_read(rawstore_tmp_db, monkeypatch, window, horizon, benchmark):
    tickers = _edge_store()
    full, windowed = _both(monkeypatch, tickers=tickers, window=window, horizon=horizon, benchmark=benchmark)
    assert windowed == full


@pytest.mark.parametrize("window", [126, 252])
def test_a_stale_benchmark_window_matches_the_full_read(rawstore_tmp_db, monkeypatch, window):
    tickers = _edge_store(stale_spy=True)
    full, windowed = _both(monkeypatch, tickers=tickers, window=window)
    assert '"benchmark_status": "stale"' in full
    assert windowed == full


def test_the_edge_names_are_judged_on_the_whole_history(rawstore_tmp_db):
    """The fixture means what it says: the full-history facts decide these names."""
    tickers = _edge_store()
    p = engine.run(no_fetch=True, tickers=tickers, window=252)
    assert {"FLATOLD", "FLATNEW"} <= set(p["flat_prints"]) and "THIN" not in p["flat_prints"]
    assert {"OLDONLY", "GAPPY"} <= set(p["dropped"]) and "SHORT" in p["dropped"]
    assert "OLDONLY" not in p["no_prices"]
    f = p["funnel"]
    assert f["listed_recently"] == 1                                      # SHORT; OLDONLY and GAPPY listed early
    assert f["missing_bars"] == len(p["dropped"]) - 1


def test_a_served_run_matches_the_full_read(rawstore_tmp_db, monkeypatch):
    tickers = _edge_store()
    engine.run(no_fetch=True, persist=True, tickers=tickers, window=252)
    full, windowed = _both(monkeypatch, tickers=tickers, window=252)
    assert windowed == full and '"config_hash": null' not in windowed


# ── what is read ────────────────────────────────────────────────────────────
@pytest.fixture
def reads(monkeypatch):
    """Every D01 bar read (``core.prices.daily_bars``, rows with a ``close``): (sql, params, rows)."""
    seen: list[tuple[str, list, list]] = []
    real = rawstore.query

    def spy(sql, params=None, *a, **kw):
        out = real(sql, params, *a, **kw)
        if "FROM price_bars_daily b" in sql and out and "close" in out[0]:
            seen.append((sql, list(params or []), out))
        return out

    monkeypatch.setattr(rawstore, "query", spy)
    if hasattr(prices, "_read_columns"):
        # K8a speed (#383) reads the bars as numpy columns, past rawstore.query: record those reads as rows too
        real_columns = prices._read_columns

        def spy_columns(sql, params, *a, **kw):
            out = real_columns(sql, params, *a, **kw)
            if "FROM price_bars_daily b" in sql and out and "close" in out and len(out["close"]):
                rows = [{"session_date": pd.Timestamp(d).date(), "close": c}
                        for d, c in zip(out["session_date"], out["close"])]
                seen.append((sql, list(params or []), rows))
            return out

        monkeypatch.setattr(prices, "_read_columns", spy_columns)
    return seen


def _sessions() -> list[str]:
    return sorted({str(r["d"]) for r in rawstore.query(
        "SELECT DISTINCT session_date d FROM price_bars_daily WHERE close IS NOT NULL", db=d01_bars.DB)})


@pytest.mark.parametrize("window,horizon", [(252, 1), (126, 5), (21, 1)])
def test_a_store_only_run_reads_only_its_window(rawstore_tmp_db, reads, window, horizon):
    tickers = _edge_store()
    sessions = _sessions()
    engine.run(no_fetch=True, tickers=tickers, window=window, horizon=horizon)
    need = max(window + horizon, engine.settings_mod.effective(window).oos_holdout + 1)
    start = sessions[-need]
    lo = ohlc.history_start()
    bounded = [r for r in reads if str(r[1][-1]) == start]
    assert len(bounded) == 1
    # the window plus each name's previous bar (the close a dividend in the window needs, K8a)
    assert len({r["session_date"] for r in bounded[0][2] if str(r["session_date"]) >= start}) == need
    whole = [r for r in reads if r not in bounded]
    assert all(r[1][-1] == lo for r in whole)
    assert {t for r in whole for t in r[1][:-2]} <= {"FLATOLD", "FLATNEW", "THIN"}   # only the flat candidates


def test_a_persisting_run_still_reads_the_whole_history(rawstore_tmp_db, reads):
    tickers = _edge_store()
    engine.run(no_fetch=True, persist=True, tickers=tickers, window=252)
    lo = ohlc.history_start()
    assert reads and all(r[1][-1] == lo for r in reads)


def test_price_frame_start_keeps_the_sessions_on_and_after_it(rawstore_tmp_db):
    _edge_store()
    sessions = _sessions()
    frame = ohlc.price_frame(["B0N00", "SPY"], start=sessions[-10])
    assert len(frame) == 10 and frame.index[0] == pd.Timestamp(sessions[-10])
    assert ohlc.price_frame(["B0N00"]).shape[0] == len(sessions)


def test_history_facts_name_the_bars_price_frame_keeps(rawstore_tmp_db):
    tickers = _edge_store()
    frame = ohlc.price_frame([*tickers, "SPY"])
    facts = ohlc.history_facts([*tickers, "SPY"])
    assert facts["sessions"] == [d.strftime("%Y-%m-%d") for d in frame.index]
    assert facts["first"] == {t: frame[t].first_valid_index().strftime("%Y-%m-%d") for t in frame.columns}
    assert facts["last"] == {t: frame[t].last_valid_index().strftime("%Y-%m-%d") for t in frame.columns}
    # Exactly the names with an unbroken run of FLAT_RUN unchanged closes (THIN has one, under the share).
    assert facts["flat"] == ["FLATNEW", "FLATOLD", "THIN"]


def test_history_facts_skip_a_nan_close(rawstore_tmp_db):
    """A NaN close is no bar, in SQL as in ``price_frame`` (D01 holds one row per session, so the old two-closes
    case cannot occur)."""
    idx = pd.date_range("2025-01-01", periods=20, freq="B")
    s = pd.Series(np.linspace(10, 20, 20), index=idx)
    d01_bars.write_bars(_rows("ONE", s) + [{"ticker": "NAN", "ts": "2025-01-01T00:00:00", "close": float("nan"),
                                            "open": None, "high": None, "low": None, "volume": 1.0,
                                            "cached_at": time.time()}])
    facts = ohlc.history_facts(["ONE", "NAN"])
    assert facts["flat"] == []
    assert "NAN" not in facts["last"] and len(facts["sessions"]) == 20
    assert list(ohlc.price_frame(["ONE", "NAN"]).columns) == ["ONE"]
