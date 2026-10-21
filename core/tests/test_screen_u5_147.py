"""#147 (27-3a), U5: one bad ticker or a short SPY series never aborts a Swing or Fundamentals scan.

Before: Fundamentals subtracted two 126-bar returns, so an SPY series under 127 bars raised ``TypeError`` out of
the worker, and both pools called ``f.result()`` unguarded, so any exception in one ticker's row killed the scan.
Now ``rs_6m`` is None when either return is, and a ticker that raises is counted as ``compute_error`` in
``data_quality.dropped`` while the scan carries on. Runs on the scenario in ``screen_parity.py``, as a Fetch: a
Refresh no longer scores (K4, #383 R1), so the tests that ran a Refresh run a Fetch now.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.scan_quality import DROP_LABELS
from RevenueChaser.core.tests import screen_parity as scn
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
from RevenueChaser.module_swing_potential import pipeline as sp
from RevenueChaser.module_swing_potential.report import render_swing_dashboard

BOARDS = [pytest.param(fp, "fundamentals", render_fundamental_dashboard, id="fundamentals"),
          pytest.param(sp, "swing", render_swing_dashboard, id="swing")]


@pytest.fixture
def scenario(rawstore_tmp_db, monkeypatch):
    scn.seed()
    scn.patch(monkeypatch)


def _short_spy(bars: int):
    """SPY's D01 series cut to its last ``bars`` sessions (the baseline reads D01 since U1, the scenario since C12)."""
    with rawstore.connect(db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw")) as conn:
        conn.execute("DELETE FROM price_bars_daily WHERE ticker = 'SPY'")
    scn.seed_d01("SPY", scn._bars("up", 0.0015, bars))


def _poison(monkeypatch, mod, *tickers):
    real = mod.load_ticker_info

    def info(ticker, *a, **k):
        if ticker in tickers:
            raise ValueError(f"bad payload for {ticker}")
        return real(ticker, *a, **k)
    monkeypatch.setattr(mod, "load_ticker_info", info)


# ── a short SPY series ────────────────────────────────────────────────────────

@pytest.mark.parametrize("bars", [100, 126])
def test_a_fundamentals_scan_survives_a_short_spy_series(scenario, bars):
    """126 bars is the edge: ``_ret(.., 126)`` needs 127, so the SPY return is None and ``rs_6m`` has no value."""
    _short_spy(bars)
    payload = fp.run_scan()
    assert payload["no_data"] is False and payload["candidates"]
    assert payload["regime"] is None, "a 100-bar SPY has no 200-day trend, so no regime"
    assert payload["data_quality"]["dropped"].get("compute_error") is None


def test_rs_6m_has_no_value_without_an_spy_return_and_the_momentum_gate_still_runs(scenario):
    _short_spy(100)
    fp.run_scan()
    rows = rawstore.query("SELECT ticker, rs_6m, momentum_pass FROM fundamentals_scores ORDER BY ticker")
    assert rows and all(r["rs_6m"] is None for r in rows)
    assert all(r["momentum_pass"] is not None for r in rows)


def test_rs_6m_keeps_its_value_with_a_full_spy_series(scenario):
    fp.run_scan()
    rows = rawstore.query("SELECT rs_6m FROM fundamentals_scores")
    assert rows and all(r["rs_6m"] is not None for r in rows)


def test_rs_6m_has_no_value_when_the_ticker_return_is_missing(scenario, monkeypatch):
    """The other operand: when the ticker's own 126-bar return is None, ``rs_6m`` is None, not a crash."""
    real = fp._ret
    spy_last = float(scn._bars("up", 0.0015, scn.LONG_BARS)["Close"].iloc[-1])

    def ret(close, n):
        is_spy = abs(float(close.iloc[-1]) - spy_last) < 1e-9
        return real(close, n) if (is_spy or n != 126) else None
    monkeypatch.setattr(fp, "_ret", ret)
    payload = fp.run_scan()
    assert payload["no_data"] is False and payload["candidates"]
    assert payload["data_quality"]["dropped"].get("compute_error") is None
    assert all(r["rs_6m"] is None for r in rawstore.query("SELECT rs_6m FROM fundamentals_scores"))


@pytest.mark.parametrize("mod,name,render", BOARDS)
def test_a_swing_or_fundamentals_board_renders_with_a_short_spy_series(scenario, mod, name, render):
    _short_spy(100)
    payload = mod.run_scan()
    assert payload["no_data"] is False
    html = render(payload)
    assert "RISK-ON" not in html and "RISK-OFF" not in html


# ── one ticker that raises ────────────────────────────────────────────────────

@pytest.mark.parametrize("mod,name,render", BOARDS)
def test_one_ticker_that_raises_is_counted_and_the_scan_carries_on(scenario, monkeypatch, mod, name, render):
    healthy = mod.run_scan()
    _poison(monkeypatch, mod, "UP3")
    payload = mod.run_scan()
    assert payload["no_data"] is False
    assert payload["data_quality"]["dropped"]["compute_error"] == 1
    assert payload["summary"]["data_short"] == healthy["summary"]["data_short"] + 1
    kept = {c["ticker"] for c in payload["candidates"]}
    assert kept and "UP3" not in kept, "the other tickers still score (Swing ranks are relative, so the set may shift)"
    assert render(payload), "the board renders with the counted drop"


@pytest.mark.parametrize("mod,name,render", BOARDS)
def test_several_raising_tickers_are_each_counted(scenario, monkeypatch, mod, name, render):
    _poison(monkeypatch, mod, "UP1", "UP3", "FLAT1")
    payload = mod.run_scan()
    assert payload["data_quality"]["dropped"]["compute_error"] == 3


@pytest.mark.parametrize("mod,name,render", BOARDS)
def test_a_fetch_still_persists_the_tickers_that_scored(scenario, monkeypatch, mod, name, render):
    _poison(monkeypatch, mod, "UP3")
    mod.run_scan()
    table = f"{name}_scores"
    tickers = {r["ticker"] for r in rawstore.query(f"SELECT ticker FROM {table}")}
    assert tickers and "UP3" not in tickers and "UP1" in tickers


@pytest.mark.parametrize("mod,name,render", BOARDS)
def test_when_every_ticker_raises_the_board_says_why(scenario, monkeypatch, mod, name, render):
    _poison(monkeypatch, mod, *scn.UNIVERSE)
    payload = mod.run_scan()
    assert payload["no_data"] is True and payload["is_fixture"] is False
    assert payload["no_data_reason"] == "no_scorable_tickers"
    assert payload["data_quality"]["dropped"]["compute_error"] >= 1
    assert DROP_LABELS["compute_error"] in payload["no_data_message"]
    assert DROP_LABELS["compute_error"] in render(payload)


def test_the_compute_error_label_is_plain_language():
    assert DROP_LABELS["compute_error"] == "unexpected error while scoring"
