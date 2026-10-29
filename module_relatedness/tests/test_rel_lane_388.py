"""#388 REL lane: the Pending split (D4), a Refresh keeps the last run's ``as_of`` (REL-10), and the peer market reads
only its window of the stored bars (the prices store since PR 8; D01 from the #379 switch before).

Offline, temp store; nothing calls DeepSeek or SEC (``build_profiles`` runs with ``use_llm=False`` on a Refresh and is
replaced on a Fetch).
"""
from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_comovement.tests import d01_bars
from RevenueChaser.module_relatedness import engine, report, store
from RevenueChaser.module_relatedness.tests.test_pure_refresh import _market_peers, _market_prices
from RevenueChaser.module_relatedness.tests.test_relatedness import _prof, _seed_filings, _seed_universe

TICKERS = [f"T{i:02d}" for i in range(8)]


@pytest.fixture
def seeded(rawstore_tmp_db, monkeypatch, tmp_path):
    """Five usable profiles, two names with no filing text, one never profiled."""
    _seed_universe(TICKERS)
    _seed_filings(TICKERS)
    monkeypatch.setattr(engine.config, "RELATEDNESS_JSON", tmp_path / "r.json")
    for t in TICKERS[:5]:
        store.save_profile(t, "cik-" + t, _prof(products=["gpu"], end_markets=["dc"]), "k", "m")
    for t in TICKERS[5:7]:
        store.save_profile(t, "cik-" + t, _prof(), "", "m", status="no_text")
    monkeypatch.setattr(engine.vc_engine, "fetch_corpus", lambda *a, **k: {"no_fetch": True})
    return TICKERS


# ── D4: Pending = tickers - usable - no_text ────────────────────────────────
def test_a_name_without_filing_text_is_not_pending(seeded):
    payload = engine.run(no_fetch=True, tickers=seeded)
    assert payload["n"] == 5
    assert payload["no_text"] == 2
    assert payload["pending"] == 1                                    # T07: no profile row yet


def test_a_no_text_name_with_a_usable_profile_counts_once(seeded):
    """A kept (stale) profile is usable: it is profiled, neither pending nor without text."""
    store.save_profile("T07", "cik-T07", _prof(products=["cpu"]), "k", "m", status="no_text")
    payload = engine.run(no_fetch=True, tickers=seeded)
    assert (payload["n"], payload["no_text"], payload["pending"]) == (6, 2, 0)


def test_the_fetch_records_the_split(seeded, monkeypatch):
    monkeypatch.setattr(engine, "build_profiles", lambda tickers, **k: store.load_profiles(tickers))
    engine.run(no_fetch=False, tickers=seeded)
    reg = rawstore.get("run_registry", "relatedness_run")["value"]
    reg = reg if isinstance(reg, dict) else json.loads(reg)
    assert (reg["pending"], reg["no_text"]) == (1, 2)


def test_the_pending_tile_says_how_many_have_no_filing_text(seeded):
    html = report.render_relatedness_dashboard(engine.run(no_fetch=True, tickers=seeded))
    assert '<div class="v">1</div><div class="l">Pending</div>' in html
    assert "2 without filing text" in html
    none = report.render_relatedness_dashboard({"pending": 3, "no_text": 0})
    assert "without filing text" not in none


def test_the_history_header_keeps_the_split(seeded, monkeypatch):
    """requested = profiled + pending + no_text in the run's result-history context."""
    monkeypatch.setattr(engine, "build_profiles", lambda tickers, **k: store.load_profiles(tickers))
    engine.run(no_fetch=False, tickers=seeded, run_id="r1")
    (head,) = rawstore.query("SELECT context FROM result_runs WHERE board = 'module_relatedness'", db="history")
    ctx = head["context"] if isinstance(head["context"], dict) else json.loads(head["context"])
    assert (ctx["requested"], ctx["profiled"], ctx["pending"], ctx["no_text"]) == (8, 5, 1, 2)


# ── REL-10: a Refresh keeps the last run's as_of ────────────────────────────
def test_a_refresh_keeps_the_last_runs_as_of(seeded):
    rawstore.write_rows("run_registry", [{"key": "relatedness_run", "value": json.dumps(
        {"as_of": "2026-10-01T10:30:00+00:00", "n": 5, "pending": 1, "next_off_peak": None})}])
    payload = engine.run(no_fetch=True, tickers=seeded)
    assert payload["as_of"] == "2026-10-01T10:30:00+00:00"
    assert "Thursday, October 01, 2026" in report.render_relatedness_dashboard(payload)


def test_a_fetch_stamps_its_own_as_of(seeded, monkeypatch):
    rawstore.write_rows("run_registry", [{"key": "relatedness_run", "value": json.dumps(
        {"as_of": "2026-10-01T10:30:00+00:00", "n": 5, "pending": 1, "next_off_peak": None})}])
    monkeypatch.setattr(engine, "build_profiles", lambda tickers, **k: store.load_profiles(tickers))
    payload = engine.run(no_fetch=False, tickers=seeded)
    assert payload["as_of"] > "2026-10-02"
    reg = rawstore.get("run_registry", "relatedness_run")["value"]
    assert (reg if isinstance(reg, dict) else json.loads(reg))["as_of"] == payload["as_of"]


def test_a_refresh_before_any_fetch_has_no_run_to_show(seeded):
    payload = engine.run(no_fetch=True, tickers=seeded)
    assert payload["as_of"]                                           # the render time, as before


# ── the peer market reads its window only ───────────────────────────────────
def _seed_prices(prices: pd.DataFrame) -> None:
    now = time.time()
    d01_bars.write_bars([
        {"ticker": t, "ts": ts.isoformat(), "open": float(p), "high": float(p), "low": float(p),
         "close": float(p), "volume": 1.0, "cached_at": now}
        for t in prices.columns for ts, p in prices[t].items() if not np.isnan(p)])


def _attach(peers):
    engine._attach_peer_market(peers)
    return json.dumps(peers, sort_keys=True)


def test_the_peer_market_equals_the_full_read(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_comovement import ohlc as comov_ohlc

    _seed_prices(_market_prices(n=420))
    real = comov_ohlc.price_frame
    monkeypatch.setattr(comov_ohlc, "price_frame", lambda tickers, start=None: real(tickers))
    full = _attach(_market_peers())
    monkeypatch.setattr(comov_ohlc, "price_frame", real)
    windowed = _attach(_market_peers())
    assert windowed == full
    assert '"move"' in full and '"beyond_spy"' in full


def test_the_peer_market_reads_only_its_window(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_comovement import config as comov_config
    from RevenueChaser.module_comovement import ohlc as comov_ohlc
    from RevenueChaser.module_comovement import settings as comov_settings

    prices = _market_prices(n=420)
    _seed_prices(prices)
    starts = []
    real = comov_ohlc.price_frame

    def spy(tickers, start=None):
        starts.append(start)
        return real(tickers, start=start)

    monkeypatch.setattr(comov_ohlc, "price_frame", spy)
    engine._attach_peer_market(_market_peers())
    need = comov_settings.effective().window + max(comov_config.HORIZONS)
    assert starts == [prices.index[-need].strftime("%Y-%m-%d")]


def test_a_short_store_reads_everything(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_comovement import ohlc as comov_ohlc

    _seed_prices(_market_prices(n=200))
    starts = []
    real = comov_ohlc.price_frame
    monkeypatch.setattr(comov_ohlc, "price_frame",
                        lambda tickers, start=None: starts.append(start) or real(tickers, start=start))
    engine._attach_peer_market(_market_peers())
    assert starts == [None]
